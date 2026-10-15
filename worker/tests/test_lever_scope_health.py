"""Exact native-case, guarded pagination and reviewed-gap regressions; no network."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from app import lever_ingestion as lever
from app.adapters.lever import LeverAdapter, LeverHTTPError, LeverPaginationError, native_site_slug, source_job_id
from app.direct_freshness import LIGHT, aggregate_status, load_config, next_baseline, normalize_outcome, run_group
from app.freshness_report import classify, full_complete, generate_report, markdown
from app.lever_scope_health import (accepted_gaps, failure_health, load_policy, probe_scope,
                                    scope_decision, scope_health_report)
from app.lifecycle import InventoryCompletion, LifecycleRun, canonical_activity
from test_freshness import outcome
from test_lever import LeverCursor, fantastic_row, payload, POSTING
from test_lifecycle import Store, row
from test_stabilization import OfflineTest

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def reviewed(state='KNOWN_UNAVAILABLE', site='gone'):
    return {'source_name': 'lever_direct', 'scope': ['global', site], 'state': state,
            'reason': 'Reviewed diagnostic evidence', 'reviewed_at': (NOW-timedelta(days=1)).isoformat(),
            'review_expires_at': (NOW+timedelta(days=30)).isoformat(), 'recheck_hours': 168}


def gap_health(policy=None, status=404):
    policy = policy or reviewed()
    decision = scope_decision(policy['scope'], [], NOW, [policy])
    return probe_scope(decision, Mock(probe=Mock(return_value=status)), policy['scope'][1], 'global', NOW)


def gap_outcome(health=None):
    health = health or gap_health()
    value = outcome('lever_direct')
    value['sites'] = 2
    value['lifecycle']['scopes'][0]['scope'] = ['global', 'acme']
    value['lifecycle']['scopes'].append({'scope': health['scope'], 'successful': False, 'complete': False,
                                       'observed': 0, 'deactivated': 0, 'reactivated': 0})
    value['scope_health'] = [health]
    return value


class NativeCaseAndPaginationTests(unittest.TestCase):
    def client(self, pages):
        manager = patch('app.adapters.lever.httpx.Client')
        self.addCleanup(manager.stop)
        client = manager.start().return_value.__enter__.return_value
        client.get.side_effect = [httpx.Response(status, json=data) for status, data in pages]
        return client

    def test_fliff_404_is_not_empty_inventory(self):
        self.client([(404, {'ok': False, 'error': 'Document not found'})])
        with self.assertRaises(LeverHTTPError) as error: LeverAdapter().list_postings('fliff')
        self.assertEqual(error.exception.status_code, 404)

    def test_advertised_native_case_is_preserved(self):
        client = self.client([(200, [payload(site='Fliff')])])
        self.assertEqual(len(LeverAdapter().list_postings('Fliff')), 1)
        self.assertTrue(client.get.call_args.args[0].endswith('/Fliff'))

    def test_discovery_retains_case_without_changing_identity(self):
        site = lever.discover_sites(LeverCursor([fantastic_row(site='Fliff')]))[0]
        self.assertEqual((site.site, site.api_site), ('fliff', 'Fliff'))
        self.assertEqual(source_job_id('global', 'Fliff', POSTING), source_job_id('global', 'fliff', POSTING))

    def test_mixed_case_evidence_is_ambiguous_not_guessed(self):
        site = lever.discover_sites(LeverCursor([fantastic_row(site='Fliff'), fantastic_row(site='fliff')]))[0]
        self.assertTrue(site.ambiguous_native_case)

    def test_explicit_case_and_url_validation(self):
        site = lever.select_sites([], 'Fliff', 'global', None)[0]
        self.assertEqual((site.site, site.api_site), ('fliff', 'Fliff'))
        self.assertEqual(native_site_slug(f'https://jobs.lever.co/Fliff/{POSTING}/apply'), 'Fliff')
        self.assertIsNone(native_site_slug('https://jobs.lever.co.evil.test/Fliff'))

    def test_real_jobgether_pattern_rejects_one_overlap_with_new_records(self):
        client = self.client([(200, [{'id':'a'},{'id':'b'}]), (200,[{'id':'b'},{'id':'c'}])])
        adapter=LeverAdapter();adapter.PAGE_SIZE=2
        with self.assertRaises(LeverPaginationError) as error: adapter.list_postings('jobgether')
        self.assertEqual(error.exception.diagnostics, {'reason':'cross_page_overlap','skip':2,'returned_rows':2,'overlap_count':1})
        self.assertEqual(client.get.call_count,2)

    def test_repeated_page_fails_without_further_requests(self):
        client=self.client([(200,[{'id':'a'}]),(200,[{'id':'a'}])])
        adapter=LeverAdapter();adapter.PAGE_SIZE=1
        with self.assertRaisesRegex(LeverPaginationError,'repeated_page'):adapter.list_postings('acme')
        self.assertEqual(client.get.call_count,2)

    def test_identical_duplicate_slots_dedup_but_advance_raw_offset(self):
        client=self.client([(200,[{'id':'a'},{'id':'a'}]),(200,[{'id':'b'}])])
        adapter=LeverAdapter();adapter.PAGE_SIZE=2
        self.assertEqual(adapter.list_postings('acme'),[{'id':'a'},{'id':'b'}])
        self.assertEqual(client.get.call_args.kwargs['params']['skip'],2)

    def test_conflicting_duplicate_fails(self):
        self.client([(200,[{'id':'a','text':'one'},{'id':'a','text':'two'}])])
        with self.assertRaisesRegex(LeverPaginationError,'conflicting_duplicate'):LeverAdapter().list_postings('acme')

    def test_missing_identity_fails(self):
        self.client([(200,[{'text':'missing id'}])])
        with self.assertRaisesRegex(LeverPaginationError,'missing_identity'):LeverAdapter().list_postings('acme')

    def test_safe_terminal_empty_page(self):
        client=self.client([(200,[{'id':'a'}]),(200,[])])
        adapter=LeverAdapter();adapter.PAGE_SIZE=1
        self.assertEqual(adapter.list_postings('acme'),[{'id':'a'}])
        self.assertEqual(client.get.call_args.kwargs['params']['skip'],1)

    def test_cursor_object_is_not_a_supported_complete_inventory(self):
        self.client([(200,{'data':[],'next':'repeated-cursor'})])
        with self.assertRaisesRegex(RuntimeError,'Unexpected'):LeverAdapter().list_postings('acme')

    def test_hard_page_limit_remains(self):
        client=self.client([(200,[{'id':str(n)}]) for n in range(1000)])
        adapter=LeverAdapter();adapter.PAGE_SIZE=1
        with self.assertRaisesRegex(RuntimeError,'safety limit'):adapter.list_postings('acme')
        self.assertEqual(client.get.call_count,1000)

    def test_probe_is_one_record_and_preserves_case(self):
        client=self.client([(200,[])])
        self.assertEqual(LeverAdapter().probe('Fliff'),200)
        self.assertEqual(client.get.call_args.kwargs['params'],{'mode':'json','limit':1,'skip':0})

    def test_probe_invalid_json_is_not_healthy(self):
        self.client([(200,{'not':'list'})])
        with self.assertRaises(RuntimeError):LeverAdapter().probe('acme')


class PolicyTests(unittest.TestCase):
    def test_current_config_only_reviews_jobgether(self):
        policy=load_policy()
        self.assertEqual([(r['scope'],r['state']) for r in policy],[(['global','jobgether'],'NEEDS_REVIEW')])

    def test_unknown_scope_has_no_automatic_exclusion(self):
        self.assertIsNone(scope_decision(['global','fliff'],[],NOW,[]))

    def test_known_unavailable_expected_404(self):
        health=gap_health()
        self.assertTrue(health['expected_gap']);self.assertFalse(health['alert_worthy'])
        self.assertEqual(health['probe_http_status'],404)

    def test_known_scope_reappearance_requires_review(self):
        health=gap_health(status=200)
        self.assertEqual(health['state'],'NEEDS_REVIEW')
        self.assertTrue(health['alert_worthy']);self.assertFalse(health['expected_gap'])

    def test_needs_review_successful_probe_is_not_reenable(self):
        health=gap_health(reviewed('NEEDS_REVIEW'),200)
        self.assertEqual(health['state'],'NEEDS_REVIEW');self.assertTrue(health['coverage_gap'])

    def test_unexpected_probe_status_alerts(self):
        self.assertTrue(gap_health(status=503)['alert_worthy'])

    def test_probe_failure_retains_quarantine(self):
        decision=scope_decision(['global','gone'],[],NOW,[reviewed()])
        result=probe_scope(decision,Mock(probe=Mock(side_effect=TimeoutError())), 'gone','global',NOW)
        self.assertEqual(result['state'],'TRANSIENT_FAILURE');self.assertTrue(result['coverage_gap'])

    def test_weekly_cadence_skips_without_http(self):
        prior=gap_health();decision=scope_decision(['global','gone'],[prior],NOW+timedelta(days=1),[reviewed()])
        adapter=Mock()
        self.assertEqual(probe_scope(decision,adapter,'gone','global',NOW)['action'],'skip')
        adapter.probe.assert_not_called()

    def test_unexpected_failure_reprobes_next_run_not_hidden_for_week(self):
        decision=scope_decision(['global','gone'],[gap_health(status=503)],NOW+timedelta(days=1),[reviewed()])
        self.assertEqual(decision['action'],'probe')

    def test_review_expiry_stays_quarantined_and_alerts(self):
        result=scope_decision(['global','gone'],[gap_health()],NOW+timedelta(days=31),[reviewed()])
        self.assertTrue(result['alert_worthy']);self.assertFalse(result['expected_gap'])
        self.assertIn(result['action'],{'probe','skip'})

    def test_unexpected_404_is_review_not_known_gap(self):
        result=failure_health(['global','other'],LeverHTTPError(404))
        self.assertEqual(result['state'],'NEEDS_REVIEW');self.assertFalse(result['expected_gap'])

    def test_http_503_is_transient(self):
        self.assertEqual(failure_health(['global','other'],LeverHTTPError(503))['state'],'TRANSIENT_FAILURE')

    def test_bad_config_rejected_before_execution(self):
        for change in [{'source_name':'jooble_direct'},{'scope':['global','bad/path']},{'recheck_hours':True}, {'review_expires_at':'2000-01-01T00:00:00Z'}]:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'policy.json';path.write_text(json.dumps({'version':1,'scopes':[reviewed()|change]}))
                with self.assertRaises(ValueError):load_policy(path)

    def test_gap_never_authorizes_lifecycle_or_reactivation(self):
        db=Store([row('global:gone:posting',source='lever_direct')])
        inventory=LifecycleRun(db,'lever_direct','run').scope('global','gone')
        value=inventory.finish(InventoryCompletion(False,False,'reviewed unavailable'))
        self.assertEqual(value['deactivated'],0)
        self.assertEqual(db.sources['global:gone:posting']['last_verified'],'original')
        self.assertEqual(db.jobs['global:gone:posting'],'active')


class OutcomePolicyTests(unittest.TestCase):
    def normalize(self,value,policy=None,previous=None):
        with patch('app.lever_scope_health.load_policy',return_value=policy if policy is not None else [reviewed()]):
            return normalize_outcome('lever_direct',value,previous,now=NOW)

    def test_reviewed_gap_is_explicit_success_with_known_gaps(self):
        result=self.normalize(gap_outcome())
        self.assertEqual(result['status'],'SUCCESS_WITH_KNOWN_GAPS')
        self.assertTrue(result['coverage_degraded']);self.assertFalse(result['alert_worthy'])
        self.assertEqual((result['scopes_complete'],result['scopes_failed'],result['scopes_known_gaps']),(1,1,1))

    def test_unreviewed_failure_remains_partial(self):
        self.assertEqual(self.normalize(gap_outcome(),[])['status'],'PARTIAL')

    def test_another_unexpected_error_does_not_hide_behind_gap(self):
        value=gap_outcome();value['site_errors']=[{'site':'other','error':'unexpected'}]
        self.assertEqual(self.normalize(value)['status'],'PARTIAL')

    def test_reappearance_cannot_be_known_success(self):
        self.assertEqual(self.normalize(gap_outcome(gap_health(status=200)))['status'],'PARTIAL')

    def test_unsafe_deactivation_rejected_even_if_reviewed(self):
        value=gap_outcome();value['lifecycle']['scopes'][1]['deactivated']=1
        with self.assertRaises(ValueError):self.normalize(value)

    def test_unchecked_probe_cannot_excuse_failure(self):
        health=gap_health();del health['probed_at']
        self.assertEqual(self.normalize(gap_outcome(health))['status'],'PARTIAL')

    def test_expired_review_cannot_excuse_failure(self):
        config=reviewed();config['review_expires_at']=(NOW-timedelta(hours=1)).isoformat()
        self.assertEqual(self.normalize(gap_outcome(),[config])['status'],'PARTIAL')

    def test_forged_future_next_probe_does_not_extend_configured_cadence(self):
        health=gap_health();health.update(action='skip',last_probe_at=(NOW-timedelta(days=8)).isoformat(),next_probe_at=(NOW+timedelta(days=100)).isoformat())
        self.assertEqual(self.normalize(gap_outcome(health))['status'],'PARTIAL')

    def test_probe_before_review_is_not_current_evidence(self):
        health=gap_health();health['probed_at']=(NOW-timedelta(days=2)).isoformat()
        self.assertEqual(self.normalize(gap_outcome(health))['status'],'PARTIAL')

    def test_group_known_gap_still_exit_zero_and_degraded(self):
        def execute(source,request):return gap_outcome() if source=='lever_direct' else outcome(source)
        with tempfile.TemporaryDirectory() as directory,patch('app.lever_scope_health.load_policy',return_value=[reviewed()]):
            result=run_group('light',load_config(),execute=execute,now=lambda:NOW,lock_path=Path(directory)/'lock')
        self.assertEqual(result['status'],'SUCCESS_WITH_KNOWN_GAPS');self.assertEqual(result['exit_code'],0)
        self.assertTrue(result['coverage_degraded']);self.assertFalse(result['alert_worthy'])

    def test_group_aggregation_mixed_failure(self):
        self.assertEqual(aggregate_status([{'status':'SUCCESS_WITH_KNOWN_GAPS'},{'status':'FAILED'}]),'PARTIAL')

    def test_volume_comparison_only_between_same_coverage(self):
        value=gap_outcome();value['fetched']=1
        different=self.normalize(value,previous={'fetched':1000})
        self.assertEqual(different['status'],'SUCCESS_WITH_KNOWN_GAPS')
        self.assertEqual(different['volume_comparison'],'not_comparable_coverage_changed')
        comparable=self.normalize(value,previous={'fetched':1000,'known_gap_scopes':[['global','gone']]})
        self.assertEqual(comparable['status'],'PARTIAL')
        self.assertIn('unexpected_result_collapse',comparable['anomalies'])

    def test_known_gap_baseline_remains_coverage_specific(self):
        value=self.normalize(gap_outcome());value['request']={}
        result=next_baseline(None,{'runner_type':'light','sources':[value]})
        self.assertEqual(result['sources'][0]['known_gap_scopes'],[['global','gone']])

    def test_duplicate_health_diagnostics_rejected(self):
        value=gap_outcome();value['scope_health']*=2
        with self.assertRaises(ValueError):self.normalize(value)


class ScopeReportingTests(unittest.TestCase):
    def history(self,complete=True,health=None):
        scope={'scope':['global','gone'],'successful':complete,'complete':complete,'deactivated':0,'reactivated':0}
        meta={'site_count':1,'lifecycle':{'scopes':[scope]}}
        if health:meta['scope_health']=[health]
        return {'id':'run','source_name':'lever_direct','status':'success','started_at':NOW.isoformat(),
                'finished_at':NOW.isoformat(),'metadata':meta,'records_fetched':1,'records_failed':0}

    def test_last_complete_preserved_across_gap_and_skip_not_counted_failure(self):
        first=self.history();first['started_at']=(NOW-timedelta(days=2)).isoformat()
        report=scope_health_report([first,self.history(False,gap_health())],NOW,[reviewed()])[0]
        self.assertEqual(report['last_complete_inventory']['run_id'],'run')
        self.assertEqual(report['consecutive_failed_inventories'],0)
        self.assertEqual(report['state'],'KNOWN_UNAVAILABLE')

    def test_unexpected_failure_history_counts_and_is_visible(self):
        run=self.history(False);run['metadata']['site_errors']=[{'instance':'global','site':'gone','error':'Lever postings error 404'}]
        report=scope_health_report([run],NOW,[])[0]
        self.assertEqual(report['consecutive_failed_inventories'],1)
        self.assertEqual(report['state'],'NEEDS_REVIEW')

    def test_reappearance_not_hidden_by_configured_unavailability(self):
        report=scope_health_report([self.history(False,gap_health(status=200))],NOW,[reviewed()])[0]
        self.assertEqual(report['state'],'NEEDS_REVIEW');self.assertTrue(report['alert_worthy'])

    def test_reviewed_gap_prevents_reporting_old_scope_as_verified_active(self):
        run=self.history()
        source={'job_id':'job','source_name':'lever_direct','source_job_id':'global:gone:posting','is_active':True,
                'last_seen_at':NOW,'last_verified_at':NOW,'lifecycle':{'scope':['global','gone'],'run_id':'run','inventory_complete':True,'state':'observed'}}
        state,reasons=classify({'status':'active'},[source],{'run':run},NOW,reviewed_gaps={('global','gone')})
        self.assertEqual(state,'NEEDS_VERIFICATION');self.assertIn('reviewed_native_scope_coverage_gap',reasons)

    def test_complete_native_reporting_without_gap_remains_verified(self):
        run=self.history();source={'job_id':'job','source_name':'lever_direct','source_job_id':'global:gone:posting','is_active':True,'last_seen_at':NOW,'last_verified_at':NOW,'lifecycle':{'scope':['global','gone'],'run_id':'run','inventory_complete':True,'state':'observed'}}
        self.assertEqual(classify({'status':'active'},[source],{'run':run},NOW,reviewed_gaps=set())[0],'VERIFIED_ACTIVE')

    def test_known_gap_is_never_full_population_complete(self):
        self.assertFalse(full_complete(self.history(False,gap_health())))

    def test_json_and_markdown_include_scope_health_without_input_mutation(self):
        runs=[self.history(False,gap_health())];original=deepcopy(runs)
        with patch('app.lever_scope_health.load_policy',return_value=[reviewed()]):
            report=generate_report([],[],runs,NOW)
        self.assertEqual(report['scope_health']['lever_direct'][0]['state'],'KNOWN_UNAVAILABLE')
        self.assertIn('Lever scope health',markdown(report));self.assertEqual(runs,original)

    def test_canonical_activity_semantics_unchanged(self):
        self.assertEqual(canonical_activity([('lever_direct',False),('fantastic_jobs_apify',True)]),'active')


class HistoryCursor(LeverCursor):
    def execute(self,query,params=()):
        if isinstance(query,str) and query.strip().startswith("select metadata->'scope_health'"):
            self.calls.append((query,params));self.rows=[]
        else:super().execute(query,params)


class IngestionPolicyTests(OfflineTest):
    def execute(self,site,policy,probe=None):
        cursor=HistoryCursor();self.fake_database(cursor)
        with patch.object(lever,'discover_sites',return_value=[site]),patch.object(lever,'load_policy',return_value=policy),patch('app.lever_ingestion.datetime',wraps=datetime) as clock:
            clock.now.return_value=NOW
            with patch.object(LeverAdapter,'probe',return_value=probe) as probe_call,patch.object(LeverAdapter,'list_postings',return_value=[]) as inventory:
                result=lever.ingest_lever_jobs()
        return result,cursor,probe_call,inventory

    def test_reviewed_unavailable_probe_never_ingests_or_reconciles(self):
        result,cur,probe,inventory=self.execute(lever.LeverSite('global','gone','Gone'),[reviewed()],404)
        inventory.assert_not_called();probe.assert_called_once()
        self.assertEqual(result['lifecycle']['deactivated'],0);self.assertFalse(cur.source_writes)
        self.assertEqual(result['lifecycle']['scopes'][0]['complete'],False)
        self.assertEqual(result['status'],'success')

    def test_reappearance_does_not_process_postings(self):
        result,cur,probe,inventory=self.execute(lever.LeverSite('global','gone','Gone'),[reviewed()],200)
        inventory.assert_not_called();self.assertTrue(result['scope_health'][0]['alert_worthy'])
        self.assertEqual(result['status'],'failed');self.assertFalse(cur.source_writes)

    def test_fliff_ingestion_passes_native_case_to_adapter(self):
        result,cur,probe,inventory=self.execute(lever.LeverSite('global','fliff','Fliff','Fliff'),[])
        inventory.assert_called_once_with('Fliff',instance='global');probe.assert_not_called()
        self.assertTrue(result['lifecycle']['scopes'][0]['complete'])

    def test_ambiguous_native_case_never_guesses_transport(self):
        result,cur,probe,inventory=self.execute(lever.LeverSite('global','fliff','Fliff',None,True),[])
        inventory.assert_not_called();probe.assert_not_called()
        self.assertEqual(result['status'],'failed');self.assertEqual(result['scope_health'][0]['state'],'NEEDS_REVIEW')
