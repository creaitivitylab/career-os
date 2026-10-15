"""Scope retry regressions: fake HTTP, fake clocks and fake databases only."""
from datetime import timedelta
import errno
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from app import ashby_ingestion, greenhouse_ingestion, lever_ingestion, smartrecruiters_ingestion, workable_ingestion
from app.adapters.ashby import AshbyAdapter
from app.adapters.greenhouse import GreenhouseAdapter
from app.adapters.lever import LeverAdapter, LeverHTTPError, LeverPaginationError
from app.adapters.smartrecruiters import SmartRecruitersAdapter
from app.adapters.workable import WorkableAdapter
from app.direct_freshness import load_config, run_group
from app.lifecycle import InventoryCompletion, LifecycleRun
from app.operation_budget import DeadlineExceeded, ingestion_budget
from app.transient_retry import (InventoryAcquisition, UpstreamHTTPError, retryable,
                                 transient_retry_policy, validate_policy)
from test_freshness import outcome
from test_lever import LeverCursor
from test_lever_scope_health import HistoryCursor, NOW, reviewed
from test_lifecycle import Store, row
from test_stabilization import FakeCursor, OfflineTest

POLICY = {'max_transient_retries': 1, 'retry_backoff_seconds': 10}


class Clock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value
    def sleep(self, value): self.value += value


class ClassificationTests(unittest.TestCase):
    def test_read_and_connect_timeout_are_retryable(self):
        for error in [httpx.ReadTimeout('read'), httpx.ConnectTimeout('connect')]:
            with self.subTest(error=error): self.assertTrue(retryable(error))

    def test_only_reviewed_http_statuses_retry(self):
        for status in [429, 502, 503, 504]:
            with self.subTest(status=status):
                self.assertTrue(retryable(UpstreamHTTPError(status, 'native')))
                self.assertTrue(retryable(LeverHTTPError(status)))

    def test_client_errors_and_unknown_server_errors_do_not_retry(self):
        for status in [400, 401, 403, 404, 409, 500, 501]:
            with self.subTest(status=status): self.assertFalse(retryable(LeverHTTPError(status)))

    def test_validation_pagination_configuration_deadlines_do_not_retry(self):
        for error in [ValueError('bad JSON'), RuntimeError('HTTP 503 in arbitrary text'),
                      LeverPaginationError('cross_page_overlap', 2400, 100, 1),
                      DeadlineExceeded('budget'), KeyError('config'), TypeError('shape')]:
            with self.subTest(error=error): self.assertFalse(retryable(error))

    def test_identifiable_connection_reset_retries(self):
        error = httpx.ReadError('transport')
        error.__cause__ = ConnectionResetError(errno.ECONNRESET, 'reset')
        self.assertTrue(retryable(error))
        self.assertTrue(retryable(ConnectionResetError('reset')))

    def test_identifiable_temporary_dns_retries(self):
        error = httpx.ConnectError('connect')
        error.__cause__ = socket.gaierror(socket.EAI_AGAIN, 'temporary DNS')
        self.assertTrue(retryable(error))

    def test_permanent_dns_tls_and_unknown_transport_do_not_retry(self):
        for error in [httpx.ConnectError('unknown'), httpx.ReadError('unknown'),
                      httpx.PoolTimeout('local pool'), httpx.WriteTimeout('not reviewed'),
                      socket.gaierror(socket.EAI_NONAME, 'bad host'), OSError(errno.EACCES, 'denied')]:
            with self.subTest(error=error): self.assertFalse(retryable(error))

    def test_httpx_status_exception_is_typed(self):
        for status, expected in [(503, True), (404, False)]:
            response = httpx.Response(status, request=httpx.Request('GET', 'https://example.test'))
            self.assertEqual(retryable(httpx.HTTPStatusError('HTTP', request=response.request, response=response)), expected)


class RetryTests(unittest.TestCase):
    def execute(self, callback, budget=1000, policy=POLICY, sleep=None, source='lever_direct'):
        self.clock = Clock()
        with ingestion_budget(budget, self.clock), transient_retry_policy(policy):
            self.acquisition = InventoryAcquisition(source, clock=self.clock, sleep=sleep or self.clock.sleep)
            return self.acquisition.run(('global', 'tsmg'), callback)

    def test_exact_tsmg_read_timeout_then_success(self):
        callback = Mock(side_effect=[httpx.ReadTimeout('The read operation timed out'), ['complete']])
        self.assertEqual(self.execute(callback), ['complete'])
        self.assertEqual(callback.call_count, 2)
        record = self.acquisition.records[0]
        self.assertEqual([x['result'] for x in record['attempts']], ['FAILED', 'ACQUIRED'])
        self.assertEqual(record['attempts'][0]['error_class'], 'ReadTimeout')
        self.assertTrue(record['attempts'][0]['retryable'])
        self.assertEqual(record['retry_delay_seconds'], 10)

    def test_read_timeout_twice_never_third_attempt(self):
        callback = Mock(side_effect=httpx.ReadTimeout('timeout'))
        with self.assertRaises(httpx.ReadTimeout): self.execute(callback)
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(self.acquisition.summary()['retry_metrics'], {
            'transient_failures': 2, 'transient_retries': 1, 'retry_successes': 0, 'retry_failures': 1})

    def test_connect_timeout_retry(self):
        callback = Mock(side_effect=[httpx.ConnectTimeout('connect'), []])
        self.assertEqual(self.execute(callback), [])
        self.assertEqual(callback.call_count, 2)

    def test_429_502_503_504_retry_once(self):
        for status in [429, 502, 503, 504]:
            with self.subTest(status=status):
                callback = Mock(side_effect=[UpstreamHTTPError(status, 'native'), []])
                self.execute(callback)
                self.assertEqual(callback.call_count, 2)
                self.assertEqual(self.acquisition.records[0]['attempts'][0]['http_status'], status)

    def test_nonretryable_errors_make_only_one_attempt(self):
        for error in [LeverHTTPError(400), LeverHTTPError(404),
                      LeverPaginationError('cross_page_overlap', 2400, 100, 1), ValueError('JSON')]:
            with self.subTest(error=error):
                callback = Mock(side_effect=error)
                with self.assertRaises(type(error)): self.execute(callback)
                self.assertEqual(callback.call_count, 1)

    def test_insufficient_budget_reports_original_failure_without_sleep(self):
        error = httpx.ReadTimeout('original')
        callback = Mock(side_effect=error); sleep = Mock()
        with self.assertRaises(httpx.ReadTimeout) as caught: self.execute(callback, budget=70, sleep=sleep)
        self.assertIs(caught.exception, error)
        self.assertEqual(callback.call_count, 1); sleep.assert_not_called()
        self.assertEqual(self.acquisition.records[0]['retry_skip_reason'], 'insufficient_operation_budget')

    def test_expensive_first_traversal_requires_matching_reserve(self):
        calls = []
        def callback():
            calls.append(1); self.clock.value += 100
            raise httpx.ReadTimeout('slow')
        with self.assertRaises(httpx.ReadTimeout): self.execute(callback, budget=150)
        self.assertEqual(len(calls), 1)

    def test_budget_rechecked_after_backoff(self):
        callback = Mock(side_effect=httpx.ReadTimeout('original'))
        def oversleep(delay): self.clock.value += 100
        with self.assertRaises(httpx.ReadTimeout): self.execute(callback, budget=150, sleep=oversleep)
        self.assertEqual(callback.call_count, 1)
        self.assertEqual(self.acquisition.records[0]['retry_skip_reason'], 'insufficient_budget_after_backoff')

    def test_long_retry_after_is_not_ignored_or_slept_unboundedly(self):
        callback = Mock(side_effect=UpstreamHTTPError(429, 'rate limit', '120'))
        sleep = Mock()
        with self.assertRaises(UpstreamHTTPError): self.execute(callback, sleep=sleep)
        self.assertEqual(callback.call_count, 1); sleep.assert_not_called()
        self.assertEqual(self.acquisition.records[0]['retry_skip_reason'], 'retry_after_exceeds_bounded_backoff')

    def test_short_retry_after_uses_configured_bounded_backoff(self):
        callback = Mock(side_effect=[UpstreamHTTPError(503, 'native', '5'), []])
        self.execute(callback)
        self.assertEqual(self.clock.value, 10)

    def test_malformed_retry_after_is_conservative(self):
        for value in ['nonsense', 'NaN', '-1']:
            with self.subTest(value=value):
                callback = Mock(side_effect=UpstreamHTTPError(429, 'native', value))
                with self.assertRaises(UpstreamHTTPError): self.execute(callback)
                self.assertEqual(callback.call_count, 1)

    def test_no_budget_no_automatic_retry(self):
        with transient_retry_policy(POLICY):
            acquisition = InventoryAcquisition('lever_direct', sleep=Mock())
            callback = Mock(side_effect=httpx.ReadTimeout('timeout'))
            with self.assertRaises(httpx.ReadTimeout): acquisition.run(('global', 'tsmg'), callback)
        self.assertEqual(callback.call_count, 1)

    def test_ordinary_endpoint_remains_single_attempt(self):
        with ingestion_budget(10**20):
            callback = Mock(side_effect=httpx.ReadTimeout('timeout'))
            acquisition = InventoryAcquisition('lever_direct', sleep=Mock())
            with self.assertRaises(httpx.ReadTimeout): acquisition.run(('global', 'tsmg'), callback)
        self.assertEqual(callback.call_count, 1)

    def test_heavy_and_aggregator_never_gain_retry_policy(self):
        for source in ['workday_direct', 'successfactors_direct', 'fantastic_jobs_apify', 'jooble_direct']:
            callback = Mock(side_effect=httpx.ReadTimeout('timeout'))
            with self.subTest(source=source), self.assertRaises(httpx.ReadTimeout): self.execute(callback, source=source)
            self.assertEqual(callback.call_count, 1)

    def test_zero_retries_explicitly_disables(self):
        callback = Mock(side_effect=httpx.ReadTimeout('timeout'))
        with self.assertRaises(httpx.ReadTimeout): self.execute(callback, policy=POLICY | {'max_transient_retries': 0})
        self.assertEqual(callback.call_count, 1)

    def test_invalid_retry_configuration_rejected(self):
        for changed in [{'max_transient_retries': 2}, {'max_transient_retries': True},
                        {'retry_backoff_seconds': 0}, {'retry_backoff_seconds': 16},
                        {'retry_backoff_seconds': float('nan')}]:
            with self.subTest(changed=changed), self.assertRaises(ValueError): validate_policy(POLICY | changed)

    def test_second_call_for_same_scope_is_not_an_implicit_extra_retry(self):
        self.execute(Mock(return_value=[]))
        with self.assertRaises(ValueError): self.acquisition.run(('global', 'tsmg'), Mock())

    def test_logging_retains_retry_without_exception_secrets(self):
        with self.assertLogs('career_os.freshness', level='INFO') as logs:
            self.execute(Mock(side_effect=[httpx.ReadTimeout('token=secret'), []]))
        self.assertIn('scope=', '\n'.join(logs.output))
        self.assertIn('ReadTimeout', '\n'.join(logs.output))
        self.assertNotIn('token=secret', '\n'.join(logs.output))

    def test_fatal_processing_is_not_retried_or_reported_as_complete(self):
        callback = Mock(side_effect=[httpx.ReadTimeout('transport'), []])
        calls = []
        def execute(source, request):
            calls.append(source)
            InventoryAcquisition(source).run(('global', 'tsmg'), callback)
            raise RuntimeError('processing failed after acquisition')
        with tempfile.TemporaryDirectory() as directory, patch('app.transient_retry.time.sleep'):
            result = run_group('light', load_config(), only_source='lever_direct', execute=execute,
                               lock_path=Path(directory) / 'lock')
        self.assertEqual(calls, ['lever_direct'])
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['sources'][0]['scope_attempts'][0]['final_scope_result'], 'ABORTED')
        self.assertEqual(result['retry_metrics']['retry_successes'], 0)
        self.assertEqual(result['retry_metrics']['retry_failures'], 1)


class TraversalSafetyTests(unittest.TestCase):
    def test_partial_first_pages_never_contribute_to_reconciliation(self):
        clock = Clock()
        db = Store([row('global:tsmg:first', source='lever_direct'),
                    row('global:tsmg:second', source='lever_direct')])
        lifecycle = LifecycleRun(db, 'lever_direct', 'run')
        with patch('app.adapters.lever.httpx.Client') as factory:
            client = factory.return_value.__enter__.return_value
            client.get.side_effect = [httpx.Response(200, json=[{'id': 'first'}]), httpx.ReadTimeout('tsmg'),
                                      httpx.Response(200, json=[{'id': 'second'}]), httpx.Response(200, json=[])]
            adapter = LeverAdapter(); adapter.PAGE_SIZE = 1
            with ingestion_budget(1000, clock), transient_retry_policy(POLICY):
                acquisition = InventoryAcquisition('lever_direct', clock=clock, sleep=clock.sleep)
                result = acquisition.run(('global', 'tsmg'), lambda: adapter.list_postings('tsmg'))
                self.assertEqual(result, [{'id': 'second'}])
                self.assertEqual([call.kwargs['params']['skip'] for call in client.get.call_args_list], [0, 1, 0, 1])
                self.assertEqual(len(db.calls), 1)  # Only source advisory lock, no snapshot/writes yet.
                inventory = lifecycle.scope('global', 'tsmg')
                inventory.observe('global:tsmg:second')
                acquisition.finish(inventory, InventoryCompletion(True, True, 'second traversal complete'))
        self.assertFalse(db.sources['global:tsmg:first']['active'])
        self.assertTrue(db.sources['global:tsmg:second']['active'])
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(acquisition.records[0]['retry_result'], 'COMPLETE')
        self.assertEqual(acquisition.records[0]['metrics']['retry_successes'], 1)

    def test_two_failed_traversals_preserve_existing_source(self):
        clock = Clock(); db = Store([row('global:tsmg:old', source='lever_direct')])
        lifecycle = LifecycleRun(db, 'lever_direct', 'run')
        with ingestion_budget(1000, clock), transient_retry_policy(POLICY):
            acquisition = InventoryAcquisition('lever_direct', clock=clock, sleep=clock.sleep)
            callback = Mock(side_effect=httpx.ReadTimeout('tsmg'))
            with self.assertRaises(httpx.ReadTimeout): acquisition.run(('global', 'tsmg'), callback)
            acquisition.finish(lifecycle.scope('global', 'tsmg'), InventoryCompletion(False, False, 'failed'))
        self.assertEqual(callback.call_count, 2)
        self.assertTrue(db.sources['global:tsmg:old']['active'])
        self.assertEqual(db.sources['global:tsmg:old']['last_verified'], 'original')
        self.assertEqual(acquisition.records[0]['final_scope_result'], 'FAILED')

    def test_successful_http_retry_does_not_override_incomplete_inventory(self):
        clock = Clock(); db = Store([row('global:tsmg:old', source='lever_direct')])
        lifecycle = LifecycleRun(db, 'lever_direct', 'run')
        with ingestion_budget(1000, clock), transient_retry_policy(POLICY):
            acquisition = InventoryAcquisition('lever_direct', clock=clock, sleep=clock.sleep)
            acquisition.run(('global', 'tsmg'), Mock(side_effect=[httpx.ReadTimeout('timeout'), []]))
            clock.value += 3
            acquisition.finish(lifecycle.scope('global', 'tsmg'), InventoryCompletion(True, False, 'bad records'))
        self.assertTrue(db.sources['global:tsmg:old']['active'])
        self.assertEqual(acquisition.records[0]['retry_result'], 'INCOMPLETE')
        self.assertEqual(acquisition.records[0]['metrics']['retry_successes'], 0)
        self.assertEqual(acquisition.records[0]['duration_seconds'], 13)

    def test_jobgether_overlap_is_not_retried(self):
        clock = Clock()
        with patch('app.adapters.lever.httpx.Client') as factory:
            client = factory.return_value.__enter__.return_value
            client.get.side_effect = [httpx.Response(200, json=[{'id': 'one'}]),
                                      httpx.Response(200, json=[{'id': 'one'}])]
            adapter = LeverAdapter(); adapter.PAGE_SIZE = 1
            with ingestion_budget(1000, clock), transient_retry_policy(POLICY):
                acquisition = InventoryAcquisition('lever_direct', clock=clock, sleep=clock.sleep)
                with self.assertRaises(LeverPaginationError):
                    acquisition.run(('global', 'jobgether'), lambda: adapter.list_postings('jobgether'))
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(acquisition.summary()['retry_metrics']['transient_retries'], 0)


class AdapterHTTPStatusTests(unittest.TestCase):
    def test_all_light_listing_and_detail_paths_keep_typed_status_and_retry_after(self):
        calls = [lambda: SmartRecruitersAdapter().list_postings('acme'),
                 lambda: SmartRecruitersAdapter().get_posting('acme', '1'),
                 lambda: GreenhouseAdapter().list_jobs('acme'),
                 lambda: GreenhouseAdapter().get_job('acme', '1'),
                 lambda: WorkableAdapter().get_account('acme'),
                 lambda: AshbyAdapter().list_jobs('acme'),
                 lambda: LeverAdapter().list_postings('acme')]
        for invoke in calls:
            with self.subTest(invoke=invoke), patch('httpx.Client') as factory:
                factory.return_value.__enter__.return_value.get.return_value = httpx.Response(
                    503, json={'secret': 'do not log response bodies'}, headers={'Retry-After': '7'})
                with self.assertRaises(UpstreamHTTPError) as caught: invoke()
                self.assertEqual(caught.exception.status_code, 503)
                self.assertEqual(caught.exception.retry_after, '7')
                self.assertNotIn('secret', str(caught.exception))


class IngestionIntegrationTests(OfflineTest):
    def test_each_light_ingestion_uses_the_same_scope_retry(self):
        cases = [
            (smartrecruiters_ingestion.ingest_smartrecruiters_jobs, {'company_identifier': 'acme'},
             SmartRecruitersAdapter, 'list_postings', []),
            (greenhouse_ingestion.ingest_greenhouse_jobs, {'board_token': 'acme'},
             GreenhouseAdapter, 'list_jobs', []),
            (workable_ingestion.ingest_workable_jobs, {'tenant_slug': 'acme'},
             WorkableAdapter, 'get_account', {'jobs': []}),
            (ashby_ingestion.ingest_ashby_jobs, {'board_name': 'acme'}, AshbyAdapter, 'list_jobs', []),
            (lever_ingestion.ingest_lever_jobs, {'site': 'tsmg', 'instance': 'global'},
             LeverAdapter, 'list_postings', []),
        ]
        for invoke, kwargs, adapter, method, result in cases:
            with self.subTest(source=adapter.__name__):
                cur = LeverCursor() if adapter is LeverAdapter else FakeCursor()
                self.fake_database(cur)
                with ingestion_budget(10**20), transient_retry_policy(POLICY), patch('app.transient_retry.time.sleep'), \
                     patch.object(workable_ingestion, 'WORKABLE_TENANTS', {'Acme': 'acme'}), \
                     patch.object(ashby_ingestion, 'discover_boards', return_value=[('Acme', 'acme')]), \
                     patch.object(adapter, method, side_effect=[httpx.ReadTimeout('timeout'), result]) as request:
                    value = invoke(**kwargs)
                self.assertEqual(request.call_count, 2)
                self.assertTrue(value['lifecycle']['scopes'][0]['complete'])
                self.assertEqual(value['retry_metrics']['retry_successes'], 1)
                self.assertIn('scope_attempts', cur.run_update['metadata'].obj)

    def test_tsmg_successful_retry_and_jobgether_gap_keep_runner_healthy(self):
        cur = HistoryCursor(); self.fake_database(cur)
        policy = reviewed('NEEDS_REVIEW'); policy['scope'] = ['global', 'jobgether']
        policy['review_expires_at'] = (NOW + timedelta(days=20)).isoformat()
        sites = [lever_ingestion.LeverSite('global', 'tsmg', 'TSMG'),
                 lever_ingestion.LeverSite('global', 'jobgether', 'Jobgether')]
        def execute(source, request):
            return lever_ingestion.ingest_lever_jobs() if source == 'lever_direct' else outcome(source)
        with tempfile.TemporaryDirectory() as directory, patch('app.transient_retry.time.sleep'), \
             patch.object(lever_ingestion, 'discover_sites', return_value=sites), \
             patch.object(lever_ingestion, 'load_policy', return_value=[policy]), \
             patch('app.lever_scope_health.load_policy', return_value=[policy]), \
             patch('app.lever_ingestion.datetime', wraps=__import__('datetime').datetime) as now, \
             patch.object(LeverAdapter, 'probe', return_value=200) as probe, \
             patch.object(LeverAdapter, 'list_postings', side_effect=[httpx.ReadTimeout('tsmg'), []]) as listing:
            now.now.return_value = NOW
            result = run_group('light', load_config(), execute=execute, now=lambda: NOW,
                               lock_path=Path(directory) / 'lock')
        self.assertEqual(result['status'], 'SUCCESS_WITH_KNOWN_GAPS')
        self.assertEqual(result['retry_metrics']['retry_successes'], 1)
        self.assertEqual(listing.call_count, 2); probe.assert_called_once()
        self.assertTrue(all(call.args[0] == 'tsmg' for call in listing.call_args_list))

    def test_failed_tsmg_retry_keeps_runner_partial(self):
        self.fake_database(LeverCursor())
        def execute(source, request):
            return lever_ingestion.ingest_lever_jobs(site='tsmg', instance='global') if source == 'lever_direct' else outcome(source)
        with tempfile.TemporaryDirectory() as directory, patch('app.transient_retry.time.sleep'), \
             patch.object(LeverAdapter, 'list_postings', side_effect=httpx.ReadTimeout('tsmg')) as listing:
            result = run_group('light', load_config(), execute=execute, lock_path=Path(directory) / 'lock')
        self.assertEqual(listing.call_count, 2)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['retry_metrics']['retry_failures'], 1)
        self.assertEqual(result['sources'][-1]['deactivated'], 0)

    def test_smartrecruiters_detail_timeout_restarts_listing_before_writes(self):
        cur = FakeCursor(); self.fake_database(cur)
        listing = self.set_api(SmartRecruitersAdapter, 'list_postings', result=[{'id': '1'}])
        def details(*args):
            self.assertFalse(cur.source_writes)
            self.assertFalse(cur.canonical_writes)
            raise httpx.ReadTimeout('detail')
        detail = self.set_api(SmartRecruitersAdapter, 'get_posting', side_effect=details)
        with ingestion_budget(10**20), transient_retry_policy(POLICY), patch('app.transient_retry.time.sleep'):
            value = smartrecruiters_ingestion.ingest_smartrecruiters_jobs(company_identifier='acme')
        self.assertEqual(listing.call_count, 2); self.assertEqual(detail.call_count, 2)
        self.assertFalse(value['lifecycle']['scopes'][0]['complete'])
        self.assertFalse(cur.source_writes)

    def test_greenhouse_detail_timeout_restarts_listing_before_writes(self):
        cur = FakeCursor(); self.fake_database(cur)
        listing = self.set_api(GreenhouseAdapter, 'list_jobs', result=[{'id': 1, 'location': {'name': 'Prague'}}])
        detail = self.set_api(GreenhouseAdapter, 'get_job', side_effect=httpx.ReadTimeout('detail'))
        with ingestion_budget(10**20), transient_retry_policy(POLICY), patch('app.transient_retry.time.sleep'):
            value = greenhouse_ingestion.ingest_greenhouse_jobs(board_token='acme')
        self.assertEqual(listing.call_count, 2); self.assertEqual(detail.call_count, 2)
        self.assertFalse(cur.source_writes)
        self.assertEqual(value['retry_metrics']['retry_failures'], 1)

    def test_successful_smartrecruiters_detail_retry_discards_first_buffer(self):
        self.check_detail_recovery(smartrecruiters_ingestion, SmartRecruitersAdapter,
                                   'list_postings', 'get_posting', {'company_identifier': 'acme'})

    def test_successful_greenhouse_detail_retry_discards_first_buffer(self):
        self.check_detail_recovery(greenhouse_ingestion, GreenhouseAdapter,
                                   'list_jobs', 'get_job', {'board_token': 'acme'})

    def check_detail_recovery(self, module, adapter, listing_method, detail_method, kwargs):
        cur = FakeCursor(); self.fake_database(cur)
        one = {'id': '1', 'location': {'name': 'Prague'}}
        two = {'id': '2', 'location': {'name': 'Prague'}}
        listing = self.set_api(adapter, listing_method, side_effect=[[one, two], [two]])
        calls = []
        def detail(tenant, identity):
            calls.append(identity)
            self.assertFalse(cur.source_writes)
            self.assertFalse(cur.canonical_writes)
            if len(calls) == 2:
                raise httpx.ReadTimeout('first traversal detail failed')
            return {'id': identity, 'title': 'Engineer', 'name': 'Engineer',
                    'company': {'name': 'Acme'}, 'company_name': 'Acme',
                    'location': {'name': 'Prague', 'city': 'Prague', 'country': 'cz'}}
        self.set_api(adapter, detail_method, side_effect=detail)
        invoke = module.ingest_smartrecruiters_jobs if adapter is SmartRecruitersAdapter else module.ingest_greenhouse_jobs
        with ingestion_budget(10**20), transient_retry_policy(POLICY), patch('app.transient_retry.time.sleep'):
            value = invoke(**kwargs)
        self.assertEqual(listing.call_count, 2)
        self.assertEqual(calls, ['1', '2', '2'])
        self.assertEqual(value['fetched'], 1)
        self.assertEqual(value['details_loaded'], 1)
        self.assertEqual(value['failed'], 0)
        self.assertTrue(value['lifecycle']['scopes'][0]['complete'])
        self.assertEqual(value['retry_metrics']['retry_successes'], 1)
        self.assertEqual(len(cur.source_writes), 1)
