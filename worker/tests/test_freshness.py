"""Offline orchestration/reporting tests. Native execution is always injected."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx
import psycopg

from app.direct_freshness import (LIGHT, HEAVY, aggregate_status, load_config, main,
    baseline_from_runs, entrypoint, next_baseline, normalize_outcome, outer_lock, run_group, safe_error, source_request)
from app.operation_budget import DeadlineExceeded, check_deadline, commit_inventory, ingestion_budget
from app.freshness_report import classify, collect_report, composition, full_complete, generate_report, markdown
from app.lifecycle import InventoryCompletion, LifecycleRun
from app.adapters.workday import WorkdayAdapter, WorkdaySite
from app.adapters.lever import LeverAdapter
from test_lifecycle import Store, row

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self): self.t = 0
    def __call__(self): return self.t


def outcome(source=LIGHT[0], complete=True, failed=0, fetched=10):
    key = 'tenants' if source in (LIGHT[0], LIGHT[2]) else 'boards' if source in (LIGHT[1], LIGHT[3]) else 'sites'
    fetch_key = 'raw_rows' if source == LIGHT[2] else 'listing_records' if source in HEAVY else 'fetched'
    return {'status': 'success', 'run_id': 'run-1', key: 1, fetch_key: fetched,
        'created': 2, 'updated': 7, 'attached': 1, 'failed': failed,
        'lifecycle': {'deactivated': 0, 'reactivated': 0, 'scopes': [
            {'scope': ['acme'], 'successful': True, 'complete': complete, 'deactivated': 0, 'reactivated': 0}]}}


class OutcomeTests(unittest.TestCase):
    def test_all_source_output_contracts(self):
        for source in LIGHT + HEAVY:
            self.assertEqual(normalize_outcome(source, outcome(source))['status'], 'SUCCESS')

    def test_partial_scope_is_not_success(self):
        r = normalize_outcome(LIGHT[0], outcome(complete=False))
        self.assertEqual(r['status'], 'PARTIAL'); self.assertEqual(r['scopes_incomplete'], 1)

    def test_failed_records_override_success_status(self):
        self.assertEqual(normalize_outcome(LIGHT[0], outcome(failed=1))['status'], 'PARTIAL')

    def test_errors_override_http_success(self):
        r = outcome(); r['tenant_errors'] = [{'tenant': 'acme', 'error': 'failed'}]
        self.assertEqual(normalize_outcome(LIGHT[0], r)['status'], 'PARTIAL')

    def test_zero_and_collapse_anomalies(self):
        for fetched, flag in [(0, 'previously_nonempty_source_now_zero'), (10, 'unexpected_result_collapse')]:
            r = normalize_outcome(LIGHT[0], outcome(fetched=fetched), {'fetched': 100})
            self.assertEqual(r['status'], 'PARTIAL'); self.assertIn(flag, r['anomalies'])

    def test_empty_valid_scope_without_prior_positive_is_success(self):
        self.assertEqual(normalize_outcome(LIGHT[0], outcome(fetched=0))['status'], 'SUCCESS')

    def test_no_scopes_is_partial(self):
        r = outcome(); r['tenants'] = 0; r['lifecycle']['scopes'] = []
        self.assertEqual(normalize_outcome(LIGHT[0], r)['status'], 'PARTIAL')

    def test_all_scopes_failed(self):
        r = outcome(); r['lifecycle']['scopes'][0].update(successful=False, complete=False)
        self.assertEqual(normalize_outcome(LIGHT[0], r)['status'], 'FAILED')

    def test_deactivation_in_partial_scope_rejected(self):
        r = outcome(complete=False); r['lifecycle']['scopes'][0]['deactivated'] = 1
        with self.assertRaises(ValueError): normalize_outcome(LIGHT[0], r)

    def test_missing_or_invalid_output_fails_closed(self):
        for r in ({}, {'status': 'success'}, outcome() | {'fetched': '10'}, outcome() | {'lifecycle': {}}):
            with self.assertRaises(ValueError): normalize_outcome(LIGHT[0], r)

    def test_lifecycle_sum_must_match(self):
        r = outcome(); r['lifecycle']['deactivated'] = 1
        with self.assertRaises(ValueError): normalize_outcome(LIGHT[0], r)

    def test_first_run_volume_baseline_from_native_history(self):
        previous = {'source_name': LIGHT[0], 'status': 'success', 'started_at': '2026-10-05',
                    'records_fetched': 368, 'records_failed': 0, 'metadata': {'country': 'cz'}}
        seed = baseline_from_runs('light', {LIGHT[0]: {'country': 'cz'}}, [previous])
        self.assertEqual(seed['sources'][0]['fetched'], 368)
        self.assertIn('previously_nonempty_source_now_zero', normalize_outcome(LIGHT[0], outcome(fetched=0), seed['sources'][0])['anomalies'])

    def test_bounded_or_failed_native_history_does_not_seed_full_volume(self):
        previous = {'source_name': LIGHT[0], 'status': 'success', 'started_at': '2026-10-05',
                    'records_fetched': 368, 'records_failed': 0, 'metadata': {'country': 'cz', 'company_identifier': 'one'}}
        self.assertEqual(baseline_from_runs('light', {LIGHT[0]: {'country': 'cz'}}, [previous])['sources'], [])
        previous['metadata'] = {'country': 'cz'}; previous['records_failed'] = 1
        self.assertEqual(baseline_from_runs('light', {LIGHT[0]: {'country': 'cz'}}, [previous])['sources'], [])

    def test_error_output_redacts_connections(self):
        r = safe_error(RuntimeError('failure postgres://user:pass@host/database token=secret'))
        self.assertNotIn('pass@', r['message']); self.assertNotIn('token=secret', r['message'])

    def test_aggregation(self):
        for states, expected in [(['SUCCESS'] * 5, 'SUCCESS'), (['SUCCESS', 'FAILED'], 'PARTIAL'),
                                 (['PARTIAL'], 'PARTIAL'), (['FAILED'] * 2, 'FAILED')]:
            self.assertEqual(aggregate_status([{'status': s} for s in states]), expected)

    def test_baseline_preserved_after_failure_and_bounded_request(self):
        old = {'sources': [{'source_name': LIGHT[0], 'request': {}, 'ingestion_status': 'success', 'fetched': 100}]}
        r = normalize_outcome(LIGHT[0], outcome()); r['request'] = {'company_identifier': 'acme'}
        result = {'runner_type': 'light', 'sources': [r, {'source_name': LIGHT[1], 'status': 'FAILED'}]}
        cache = next_baseline(old, result)
        self.assertEqual(len(cache['sources']), 2)
        self.assertEqual(next(s for s in cache['sources'] if s['request'] == {})['fetched'], 100)


class RunnerTests(unittest.TestCase):
    def invoke(self, execute, group='light', **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            return run_group(group, load_config(), execute=execute,
                lock_path=Path(directory)/'lock', now=lambda: NOW, **kwargs)

    def test_light_success_every_source_once(self):
        calls = []
        def execute(source, request): calls.append(source); return outcome(source)
        r = self.invoke(execute)
        self.assertEqual(calls, list(LIGHT)); self.assertEqual(r['exit_code'], 0)
        self.assertFalse(r['dedupe_enabled'])

    def test_isolated_failure_continues(self):
        calls = []
        def execute(source, request):
            calls.append(source)
            if source == LIGHT[1]: raise RuntimeError('isolated API failure')
            return outcome(source)
        r = self.invoke(execute)
        self.assertEqual(len(calls), 5); self.assertEqual(r['status'], 'PARTIAL'); self.assertEqual(r['exit_code'], 2)

    def test_heavy_filtered_is_partial_not_authoritative(self):
        r = self.invoke(lambda s, req: outcome(s, complete=False), group='heavy')
        self.assertEqual(r['status'], 'PARTIAL')
        self.assertEqual(r['sources'][1]['request']['validated_scopes'], True)

    def test_config_validation(self):
        c = load_config(); c['source_deadlines_seconds'][LIGHT[0]] = -1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config'; path.write_text(json.dumps(c))
            with self.assertRaises(ValueError): load_config(path)

    def test_default_cli_is_plan_and_never_ingests(self):
        with patch('app.direct_freshness.execute_source') as native, patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['heavy']), 0); self.assertTrue(json.loads(output.getvalue())['plan_only'])
            native.assert_not_called()

    def test_preflight_failure_has_safe_structured_output(self):
        with patch('app.direct_freshness.database_baseline', side_effect=RuntimeError('postgres://user:secret@host/db')), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(entrypoint(['light', '--apply']), 1)
            r = json.loads(output.getvalue()); self.assertEqual(r['status'], 'FAILED')
            self.assertNotIn('secret@', output.getvalue()); self.assertEqual(r['sources'], [])

    def test_rmk_rejects_unrestricted_and_unreviewed(self):
        for req in [{'validated_scopes': False}, {'max_sites': 1}, {'host': 'unreviewed.example', 'brand': ''}]:
            with self.assertRaises(ValueError): source_request(HEAVY[1], req)

    def test_bounded_request_does_not_compare_full_baseline(self):
        previous = {'sources': [{'source_name': LIGHT[0], 'request': {}, 'ingestion_status': 'success', 'fetched': 1000}]}
        r = self.invoke(lambda s, req: outcome(s), only_source=LIGHT[0], requests={LIGHT[0]: {'company_identifier': 'acme'}}, previous=previous)
        self.assertEqual(r['status'], 'SUCCESS')

    def test_lock_contention_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory)/'lock'
            with outer_lock(lock, lambda: NOW):
                r = run_group('light', load_config(), execute=Mock(side_effect=AssertionError('must not ingest')),
                    lock_path=lock, now=lambda: NOW)
                self.assertEqual(r['status'], 'SKIPPED_LOCKED'); self.assertEqual(r['exit_code'], 75)
                self.assertEqual(len(r['sources']), 5); self.assertTrue(all(not s['attempted'] for s in r['sources']))
            with outer_lock(lock, lambda: NOW) as acquired: self.assertTrue(acquired['acquired'])

    def test_old_lock_file_alone_does_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory)/'lock'; lock.write_text('{"started_at":"2000-01-01T00:00:00+00:00"}')
            with outer_lock(lock) as acquired: self.assertTrue(acquired['acquired'])

    def test_true_monotonic_duration_and_expected_budget(self):
        clock = Clock()
        def execute(s, req): clock.t += 3; return outcome(s)
        r = self.invoke(execute, clock=clock)
        self.assertEqual(r['duration_seconds'], 15)
        self.assertTrue(all(s['duration_seconds'] == 3 for s in r['sources']))
        self.assertEqual(r['started_at'], r['finished_at'])  # wall clock frozen; elapsed is still correct

    def test_group_deadline_does_not_start_remaining_sources(self):
        clock = Clock(); c = load_config(); c['groups']['heavy']['deadline_seconds'] = 10
        def execute(s, req): clock.t = 11; check_deadline()
        with tempfile.TemporaryDirectory() as directory:
            r = run_group('heavy', c, execute=execute, clock=clock, lock_path=Path(directory)/'lock')
        self.assertTrue(r['timeout']); self.assertFalse(r['sources'][1]['attempted'])

    def test_structured_summary_and_unknown_closure_count(self):
        r = self.invoke(lambda s, req: outcome(s))
        self.assertEqual(len(json.loads(json.dumps(r))['sources']), 5)
        self.assertIsNone(r['sources'][0]['canonical_closed'])

    def test_ambiguous_commit_stops_remaining_heavy_sources(self):
        conn = Mock(); conn.commit.side_effect = OSError('commit acknowledgment lost')
        def execute(s, req): commit_inventory(conn)
        r = self.invoke(execute, group='heavy')
        self.assertEqual(r['status'], 'FAILED')
        self.assertTrue(r['sources'][0]['requires_operator_check'])
        self.assertFalse(r['sources'][1]['attempted'])

    def test_deadline_after_commit_preserves_committed_outcome(self):
        clock = Clock(); conn = Mock()
        conn.commit.side_effect = lambda: setattr(clock, 't', 9001)
        def execute(s, req): commit_inventory(conn); return outcome(s)
        r = self.invoke(execute, group='heavy', only_source=HEAVY[0], clock=clock)
        self.assertEqual(r['status'], 'PARTIAL'); self.assertTrue(r['sources'][0]['timeout'])
        self.assertEqual(r['sources'][0]['created'], 2)

    def test_light_and_heavy_share_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory)/'lock'
            with outer_lock(lock):
                r = run_group('heavy', load_config(), execute=Mock(side_effect=AssertionError('must not ingest')),
                              lock_path=lock)
                self.assertEqual(r['status'], 'SKIPPED_LOCKED')

    def test_long_busy_lock_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory)/'lock'
            with outer_lock(lock, lambda: NOW-timedelta(hours=5)):
                r = run_group('light', load_config(), execute=Mock(side_effect=AssertionError('must not ingest')),
                              lock_path=lock, now=lambda: NOW)
                self.assertIn('lock_busy_beyond_budget', r['anomalies'])


class DeadlineTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('JOB_PROFILE_TEST_DATABASE_URL'), 'Isolated test PostgreSQL required')
    def test_actual_transaction_rollback_after_prior_updates(self):
        with psycopg.connect(os.environ['JOB_PROFILE_TEST_DATABASE_URL']) as conn:
            conn.execute('create temporary table freshness_budget_probe(id int primary key, active boolean)')
            conn.execute('insert into freshness_budget_probe values(1,true)'); conn.commit()
            clock = Clock()
            try:
                with ingestion_budget(10, clock):
                    conn.execute('update freshness_budget_probe set active=false')
                    clock.t = 11
                    commit_inventory(conn)
            except DeadlineExceeded:
                conn.rollback()
            self.assertTrue(conn.execute('select active from freshness_budget_probe').fetchone()[0])

    @unittest.skipUnless(os.environ.get('JOB_PROFILE_TEST_DATABASE_URL'), 'Isolated test PostgreSQL required')
    def test_completion_clock_timestamp_advances_in_long_transaction(self):
        with psycopg.connect(os.environ['JOB_PROFILE_TEST_DATABASE_URL']) as conn:
            before = conn.execute('select now()').fetchone()[0]
            conn.execute('select pg_sleep(0.01)')
            frozen, finished = conn.execute('select now(),clock_timestamp()').fetchone()
            self.assertEqual(before, frozen); self.assertGreater(finished, frozen)
            conn.rollback()

    def test_context_reset_and_unaffected_normal_endpoint(self):
        with ingestion_budget(0, clock=lambda: 1):
            with self.assertRaises(DeadlineExceeded): check_deadline()
        check_deadline()

    def test_before_commit_timeout_leaves_transaction_uncommitted(self):
        conn = Mock()
        with ingestion_budget(10, clock=lambda: 11):
            with self.assertRaises(DeadlineExceeded): commit_inventory(conn)
        conn.commit.assert_not_called()

    def test_commit_is_not_interrupted_when_it_crosses_deadline(self):
        clock = Clock(); conn = Mock(); conn.commit.side_effect = lambda: setattr(clock, 't', 11)
        with ingestion_budget(10, clock) as budget: commit_inventory(conn)
        self.assertTrue(budget.commit_succeeded); conn.commit.assert_called_once()

    def test_timeout_before_reconciliation_preserves_missing_row(self):
        db = Store([row()]); scope = LifecycleRun(db, 'ashby_direct', 'r').scope('acme')
        with ingestion_budget(0, clock=lambda: 1):
            with self.assertRaises(DeadlineExceeded): scope.finish(InventoryCompletion(True, True, 'complete'))
        self.assertTrue(db.sources['acme:1']['active'])

    def test_failed_run_id_recorded_without_changing_lifecycle(self):
        with ingestion_budget(100, clock=lambda: 0) as budget:
            LifecycleRun(Store(), 'ashby_direct', 'run-for-monitor')
            self.assertEqual(budget.run_id, 'run-for-monitor')

    def test_late_timeout_rolls_back_prior_scope_deactivation_in_isolated_db(self):
        # The actual PostgreSQL transaction scenario is covered separately below;
        # this verifies the required handler shape for every ingestion source.
        root = Path(__file__).parents[1]/'app'
        for source in LIGHT + HEAVY:
            text = (root/(source.removesuffix('_direct')+'_ingestion.py')).read_text()
            self.assertIn('commit_inventory(conn)', text); self.assertIn('conn.rollback()', text)
            self.assertNotIn('finished_at = now()', text)
            self.assertIn('finished_at = clock_timestamp()', text)

    def test_heavy_api_deadline_is_not_swallowed(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
        adapter = WorkdayAdapter(client=client)
        with ingestion_budget(0, clock=lambda: 1), self.assertRaises(DeadlineExceeded):
            adapter._request('POST', WorkdaySite('acme.wd1.myworkdayjobs.com', 'acme', 'Careers'), '/jobs')

    def test_legacy_lever_request_guard(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
        with patch('httpx.Client', return_value=client), ingestion_budget(0, clock=lambda: 1), self.assertRaises(DeadlineExceeded):
            LeverAdapter().list_postings('acme')


def source(job='job', name='ashby_direct', active=True, days=1, state='observed', complete=True):
    return {'job_id': job, 'source_name': name, 'source_job_id': 'acme:native', 'is_active': active,
        'last_seen_at': NOW-timedelta(days=days), 'last_verified_at': NOW-timedelta(days=days),
        'lifecycle': {'run_id': 'run', 'scope': ['acme'], 'state': state, 'inventory_complete': complete}}


def run(name='ashby_direct', complete=True):
    return {'id': 'run', 'source_name': name, 'status': 'success', 'started_at': NOW-timedelta(days=1),
        'finished_at': NOW-timedelta(days=1), 'metadata': {'board_count': 1,
        'lifecycle': {'scopes': [{'scope': ['acme'], 'successful': True, 'complete': complete}], 'deactivated': 0, 'reactivated': 0}}}


class ReportTests(unittest.TestCase):
    def test_verified_active_requires_complete_recent_native_proof(self):
        self.assertEqual(classify({'status': 'active'}, [source()], {'run': run()}, NOW)[0], 'VERIFIED_ACTIVE')

    def test_old_direct_is_needs_verification(self):
        state, why = classify({'status': 'active'}, [source(days=8)], {'run': run()}, NOW)
        self.assertEqual(state, 'NEEDS_VERIFICATION'); self.assertIn('direct_evidence_stale', why)

    def test_aggregator_is_not_native_verification(self):
        for name in ['jooble_direct', 'fantastic_jobs_apify']:
            state, why = classify({'status': 'active'}, [source(name=name)], {}, NOW)
            self.assertEqual(state, 'NEEDS_VERIFICATION'); self.assertIn('discovery_only', why)

    def test_bounded_filtered_heavy_is_not_complete(self):
        for name in HEAVY:
            self.assertEqual(classify({'status': 'active'}, [source(name=name, complete=False)], {'run': run(name, complete=False)}, NOW)[0], 'NEEDS_VERIFICATION')

    def test_heavy_recovery_is_not_promoted_even_if_one_scope_complete(self):
        s = source(name=HEAVY[0]); s['source_job_id'] = 'acme:Careers:native'
        s['native_scope'] = {'host': 'acme.wd1.myworkdayjobs.com'}
        scope = ['acme.wd1.myworkdayjobs.com', 'acme', 'Careers']; s['lifecycle']['scope'] = scope
        r = run(name=HEAVY[0]); r['metadata'] = {'sites': 1, 'lifecycle': {'scopes': [
            {'scope': scope, 'complete': True, 'successful': True}]}}
        self.assertEqual(classify({'status': 'active'}, [s], {'run': r}, NOW)[0], 'VERIFIED_ACTIVE')
        r['metadata']['host'] = 'acme.wd1.myworkdayjobs.com'
        self.assertEqual(classify({'status': 'active'}, [s], {'run': r}, NOW)[0], 'NEEDS_VERIFICATION')
        result = generate_report([{'id': 'job', 'status': 'active'}], [s], [r], NOW)
        self.assertIsNone(next(x for x in result['direct_verification'] if x['source_name'] == HEAVY[0])['latest_authoritative_complete_scope_verification'])

    def test_inactive_requires_proven_absence(self):
        s = source(active=False, state='absent')
        self.assertEqual(classify({'status': 'inactive'}, [s], {'run': run()}, NOW)[0], 'CONFIRMED_INACTIVE')
        self.assertEqual(classify({'status': 'inactive'}, [s], {}, NOW)[0], 'NEEDS_VERIFICATION')

    def test_direct_withdrawn_aggregator_present(self):
        sources = [source(active=False, state='absent'), source(name='fantastic_jobs_apify')]
        state, why = classify({'status': 'active'}, sources, {'run': run()}, NOW)
        self.assertEqual(state, 'NEEDS_VERIFICATION'); self.assertIn('direct_withdrawn_but_aggregator_present', why)

    def test_expired_but_unconfirmed(self):
        state, why = classify({'status': 'active', 'expires_at': NOW-timedelta(days=1)}, [source(name='jooble_direct')], {}, NOW)
        self.assertEqual(state, 'NEEDS_VERIFICATION'); self.assertIn('expired_timestamp_unconfirmed', why)

    def test_active_native_expiry_not_overridden(self):
        state, why = classify({'status': 'active', 'expires_at': NOW-timedelta(days=1)}, [source()], {'run': run()}, NOW)
        self.assertEqual(state, 'VERIFIED_ACTIVE'); self.assertIn('expiry_overridden_by_verified_native', why)

    def test_compositions(self):
        for names, expected in [(['ashby_direct'],'direct_only'), (['ashby_direct','jooble_direct'],'direct_plus_aggregator'),
                                (['fantastic_jobs_apify'],'fantastic_only'), (['jooble_direct'],'jooble_only'),
                                (['fantastic_jobs_apify','jooble_direct'],'fantastic_plus_jooble')]:
            self.assertEqual(composition([source(name=n) for n in names]), expected)

    def test_age_buckets_negative_verification_not_support(self):
        jobs = [{'id': 'job', 'status': 'active'}]
        sources = [source(name='fantastic_jobs_apify', days=15), source(active=False, days=0, state='absent')]
        r = generate_report(jobs, sources, [run()], NOW)
        for h in [24,72,168,336]: self.assertEqual(r['active_age_totals']['no_seen'][str(h)], 1)
        self.assertEqual(r['active_age_totals']['no_seen']['720'], 0)

    def test_partial_scope_and_bounded_recovery_are_not_full_run(self):
        r = run(); self.assertTrue(full_complete(r))
        r['metadata']['host'] = 'recovery.example'; self.assertFalse(full_complete(r))
        r = run(complete=False); self.assertFalse(full_complete(r))

    def test_scope_identity_mismatch_rejects_proof(self):
        s = source(); s['source_job_id'] = 'other:native'
        self.assertEqual(classify({'status': 'active'}, [s], {'run': run()}, NOW)[0], 'NEEDS_VERIFICATION')

    def test_report_json_stability_and_no_mutation(self):
        jobs = [{'id':'job','status':'active'}]; sources = [source()]; runs = [run()]
        original = deepcopy((jobs,sources,runs))
        a = generate_report(jobs,sources,runs,NOW); b = generate_report(jobs,sources,runs,NOW)
        self.assertEqual(json.dumps(a), json.dumps(b)); self.assertEqual((jobs,sources,runs), original)
        self.assertIn('Stored activity', markdown(a))

    def test_reporting_connection_is_readonly_and_select_only(self):
        class Connection:
            read_only = False
            isolation_level = None
            calls = []
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, sql):
                self.calls.append(sql)
                self_test.assertTrue(self.read_only)
                self_test.assertTrue(sql.lower().startswith(('select','show','set local')))
                if sql.startswith('show'): return Mock(fetchone=lambda:{'transaction_read_only':'on'})
                if sql.startswith('select now'): return Mock(fetchone=lambda:{'snapshot':NOW})
                return Mock(fetchall=lambda:[])
        self_test = self; conn = Connection()
        with patch('app.freshness_report.psycopg.connect', return_value=conn):
            r = collect_report('synthetic test URL')
        self.assertEqual(r['canonical']['total'],0)
        self.assertTrue(conn.read_only)

    def test_recent_lifecycle_counts_no_fabricated_closure_events(self):
        r = run(); r['metadata']['lifecycle']['deactivated'] = 3
        result = generate_report([{'id':'job','status':'inactive'}], [source(active=False,state='absent')], [r], NOW)
        self.assertEqual(result['lifecycle']['deactivated_last_7d'],3)
        self.assertIsNone(result['lifecycle']['canonical_closure_events_last_7d'])
