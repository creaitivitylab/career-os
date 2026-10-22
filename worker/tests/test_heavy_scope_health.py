"""Offline HEAVY policy and record identity safety; never production ingestion."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from app.heavy_scope_health import (classify_scope, coverage_signature, evaluate_source,
                                    health_report, load_policy)
from app.record_quality import RecordQuality, legacy_quality
from app.direct_freshness import normalize_outcome, aggregate_status, next_baseline
from app.adapters.workday import WorkdayAdapter, WorkdayAPIError, source_job_id as wd_id
from app.adapters.successfactors import RMKError, parse_detail, SuccessFactorsAdapter
from app.freshness_report import generate_report, markdown
from test_workday import BOARD, PATH, detail
from test_successfactors import native, config, ROOT
from test_lifecycle import Store, row, COMPLETE
from app.lifecycle import LifecycleRun, InventoryCompletion

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
SOURCE = 'workday_direct'


def scope(complete=False, status='success'):
    return {'host': BOARD.host, 'tenant': BOARD.tenant, 'site': BOARD.site,
            'discovery_mode': 'fallback' if complete else 'geography_facet',
            'status': status, 'listing_records': 2, 'failed': 0,
            'lifecycle': {'scope': list(BOARD.scope), 'complete': complete,
                          'successful': status == 'success', 'deactivated': 0, 'reactivated': 0,
                          'reason': 'native complete' if complete else 'filtered geography'}}


def review(row, source=SOURCE, **changes):
    r = {'scope': [row['host'], row['tenant'], row['site']] if source == SOURCE else [row['host'], row.get('brand','')],
         'source_name': source, 'state': 'HEALTHY', 'reason': 'Reviewed filtered coverage',
         'reviewed_at': NOW.isoformat(), 'review_expires_at': (NOW+timedelta(days=30)).isoformat(),
         'evidence': 'offline test', 'coverage_signature': coverage_signature(source, row)}
    r.update(changes)
    return r


def policy(*reviews):
    return {'version': 1, 'scopes': list(reviews), 'discovery_backlog': []}


def result(*rows):
    return {'run_id': 'run', 'status': 'success', 'sites': len(rows),
            'listing_records': sum(r['listing_records'] for r in rows),
            'created': 0, 'updated': 0, 'attached': 0, 'failed': sum(r.get('failed',0) for r in rows),
            'site_results': list(rows), 'site_errors': [],
            'lifecycle': {'scopes': [r['lifecycle'] for r in rows if 'lifecycle' in r],
                          'deactivated': 0, 'reactivated': 0}}


class HeavyPolicyTests(unittest.TestCase):
    def test_clean_authoritative_success(self):
        d=evaluate_source(SOURCE,result(scope(True)),NOW,policy())
        self.assertEqual(d['status'],'SUCCESS')
        self.assertTrue(d['scopes'][0]['lifecycle_eligible'])

    def test_expected_non_authoritative_healthy_acquisition(self):
        r=scope();d=evaluate_source(SOURCE,result(r),NOW,policy(review(r)))
        self.assertEqual(d['status'],'SUCCESS_WITH_KNOWN_GAPS')
        self.assertEqual(d['scopes'][0]['acquisition_health'],'HEALTHY')
        self.assertEqual(d['scopes'][0]['coverage'],'NON_AUTHORITATIVE_EXPECTED')
        self.assertFalse(d['scopes'][0]['lifecycle_eligible'])

    def test_review_never_promotes_completeness(self):
        r=scope();original=deepcopy(r)
        classify_scope(SOURCE,r,NOW,policy(review(r)))
        self.assertEqual(r,original)
        self.assertFalse(r['lifecycle']['complete'])

    def test_unreviewed_filtered_coverage_partial(self):
        self.assertEqual(evaluate_source(SOURCE,result(scope()),NOW,policy())['status'],'PARTIAL')

    def test_expired_review_partial(self):
        r=scope();p=policy(review(r));d=evaluate_source(SOURCE,result(r),NOW+timedelta(days=31),p)
        self.assertEqual(d['status'],'PARTIAL')
        self.assertIn('review_expired_or_not_yet_effective',d['scopes'][0]['alerts'])

    def test_future_review_partial(self):
        r=scope();self.assertEqual(evaluate_source(SOURCE,result(r),NOW-timedelta(seconds=1),policy(review(r)))['status'],'PARTIAL')

    def test_unexpected_failure_partial(self):
        r=scope(status='failed');r['kind']='transient'
        d=evaluate_source(SOURCE,result(r),NOW,policy())
        self.assertEqual(d['status'],'PARTIAL')
        self.assertEqual(d['scopes'][0]['state'],'TRANSIENT_FAILURE')

    def test_recovered_transient_not_quarantined(self):
        r=scope();d=evaluate_source(SOURCE,result(r),NOW,policy(review(r)))
        self.assertEqual(d['scopes'][0]['state'],'HEALTHY')

    def test_reviewed_failure_known_gap(self):
        r=scope(status='failed');r.update(kind='access',error='reviewed unavailable')
        p=policy(review(r,state='KNOWN_UNAVAILABLE',expected_error={'kind':'access','error':'reviewed unavailable'}))
        d=evaluate_source(SOURCE,result(r),NOW,p)
        self.assertEqual(d['status'],'SUCCESS_WITH_KNOWN_GAPS')
        self.assertEqual(d['scopes'][0]['coverage'],'UNAVAILABLE')

    def test_known_unavailable_reappearance_alert(self):
        r=scope();p=policy(review(r,state='KNOWN_UNAVAILABLE',expected_error={'kind':'access','error':'403'}))
        self.assertEqual(evaluate_source(SOURCE,result(r),NOW,p)['status'],'PARTIAL')

    def test_changed_coverage_alert(self):
        r=scope();p=policy(review(r));r['fallback_skipped_foreign']=5
        self.assertEqual(evaluate_source(SOURCE,result(r),NOW,p)['status'],'PARTIAL')

    def test_new_discovery_backlog_alert(self):
        r={'host':'careers.example.com','brand':'','status':'success','listing_records':0,'failed':0}
        native=result(r);native['unapproved_discovered_scopes']=[{'host':'unknown.example.com','brand':''}]
        self.assertIn('new_or_expired_discovery_backlog',evaluate_source('successfactors_direct',native,NOW,policy())['alerts'])

    def test_reviewed_backlog_is_visible_not_authoritative(self):
        r=scope(True);native=result(r);native['unapproved_discovered_scopes']=[{'host':BOARD.host,'brand':''}]
        entry=review({'host':BOARD.host},'successfactors_direct',category='unapproved_discovered_scopes')
        p=policy();p['discovery_backlog']=[entry]
        d=evaluate_source('successfactors_direct',native,NOW,p)
        self.assertFalse(d['discovery_backlog'][0]['alert_worthy'])
        self.assertFalse(d['discovery_backlog'][0]['lifecycle_eligible'])

    def test_expired_backlog_alerts(self):
        native=result(scope(True));native['unapproved_discovered_scopes']=[{'host':BOARD.host,'brand':''}]
        p=policy();p['discovery_backlog']=[review({'host':BOARD.host},'successfactors_direct',category='unapproved_discovered_scopes')]
        self.assertIn('new_or_expired_discovery_backlog',evaluate_source('successfactors_direct',native,NOW+timedelta(days=31),p)['alerts'])

    def test_full_failure_stays_failed(self):
        native=result(scope(True));native['status']='failed'
        self.assertEqual(evaluate_source(SOURCE,native,NOW,policy())['status'],'FAILED')

    def test_conflicting_scope_flags_rejected(self):
        native=result(scope(True));native['lifecycle']=deepcopy(native['lifecycle'])
        native['lifecycle']['scopes'][0]['complete']=False
        with self.assertRaisesRegex(ValueError,'Conflicting'):
            evaluate_source(SOURCE,native,NOW,policy())

    def test_missing_scope_results_alert(self):
        native=result(scope(True));native['sites']=2
        self.assertEqual(evaluate_source(SOURCE,native,NOW,policy())['status'],'PARTIAL')

    def test_review_config_bounded(self):
        p=load_policy();self.assertGreater(len(p['scopes']),200)
        self.assertEqual(len(p['discovery_backlog']),30)

    def test_review_config_rejects_permanent_suppression(self):
        r=scope();p=policy(review(r,review_expires_at='2030-01-01T00:00:00+00:00'))
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'policy.json';f.write_text(json.dumps(p))
            with self.assertRaises(ValueError):load_policy(f)

    def test_four_rmk_exceptions_explicit_and_expire(self):
        p=load_policy();rows=[r for r in p['scopes'] if r['state']=='NEEDS_REVIEW']
        self.assertEqual({r['scope'][0] for r in rows},{'careers.dachser.com','careers.gates.com','jobs.volkswagen-group.com','mann-hummel-careers.com'})
        for r in rows:self.assertEqual(r['review_expires_at'],'2026-11-19T00:00:00+00:00')

    def test_alternate_catalog_guard_exception_known_not_approved(self):
        p=load_policy()
        for review_row in p['scopes']:
            if 'expected_error' not in review_row or review_row['scope'][0]=='mann-hummel-careers.com':continue
            r={'host':review_row['scope'][0],'brand':review_row['scope'][1],'status':'unresolved',**review_row['expected_error']}
            d=classify_scope('successfactors_direct',r,NOW,p)
            self.assertTrue(d['known_gap']);self.assertFalse(d['lifecycle_eligible'])

    def test_mann_exact_blocked_transition_only(self):
        p=load_policy();review_row=next(r for r in p['scopes'] if r['scope'][0]=='mann-hummel-careers.com')
        r={'host':review_row['scope'][0],'brand':'','status':'failed',**deepcopy(review_row['expected_error'])}
        self.assertTrue(classify_scope('successfactors_direct',r,NOW,p)['known_gap'])
        r['blocked_transition']['to_host']='other.example.com'
        self.assertTrue(classify_scope('successfactors_direct',r,NOW,p)['alert_worthy'])

    def test_nord_anglia_is_not_known_unavailable(self):
        p=load_policy();r=next(r for r in p['scopes'] if r['scope'][0]=='careers.nordanglia.com')
        self.assertEqual(r['state'],'HEALTHY');self.assertNotIn('expected_error',r)

    def test_kaufland_brand_view_never_promoted(self):
        p=load_policy();reviews=[r for r in p['scopes'] if 'kaufland' in r['scope'][0]]
        self.assertTrue(reviews)
        for r in reviews:self.assertEqual(r['state'],'HEALTHY')
        # Shared native posting identity does not make a brand view a complete inventory.
        self.assertTrue(all('coverage_signature' in r for r in reviews))

    def test_runner_known_gap_semantics(self):
        r=scope();native=result(r)
        with patch('app.heavy_scope_health.load_policy',return_value=policy(review(r))):
            d=normalize_outcome(SOURCE,native,now=NOW)
        self.assertEqual(d['status'],'SUCCESS_WITH_KNOWN_GAPS')
        self.assertEqual(aggregate_status([d,{'status':'SUCCESS'}]),'SUCCESS_WITH_KNOWN_GAPS')

    def test_runner_unknown_records_still_partial(self):
        r=scope();r.update(failed=2,listing_missing_path=2);native=result(r)
        with patch('app.heavy_scope_health.load_policy',return_value=policy(review(r))):
            self.assertEqual(normalize_outcome(SOURCE,native,now=NOW)['status'],'PARTIAL')

    def test_freshness_report_has_independent_heavy_axes(self):
        r=scope();run={'id':'run','source_name':SOURCE,'status':'success','started_at':NOW,'metadata':result(r)}
        with patch('app.heavy_scope_health.load_policy',return_value=policy(review(r))):
            d=generate_report([],[],[run],NOW)
        self.assertIn(SOURCE,d['heavy_scope_health'])
        self.assertIn('HEAVY acquisition',markdown(d))
        self.assertEqual(d['derived_status_counts']['VERIFIED_ACTIVE'],0)


class RecordQualityTests(unittest.TestCase):
    def test_identity_preserving_optional_warning(self):
        q=RecordQuality();q.failure('optional_salary',identity='tenant:native',stage='enrichment')
        d=q.summary(1,1,usable=1)
        self.assertFalse(d['alert_worthy']);self.assertEqual(d['identity_preserving_failures'],1)

    def test_unknown_identity_blocks(self):
        q=RecordQuality();q.failure('missing_identity',stage='identity')
        self.assertTrue(q.summary(1,0,usable=0)['alert_worthy'])

    def test_no_usable_required_records_systemic(self):
        q=RecordQuality();q.failure('schema_missing',count=100)
        self.assertTrue(q.summary(100,0,usable=0)['systemic_failure'])

    def test_lossless_signature_counts_beyond_example_cap(self):
        q=RecordQuality()
        for i in range(43):q.failure('missing_identity')
        d=q.summary(43,0,usable=0)
        self.assertEqual(len(d['examples']),10);self.assertEqual(d['signatures'][0]['count'],43)

    def test_legacy_examples_do_not_prove_uncaptured_failures(self):
        d=legacy_quality({'status':'success','failed':22,'detail_errors':[{'kind':'malformed'}]*10})
        self.assertEqual(d['unclassified_failures'],22)
        self.assertTrue(d['alert_worthy'])

    def test_scope_acquisition_failure_not_record(self):
        self.assertEqual(legacy_quality({'status':'failed','failed':1})['parsing_enrichment_failures'],0)

    def test_unknown_quality_cannot_claim_complete(self):
        r=scope(True);q=RecordQuality();q.failure('missing_identity');r['record_quality']=q.summary(1,0)
        d=evaluate_source(SOURCE,result(r),NOW,policy())
        self.assertEqual(d['status'],'PARTIAL')
        self.assertFalse(d['scopes'][0]['lifecycle_eligible'])
        with patch('app.heavy_scope_health.load_policy',return_value=policy()):
            with self.assertRaisesRegex(ValueError,'Unsafe authoritative'):
                normalize_outcome(SOURCE,result(r),now=NOW)

    def test_serialized_alert_false_cannot_mask_unknown(self):
        r=scope();q=RecordQuality();q.failure('missing_identity')
        r['record_quality']={**q.summary(1,0),'alert_worthy':False}
        self.assertTrue(classify_scope(SOURCE,r,NOW,policy(review(r)))['alert_worthy'])

    def test_required_parser_failure_remains_alert(self):
        q=RecordQuality();q.failure('required_description',identity='tenant:native')
        self.assertTrue(q.summary(1,1,usable=1)['alert_worthy'])

    def test_proved_identity_protects_existing_source(self):
        # Existing lifecycle implementation, not a replacement model.
        store=Store([row('acme:id', source='greenhouse_direct', job='job')]);inv=LifecycleRun(store,'greenhouse_direct','run').scope('acme')
        q=RecordQuality();q.protect(inv,RMKError('malformed','missing required text',source_identity='acme:id'))
        decision=inv.finish(COMPLETE)
        self.assertEqual(decision['deactivated'],0)
        self.assertTrue(store.sources['acme:id']['active'])
        self.assertEqual(decision['observed'],1)

    def test_unknown_failure_cannot_become_seen(self):
        inv=Mock();q=RecordQuality();q.protect(inv,RMKError('missing_identity','unknown'))
        inv.observe.assert_not_called();self.assertEqual(q.unknown,1)

    def test_workday_missing_title_carries_proved_identity(self):
        data=detail();data['jobPostingInfo']['title']=''
        with WorkdayAdapter(client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=data)))) as a:
            with self.assertRaises(WorkdayAPIError) as raised:a.get_detail(BOARD,PATH)
        self.assertEqual(raised.exception.source_identity,wd_id(BOARD,data['jobPostingInfo']['id']))

    def test_workday_wrong_site_never_carries_identity(self):
        data=detail();data['jobPostingInfo'].update(title='',jobPostingSiteId='other')
        with WorkdayAdapter(client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=data)))) as a:
            with self.assertRaises(WorkdayAPIError) as raised:a.get_detail(BOARD,PATH)
        self.assertIsNone(raised.exception.source_identity)

    def test_rmk_missing_description_proved_identity(self):
        text=native().replace('Native description','')
        with self.assertRaises(RMKError) as raised:parse_detail(ROOT+'/job/Prague-Engineer/123/',text,config())
        self.assertEqual(raised.exception.source_identity,'ACME:123')

    def test_rmk_canonical_mismatch_never_proves_identity(self):
        text=native().replace('Native description','').replace('href="'+ROOT+'/job/Prague-Engineer/123/','href="'+ROOT+'/job/Prague-Engineer/999/')
        with self.assertRaises(RMKError) as raised:parse_detail(ROOT+'/job/Prague-Engineer/123/',text,config())
        self.assertIsNone(raised.exception.source_identity)

    def test_blocked_redirect_has_exact_host_diagnostic(self):
        transport=httpx.MockTransport(lambda req:httpx.Response(302,headers={'location':'https://blocked.example.com/search/'}))
        with SuccessFactorsAdapter(client=httpx.Client(transport=transport)) as a:
            with self.assertRaises(RMKError) as raised:a._request('GET',ROOT,{ 'careers.example.com' })
        self.assertEqual(raised.exception.blocked_transition,{'from_host':'careers.example.com','to_host':'blocked.example.com'})


class IngestionQualityTests(unittest.TestCase):
    def wd_harness(self):
        from test_workday import WorkdayIngestionTests
        harness = WorkdayIngestionTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        return harness

    def rmk_harness(self):
        from test_successfactors import IngestionTests
        harness = IngestionTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        return harness

    def test_workday_proved_failed_record_seen_and_non_authoritative(self):
        h=self.wd_harness();cur, conn, adapter=h.setup_ingestion()
        identity=wd_id(BOARD,detail()['jobPostingInfo']['id'])
        adapter.get_detail.side_effect=WorkdayAPIError('malformed','missing title',source_identity=identity)
        result=h.ingest();r=result['site_results'][0]
        self.assertEqual(r['record_quality']['identity_preserving_failures'],1)
        self.assertFalse(r['lifecycle']['complete'])
        self.assertEqual(r['lifecycle']['deactivated'],0)
        self.assertTrue(any(identity in str(args) for query,args in cur.calls if 'source_job_id = any' in query))

    def test_workday_unknown_record_downgrades_and_is_not_seen(self):
        h=self.wd_harness();cur, conn, adapter=h.setup_ingestion()
        adapter.get_detail.side_effect=WorkdayAPIError('missing_identity','no native id')
        r=h.ingest()['site_results'][0]
        self.assertEqual(r['record_quality']['identity_unknown_failures'],1)
        self.assertFalse(r['lifecycle']['complete'])
        self.assertEqual(r['lifecycle']['observed'],0)
        self.assertTrue(r['record_quality']['systemic_failure'])

    def test_rmk_required_field_diagnostic_keeps_proved_identity(self):
        from test_successfactors import RMKCursor,row as rmk_row
        h=self.rmk_harness();h.detail=h.detail.replace('Native description','')
        response,conn=h.request(RMKCursor([rmk_row()]))
        r=response.json()['site_results'][0]
        self.assertEqual(r['record_quality']['identity_preserving_failures'],1)
        self.assertEqual(r['record_quality']['identity_unknown_failures'],0)
        self.assertFalse(r['lifecycle']['complete'])

    def test_rmk_empty_shell_retains_new_listing_identity_without_inventing_job(self):
        from test_successfactors import RMKCursor,row as rmk_row
        h=self.rmk_harness()
        original=h.serve
        phase={'listed':False}
        def serve(req):
            if '/search/' in req.url.path:
                phase['listed']=True
            if '/job/' in req.url.path and phase['listed']:
                return httpx.Response(200,text='<html></html>')
            return original(req)
        client=httpx.Client(transport=httpx.MockTransport(serve))
        self.addCleanup(client.close)
        h.factory.side_effect=lambda:SuccessFactorsAdapter(client)
        response,conn=h.request(RMKCursor([rmk_row()]))
        r=response.json()['site_results'][0]
        self.assertEqual(r['record_quality']['identity_unknown_failures'],0)
        self.assertEqual(r['identity_manifest']['identities'],['ACME:123'])
        self.assertEqual(r['identity_manifest']['new_identities_unpersisted_due_parse_failure'],1)
        self.assertFalse(r['lifecycle']['complete'])  # unreviewed/filtered native scope remains protected
        self.assertEqual(r['lifecycle']['deactivated'],0)


class ProductionQualityRegressions(unittest.TestCase):
    def setUp(self):
        self.data=json.loads((Path(__file__).parent/'fixtures/heavy_record_quality.json').read_text())

    def test_all_eight_workday_stubs_unknown(self):
        self.assertEqual(sum(r['count'] for r in self.data['workday']),8)
        self.assertTrue(all(not r['stable_identity_known'] for r in self.data['workday']))

    def test_43_counter_is_42_records_and_one_acquisition(self):
        rows=self.data['rmk']
        self.assertEqual(sum(r['legacy_failed_counter'] for r in rows),43)
        self.assertEqual(sum(r['legacy_failed_counter'] for r in rows if r['status']=='success'),42)

    def test_capped_error_samples_cannot_prove_eleven_signatures(self):
        self.assertEqual(sum(r['unrecorded_signature_count'] for r in self.data['rmk']),11)
        self.assertEqual(sum(r['missing_identity_total'] for r in self.data['rmk']),4)

    def test_empty_200_shell_on_complete_vodafone_downgrades(self):
        r={'host':'opportunities.vodafone.com','brand':'','status':'success','failed':0,'withdrawn':1,
           'detail_errors':[{'kind':'withdrawn','error':'RMK job is an empty/withdrawn shell'}],
           'lifecycle':{'successful':True,'complete':True,'scope':['opportunities.vodafone.com','vodafoneprod','']}}
        from app.heavy_scope_health import classify_scope
        d=classify_scope('successfactors_direct',r,NOW,load_policy())
        self.assertFalse(d['lifecycle_eligible'])
        self.assertEqual(d['coverage'],'INCOMPLETE')
        self.assertTrue(d['alert_worthy'])

    def test_proved_native_404_remains_protocol_absence(self):
        r={'status':'success','failed':0,'withdrawn':1,
           'detail_errors':[{'kind':'withdrawn','error':'Public RMK HTTP 404'}]}
        self.assertEqual(legacy_quality(r)['identity_unknown_failures'],0)

    def test_optional_warning_does_not_force_runner_partial(self):
        r=scope(True);q=RecordQuality();q.failure('salary',identity='native:id',stage='enrichment')
        r['record_quality']=q.summary(2,2,usable=2)
        r['failed']=1
        with patch('app.heavy_scope_health.load_policy',return_value=policy()):
            d=normalize_outcome(SOURCE,result(r),now=NOW)
        self.assertEqual(d['status'],'SUCCESS')
        self.assertEqual(d['failed_records'],1)
        self.assertEqual(d['heavy_scope_health']['scopes'][0]['record_quality']['identity_preserving_failures'],1)


class BaselineAndContractTests(unittest.TestCase):
    def test_healthy_heavy_known_gap_baseline_not_double_counted(self):
        r=scope()
        with patch('app.heavy_scope_health.load_policy',return_value=policy(review(r))):
            d=normalize_outcome(SOURCE,result(r),now=NOW)
        d['request']={}
        saved=next_baseline(None,{'runner_type':'heavy','sources':[d]})
        self.assertEqual(len(saved['sources']),1)
        self.assertEqual(d['scopes_unexpected_uncomplete'],0)
        self.assertEqual(saved['sources'][0]['known_gap_scopes'],[list(BOARD.scope)])

    def test_expired_review_cannot_promote_heavy_baseline(self):
        r=scope()
        with patch('app.heavy_scope_health.load_policy',return_value=policy(review(r))):
            d=normalize_outcome(SOURCE,result(r),now=NOW+timedelta(days=31))
        d['request']={}
        self.assertEqual(next_baseline(None,{'runner_type':'heavy','sources':[d]})['sources'],[])

    def test_healthy_comparable_heavy_result_collapse_still_partial(self):
        r=scope();old={'fetched':100,'known_gap_scopes':[list(BOARD.scope)]}
        with patch('app.heavy_scope_health.load_policy',return_value=policy(review(r))):
            d=normalize_outcome(SOURCE,result(r),old,now=NOW)
        self.assertEqual(d['status'],'PARTIAL')
        self.assertIn('unexpected_result_collapse',d['anomalies'])

    def test_mismatched_failure_ledger_rejected(self):
        r=scope();q=RecordQuality();r['failed']=3;r['record_quality']=q.summary(3,0)
        with self.assertRaisesRegex(ValueError,'Inconsistent record quality'):
            evaluate_source(SOURCE,result(r),NOW,policy(review(r)))

    def test_expiry_does_not_automatically_remove_quarantine(self):
        r=scope(status='failed');r.update(kind='access',error='reviewed')
        p=policy(review(r,state='NEEDS_REVIEW',expected_error={'kind':'access','error':'reviewed'}))
        d=classify_scope(SOURCE,r,NOW+timedelta(days=31),p)
        self.assertEqual(d['state'],'NEEDS_REVIEW')
        self.assertTrue(d['alert_worthy']);self.assertFalse(d['known_gap'])

    def test_recovered_scope_retains_failure_history(self):
        failed=scope(status='failed');failed.update(kind='forbidden',error='HTTP 403')
        healthy=scope()
        runs=[{'id':'old','source_name':SOURCE,'status':'success','started_at':NOW-timedelta(days=1),'metadata':result(failed)},
              {'id':'new','source_name':SOURCE,'status':'success','started_at':NOW,'metadata':result(healthy)}]
        with patch('app.heavy_scope_health.load_policy',return_value=policy(review(healthy))):
            d=health_report(runs,NOW)[SOURCE]['scopes'][0]
        self.assertEqual(d['state'],'HEALTHY')
        self.assertEqual(d['history']['consecutive_acquisition_failures'],0)
        self.assertEqual(d['history']['last_failure']['error'],'HTTP 403')


class LifecyclePolicyVetoTests(unittest.TestCase):
    def test_quarantine_can_only_veto_authority(self):
        from app.heavy_scope_health import policy_allows_lifecycle
        p=load_policy()
        for review_row in p['scopes']:
            if review_row['source_name']!='successfactors_direct':continue
            r={'host':review_row['scope'][0],'brand':review_row['scope'][1]}
            self.assertEqual(policy_allows_lifecycle('successfactors_direct',r,p),review_row['state']=='HEALTHY')

    def test_quarantine_does_not_expire_into_authority(self):
        from app.heavy_scope_health import policy_allows_lifecycle
        r={'host':'mann-hummel-careers.com','brand':''}
        p=load_policy()
        for review_row in p['scopes']:
            review_row['review_expires_at']='2026-10-01T00:00:00+00:00'
        self.assertFalse(policy_allows_lifecycle('successfactors_direct',r,p))

    def test_healthy_policy_does_not_create_completeness(self):
        from app.heavy_scope_health import policy_allows_lifecycle
        r={'host':'careers.nordanglia.com','brand':''}
        self.assertTrue(policy_allows_lifecycle('successfactors_direct',r,load_policy()))
        self.assertNotIn('lifecycle',r)

    def test_invalid_policy_stops_before_source_execution(self):
        from app.direct_freshness import run_group,load_config
        execute=Mock()
        with patch('app.heavy_scope_health.load_policy',side_effect=ValueError('invalid review')):
            with self.assertRaises(ValueError):
                run_group('heavy',load_config(),execute=execute)
        execute.assert_not_called()

class QuarantineTransactionRegression(unittest.TestCase):
    def test_otherwise_complete_native_traversal_is_vetoed_before_finish(self):
        from test_successfactors import IngestionTests, RMKCursor, row as rmk_row, synthetic_gate
        from app import successfactors_ingestion as ingestion
        import app.successfactors_scopes as native_gate
        h=IngestionTests();h.setUp();self.addCleanup(h.doCleanups)
        gate=synthetic_gate()
        h.stack.enter_context(patch.object(ingestion,'load_scope_gate',return_value=gate))
        h.stack.enter_context(patch.object(native_gate,'load_scope_gate',return_value=gate))
        h.stack.enter_context(patch.object(SuccessFactorsAdapter,'get_facets',return_value={}))
        review_row=review({'host':'careers.example.com'},'successfactors_direct',state='HEALTHY')
        with patch.object(ingestion,'load_policy',return_value=policy(review_row)):
            response,_=h.request(RMKCursor([rmk_row()]),locale=None)
        self.assertTrue(response.json()['site_results'][0]['lifecycle']['complete'])
        review_row['state']='NEEDS_REVIEW'
        with patch.object(ingestion,'load_policy',return_value=policy(review_row)):
            response,_=h.request(RMKCursor([rmk_row()]),locale=None)
        r=response.json()['site_results'][0]
        self.assertEqual(r['status'],'success')
        self.assertFalse(r['lifecycle']['complete'])
        self.assertEqual(r['lifecycle']['deactivated'],0)


class MissingInventoryTests(unittest.TestCase):
    def test_missing_native_inventory_is_unknown_not_reviewed_coverage(self):
        r=scope();p=policy(review(r));r.pop('lifecycle')
        d=classify_scope(SOURCE,r,NOW,p)
        self.assertEqual(d['coverage'],'UNKNOWN')
        self.assertFalse(d['known_gap']);self.assertTrue(d['alert_worthy'])

    def test_acquisition_success_with_unsuccessful_inventory_alerts(self):
        r=scope();p=policy(review(r));r['lifecycle']['successful']=False
        self.assertIn('acquisition_inventory_status_mismatch',classify_scope(SOURCE,r,NOW,p)['alerts'])


class QuarantineOutcomeContractTests(unittest.TestCase):
    def test_complete_flag_cannot_override_reviewed_quarantine(self):
        r=scope(True);p=policy(review(r,state='NEEDS_REVIEW'))
        d=classify_scope(SOURCE,r,NOW,p)
        self.assertFalse(d['lifecycle_eligible'])
        self.assertEqual(d['coverage'],'INCOMPLETE')
        with patch('app.heavy_scope_health.load_policy',return_value=p):
            with self.assertRaisesRegex(ValueError,'Unsafe authoritative'):
                normalize_outcome(SOURCE,result(r),now=NOW)


class FailureClassificationTests(unittest.TestCase):
    def test_non_transport_failure_is_needs_review_not_transient(self):
        r=scope(status='failed');r.update(kind='page_limit',error='uncertain traversal')
        d=classify_scope(SOURCE,r,NOW,policy())
        self.assertEqual(d['state'],'NEEDS_REVIEW')
        self.assertTrue(d['alert_worthy']);self.assertFalse(d['known_gap'])

    def test_only_identified_temporary_http_is_transient(self):
        r=scope(status='failed');r.update(kind='http',error='Public RMK HTTP 503')
        self.assertEqual(classify_scope(SOURCE,r,NOW,policy())['state'],'TRANSIENT_FAILURE')
        r['error']='Public RMK HTTP 403'
        self.assertEqual(classify_scope(SOURCE,r,NOW,policy())['state'],'NEEDS_REVIEW')
