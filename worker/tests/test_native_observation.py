"""Identity before enrichment: mocked HTTP plus explicitly isolated PostgreSQL."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.native_observation import (IdentityManifest, diagnostic_context, identity_coverage,
                                    rmk_listing_identity, request_context)
from app.record_quality import RecordQuality
from app.lifecycle import LifecycleRun
from app.heavy_scope_health import evaluate_source, load_policy, policy_allows_lifecycle
from app.direct_freshness import normalize_outcome
from app.adapters.successfactors import RMKError, SuccessFactorsAdapter
from app.adapters.workday import WorkdayAdapter, WorkdayAPIError
from test_lifecycle import Store, row, COMPLETE
from test_rmk_ownership import SCOPE, metadata
from test_successfactors import config, native, ROOT, HOST, table, synthetic_gate
from test_workday import BOARD, PATH, POSTING_ID, detail

NOW=datetime(2026,10,20,tzinfo=timezone.utc)
SOURCE='successfactors_direct'


def rmk_scope(ids=('123', '999')):
    store=Store([row('spravazel:'+i,source=SOURCE,metadata=metadata(),job=i) for i in ids])
    inventory=LifecycleRun(store,SOURCE,'identity-run').scope(*SCOPE)
    return store,inventory,IdentityManifest(inventory)


def manifest_row(manifest,quality,complete=True):
    life={'scope':SCOPE,'complete':complete,'successful':True,'deactivated':0,'reactivated':0,'reason':'native complete'}
    return {'host':SCOPE[0],'brand':SCOPE[2],'status':'success','failed':quality['parsing_enrichment_failures'],
            'listing_records':manifest['observations_total'],'lifecycle':life,
            'record_quality':quality,'identity_manifest':manifest}


class NativeManifestTests(unittest.TestCase):
    def test_listing_identity_survives_parse_failure_and_missing_other_job_closes(self):
        store,inventory,m=rmk_scope()
        observed=m.add('listing:0','spravazel:123',origin='rmk_native_listing')
        self.assertIn('spravazel:123',inventory.seen)
        m.failure(observed,RMKError('malformed','missing title'),stage='canonical_parse',signature='missing title')
        result=inventory.finish(COMPLETE)
        self.assertTrue(store.sources['spravazel:123']['active'])
        self.assertFalse(store.sources['spravazel:999']['active'])
        self.assertEqual(result['deactivated'],1)

    def test_existing_identity_is_refreshed_without_canonical_enrichment(self):
        store,inventory,m=rmk_scope(('123',));m.add(0,'spravazel:123',origin='rmk_native_listing')
        inventory.finish(COMPLETE)
        self.assertNotEqual(store.sources['spravazel:123']['last_seen'],'original')
        self.assertEqual(store.sources['spravazel:123']['first_seen'],'original')
        self.assertEqual(m.summary()['existing_identities_seen'],1)

    def test_new_failed_identity_is_manifest_only_no_orphan_or_company_write(self):
        store,inventory,m=rmk_scope(('123',))
        m.add(0,'spravazel:123',origin='rmk_native_listing')
        o=m.add(1,'spravazel:777',origin='rmk_native_listing')
        m.failure(o,RMKError('malformed','no title'),stage='canonical_parse',signature='no title')
        inventory.finish(COMPLETE)
        self.assertEqual(len(store.sources),1)
        self.assertEqual(m.summary()['new_identities_unpersisted_due_parse_failure'],1)
        self.assertFalse(any('insert into' in q or 'companies' in q for q,_ in store.calls))

    def test_detail_failure_cannot_erase_proved_id(self):
        _,inventory,m=rmk_scope(('123',));o=m.add(0,'spravazel:123',origin='rmk_native_listing')
        m.failure(o,RMKError('http','HTTP 503'),stage='detail_fetch',signature='HTTP 503')
        self.assertEqual(m.summary()['identities_unknown'],0)
        self.assertIn('spravazel:123',inventory.seen)
        self.assertEqual(m.summary()['detail_fetch_failures'],1)

    def test_unknown_fingerprint_never_becomes_source_identity(self):
        _,inventory,m=rmk_scope();context=diagnostic_context({'bulletFields':['R1']},offset=0,position=0)
        m.add('diagnostic-only',context=context)
        self.assertEqual(m.summary()['identities'],[])
        self.assertFalse(m.identity_complete)
        self.assertEqual(len(inventory.seen),0)
        self.assertFalse(context['fingerprint_is_source_identity'])

    def test_source_id_format_is_checked_before_seen(self):
        _,inventory,m=rmk_scope()
        with self.assertRaises(ValueError):m.add(0,'spravazel:hash-of-title',origin='rmk_native_listing')
        self.assertEqual(len(inventory.seen),0)

    def test_wrong_tenant_rejected_before_seen(self):
        _,inventory,m=rmk_scope()
        with self.assertRaises(ValueError):m.add(0,'other:123',origin='rmk_native_listing')
        self.assertEqual(len(inventory.seen),0)

    def test_changed_native_id_is_conflict_not_alias(self):
        _,inventory,m=rmk_scope();o=m.add(0,'spravazel:123',origin='rmk_native_listing')
        with self.assertRaises(ValueError):m.prove(o,'spravazel:456','rmk_native_listing')
        self.assertFalse(m.identity_complete);self.assertNotIn('spravazel:456',inventory.seen)

    def test_manifest_accounting_validates_scope(self):
        _,_,m=rmk_scope();m.add(0,'spravazel:123',origin='rmk_native_listing')
        self.assertTrue(identity_coverage(m.summary(),SOURCE,SCOPE))
        with self.assertRaises(ValueError):identity_coverage(m.summary(),SOURCE,['other.example.com',*SCOPE[1:]])

    def test_manifest_cannot_claim_unknown_is_complete(self):
        _,_,m=rmk_scope();m.add(0);d=m.summary();d['identity_complete']=True
        with self.assertRaises(ValueError):identity_coverage(d,SOURCE,SCOPE)

    def test_unknown_plus_known_does_not_count_as_complete(self):
        _,_,m=rmk_scope();m.add(0);m.add(1,'spravazel:123',origin='rmk_native_listing')
        d=m.summary();self.assertEqual((d['observations_total'],d['identities_proved'],d['identities_unknown']),(2,1,1))
        self.assertFalse(identity_coverage(d,SOURCE,SCOPE))

    def test_duplicate_observations_do_not_duplicate_native_identity(self):
        _,_,m=rmk_scope();m.add(0,'spravazel:123',origin='rmk_native_listing');m.add(1,'spravazel:123',origin='rmk_native_listing')
        d=m.summary();self.assertEqual(d['identities_proved'],2);self.assertEqual(d['distinct_identities_proved'],1)
        self.assertTrue(identity_coverage(d,SOURCE,SCOPE))

    def test_full_failure_manifest_not_truncated_at_ten(self):
        _,_,m=rmk_scope(())
        for n in range(42):
            o=m.add(n,'spravazel:'+str(n+1),origin='rmk_native_listing')
            m.failure(o,RMKError('malformed','title'),stage='canonical_parse',signature='title')
        self.assertEqual(len(m.summary()['failure_observations']),42)
        self.assertEqual(m.summary()['canonical_parse_failures'],42)

    def test_request_context_drops_query_credentials(self):
        self.assertEqual(request_context('https://user:secret@example.com/job/123/'),{'request_url':None})
        self.assertEqual(request_context('https://example.com/job/123/?token=sensitive')['request_url'],'https://example.com/job/123/')
        self.assertTrue(request_context('https://example.com/job/123/?token=x')['query_omitted'])


class SourceIdentityTests(unittest.TestCase):
    def test_rmk_native_listing_id_matches_existing_contract(self):
        listing={'posting_id':'123','url':ROOT+'/job/Public-Native-Listing/123/'}
        self.assertEqual(rmk_listing_identity(config(),listing),'ACME:123')

    def test_rmk_listing_id_mismatch_not_synthesized(self):
        with self.assertRaises(ValueError):rmk_listing_identity(config(),{'posting_id':'124','url':ROOT+'/job/x/123/'})

    def test_rmk_wrong_host_not_approved_by_observation(self):
        with self.assertRaises(ValueError):rmk_listing_identity(config(),{'posting_id':'123','url':'https://new.example.com/job/x/123/'})

    def test_rmk_requisition_not_posting_id(self):
        with self.assertRaises(ValueError):rmk_listing_identity(config(),{'posting_id':'R123','url':ROOT+'/job/x/123/'})

    def test_rmk_empty_shell_preserves_listing_proof(self):
        listing={'posting_id':'123','url':ROOT+'/job/x/123/'}
        self.assertEqual(rmk_listing_identity(config(),listing),'ACME:123')
        with SuccessFactorsAdapter(httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,text='<html></html>')))) as a:
            with self.assertRaises(RMKError):a.get_detail(config(),listing)
        self.assertEqual(rmk_listing_identity(config(),listing),'ACME:123')

    def test_rmk_redirect_to_different_native_posting_is_conflict(self):
        a=SuccessFactorsAdapter(Mock())
        a._request=Mock(return_value=(ROOT+'/job/x/456/',native(posting='456')))
        with self.assertRaisesRegex(RMKError,'changed the native listing'):a.get_detail(config(),{'posting_id':'123','url':ROOT+'/job/x/123/'})

    def test_workday_missing_enrichment_preserves_header_wid_without_external_url(self):
        data=detail();data['jobPostingInfo'].pop('externalUrl');data['jobPostingInfo']['title']=''
        with WorkdayAdapter(httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=data)))) as a:
            with self.assertRaises(WorkdayAPIError) as exc:a.get_detail(BOARD,PATH)
        self.assertEqual(exc.exception.source_identity,f'{BOARD.tenant}:{BOARD.site}:{POSTING_ID}')

    def test_workday_pathless_generic_id_is_not_assumed_a_posting_wid(self):
        body={'total':1,'jobPostings':[{'id':POSTING_ID,'bulletFields':['R1']}],'facets':[]}
        with WorkdayAdapter(httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=body)))) as a:
            d=a.discover_candidates(BOARD)
        self.assertEqual(d.jobs,[]);self.assertEqual(d.counters['listing_missing_path'],1)
        self.assertFalse(d.identity_diagnostics[0]['fingerprint_is_source_identity'])
        self.assertEqual(d.identity_diagnostics[0]['position'],0)

    def test_workday_native_id_still_not_requisition(self):
        data=detail();data['jobPostingInfo']['id']='R123'
        with WorkdayAdapter(httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=data)))) as a:
            with self.assertRaises(WorkdayAPIError) as exc:a.get_detail(BOARD,PATH)
        self.assertIsNone(exc.exception.source_identity)


class AuthorityTests(unittest.TestCase):
    def test_proved_identity_required_parse_alert_does_not_erase_coverage(self):
        _,_,m=rmk_scope();o=m.add(0,'spravazel:123',origin='rmk_native_listing')
        m.failure(o,RMKError('malformed','missing title'),stage='canonical_parse',signature='missing title')
        q=RecordQuality();q.failure('malformed',identity=o.source_job_id,signature='missing title')
        r=manifest_row(m.summary(),q.summary(2,1,usable=1))
        d=evaluate_source(SOURCE,{'status':'success','sites':1,'failed':1,'site_results':[r],'lifecycle':{'scopes':[r['lifecycle']]}},NOW)
        self.assertTrue(d['scopes'][0]['lifecycle_eligible']);self.assertEqual(d['status'],'PARTIAL')
        self.assertEqual(d['scopes'][0]['coverage'],'AUTHORITATIVE_COMPLETE')

    def test_optional_warning_plus_reviewed_gap_can_be_success_with_known_gaps(self):
        _,_,m=rmk_scope();o=m.add(0,'spravazel:123',origin='rmk_native_listing')
        q=RecordQuality();q.failure('optional_salary',identity=o.source_job_id,stage='enrichment')
        r=manifest_row(m.summary(),q.summary(2,1,usable=1))
        review=next(r for r in load_policy()['scopes'] if r['scope'][0]=='mann-hummel-careers.com')
        gap={'host':review['scope'][0],'brand':'','status':'failed','failed':1,'listing_records':0,**review['expected_error']}
        native={'status':'success','run_id':'run','sites':2,'failed':2,'site_results':[r,gap],
                'listing_records':2,'created':0,'updated':0,'attached':0,'lifecycle':{'scopes':[r['lifecycle']],'deactivated':0,'reactivated':0}}
        self.assertEqual(normalize_outcome(SOURCE,native,now=NOW)['status'],'SUCCESS_WITH_KNOWN_GAPS')

    def test_unknown_identity_cannot_authorize_complete_inventory(self):
        _,_,m=rmk_scope();m.add(0)
        q=RecordQuality();q.failure('missing_identity')
        r=manifest_row(m.summary(),q.summary(1,0,usable=1))
        native={'status':'success','run_id':'run','sites':1,'failed':1,'site_results':[r],
                'listing_records':1,'created':0,'updated':0,'attached':0,'lifecycle':{'scopes':[r['lifecycle']],'deactivated':0,'reactivated':0}}
        with self.assertRaisesRegex(ValueError,'Unsafe authoritative'):normalize_outcome(SOURCE,native,now=NOW)

    def test_unexpected_fetch_failure_stays_alert_worthy_despite_proved_id(self):
        q=RecordQuality();q.failure('http',identity='spravazel:123',stage='detail_fetch',signature='Public RMK HTTP 503')
        d=q.summary(1,1,usable=1)
        self.assertTrue(d['alert_worthy']);self.assertEqual(d['detail_fetch_failures'],1)
        self.assertEqual(d['optional_enrichment_failures'],0)

    def test_native_404_with_proved_listing_id_is_visible_warning(self):
        q=RecordQuality();q.failure('withdrawn',identity='spravazel:123',stage='detail_fetch',signature='Public RMK HTTP 404')
        d=q.summary(1,1,usable=1)
        self.assertFalse(d['alert_worthy']);self.assertEqual(d['detail_fetch_failures'],1)

    def test_systemic_parser_failure_still_vetoes_authority(self):
        _,_,m=rmk_scope();o=m.add(0,'spravazel:123',origin='rmk_native_listing')
        q=RecordQuality();q.failure('malformed',identity=o.source_job_id)
        r=manifest_row(m.summary(),q.summary(1,1,usable=0))
        d=evaluate_source(SOURCE,{'status':'success','sites':1,'failed':1,'site_results':[r],'lifecycle':{'scopes':[r['lifecycle']]}},NOW)
        self.assertFalse(d['scopes'][0]['lifecycle_eligible']);self.assertEqual(d['status'],'PARTIAL')

    def test_reviewed_quarantine_veto_is_still_independent(self):
        self.assertFalse(policy_allows_lifecycle(SOURCE,{'host':'careers.dachser.com','brand':'dachser_europe'},load_policy()))


class WorkdayIntegrationTests(unittest.TestCase):
    def harness(self):
        from test_workday import WorkdayIngestionTests
        h=WorkdayIngestionTests();h.setUp();self.addCleanup(h.doCleanups);return h

    def test_canonical_failure_keeps_native_manifest_and_no_good_data_overwrite(self):
        import app.workday_ingestion as ingestion
        h=self.harness();cur,conn,adapter=h.setup_ingestion()
        with patch.object(ingestion,'persist_posting',side_effect=ValueError('unparseable canonical value')):
            r=h.ingest()['site_results'][0]
        self.assertEqual(r['identity_manifest']['identities'],[f'{BOARD.tenant}:{BOARD.site}:{POSTING_ID}'])
        self.assertEqual(r['identity_manifest']['canonical_upsert_failures'],1)
        self.assertEqual(r['identity_manifest']['new_identities_unpersisted_due_parse_failure'],1)
        self.assertTrue(conn.transaction.called)
        self.assertEqual(cur.source_writes,[])

    def test_workday_unknown_fetch_cannot_fall_back_to_path_identity(self):
        h=self.harness();cur,conn,adapter=h.setup_ingestion()
        adapter.get_detail.side_effect=WorkdayAPIError('unavailable','Workday HTTP 404')
        r=h.ingest()['site_results'][0]
        self.assertEqual(r['identity_manifest']['identities_unknown'],1)
        self.assertEqual(r['identity_manifest']['identities'],[])
        self.assertFalse(r['lifecycle']['complete'])


URL=os.environ.get('NATIVE_IDENTITY_TEST_DATABASE_URL')


@unittest.skipUnless(URL,'Dedicated isolated native identity PostgreSQL required')
class AtomicIdentityDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.real_connect=psycopg.connect
        self.conn=self.real_connect(URL)
        self.addCleanup(self.conn.close)
        if self.conn.info.host!='127.0.0.1' or self.conn.info.dbname!='career_os_identity_test':
            raise RuntimeError('Refusing non-isolated native identity database')
        self.conn.execute('drop schema public cascade; create schema public')
        self.conn.execute('''create table companies(id serial primary key,name text);
            create table jobs(id text primary key,company_id int references companies,title text,status text,
                last_seen_at timestamptz,last_verified_at timestamptz,updated_at timestamptz);
            create table job_sources(source_name text,source_job_id text,job_id text references jobs,
                is_active boolean,raw_payload jsonb,last_seen_at timestamptz,last_verified_at timestamptz,updated_at timestamptz,
                primary key(source_name,source_job_id));
            create table ingestion_runs(id text default gen_random_uuid()::text primary key,source_name text,status text,
                metadata jsonb,finished_at timestamptz,error_message text,records_fetched int,records_created int,
                records_updated int,records_failed int)''')
        c=config();self.meta=c.metadata()
        for posting in ['123','999']:
            self.conn.execute("insert into jobs(id,title,status) values(%s,'original canonical','active')",(posting,))
            self.conn.execute("insert into job_sources(source_name,source_job_id,job_id,is_active,raw_payload) values(%s,%s,%s,true,%s)",
                              (SOURCE,'ACME:'+posting,posting,Jsonb({'rmk_site':self.meta})))
        self.conn.commit()

    def run_ingestion(self, persist, *, bad_detail=False):
        from app import successfactors_ingestion as ingestion
        import app.successfactors_scopes as gate_module
        gate=synthetic_gate()
        def serve(req):
            if req.method=='POST':return httpx.Response(200,json={'facets':{'map':{}}})
            if '/search/' in req.url.path:return httpx.Response(200,text=table(['123','777'],total=2))
            posting='777' if '/777/' in req.url.path else '123'
            text=native(posting=posting)
            if bad_detail and posting=='123':text=text.replace('Native description','')
            return httpx.Response(200,text=text)
        client=httpx.Client(transport=httpx.MockTransport(serve));self.addCleanup(client.close)
        rows_sql="select '123','Example',%s::text,%s::text,%s::text,null::text" % tuple("'"+ROOT+"/job/x/123/'" for _ in range(3))
        with patch.object(ingestion,'FANTASTIC_ROWS_SQL',rows_sql), patch.object(ingestion.psycopg,'connect',side_effect=lambda *a,**k:self.real_connect(URL)), \
             patch.object(ingestion,'load_scope_gate',return_value=gate),patch.object(gate_module,'load_scope_gate',return_value=gate), \
             patch.object(ingestion,'SuccessFactorsAdapter',side_effect=lambda:SuccessFactorsAdapter(client)),patch.object(ingestion,'persist_posting',side_effect=persist):
            return ingestion.ingest_successfactors_jobs(validated_scopes=True)

    def test_savepoint_rolls_back_new_company_and_job_but_commits_seen_inventory(self):
        def persist(cur,site,c,listing,detail,evidence,rows,now):
            if detail['posting_id']=='123':return 'updated',False
            cid=cur.execute("insert into companies(name) values('must rollback') returning id").fetchone()[0]
            cur.execute("insert into jobs(id,company_id,title,status) values('bad',%s,'bad','active')",(cid,))
            cur.execute("insert into job_sources(source_name,source_job_id,job_id) values('successfactors_direct','ACME:777','missing-fk')")
        result=self.run_ingestion(persist);r=result['site_results'][0]
        self.assertTrue(r['lifecycle']['complete'])
        self.assertEqual(r['identity_manifest']['identities'],['ACME:123','ACME:777'])
        self.assertEqual(r['identity_manifest']['new_identities_unpersisted_due_parse_failure'],1)
        self.assertEqual(self.conn.execute('select count(*) from companies').fetchone()[0],0)
        self.assertEqual(self.conn.execute('select count(*) from jobs').fetchone()[0],2)
        self.assertEqual(self.conn.execute("select is_active,last_seen_at is not null from job_sources where source_job_id='ACME:123'").fetchone(),(True,True))
        self.assertEqual(self.conn.execute("select title,company_id from jobs where id='123'").fetchone(),('original canonical',None))
        self.assertEqual(r['lifecycle']['deactivated'],1)  # only truly absent ACME:999

    def test_existing_unparseable_source_refreshed_without_canonical_overwrite(self):
        def persist(cur,site,c,listing,detail,evidence,rows,now):
            cur.execute("insert into jobs(id,title,status) values('777','valid native job','active')")
            cur.execute("insert into job_sources(source_name,source_job_id,job_id,is_active,raw_payload) values(%s,'ACME:777','777',true,%s)",
                        (SOURCE,Jsonb({'rmk_site':self.meta})))
            return 'created',False
        result=self.run_ingestion(persist,bad_detail=True);r=result['site_results'][0]
        self.assertTrue(r['lifecycle']['complete'])
        self.assertEqual(self.conn.execute("select title,company_id,status from jobs where id='123'").fetchone(),('original canonical',None,'active'))
        self.assertEqual(self.conn.execute("select is_active,last_seen_at is not null from job_sources where source_job_id='ACME:123'").fetchone(),(True,True))
        self.assertEqual(self.conn.execute('select count(*) from companies').fetchone()[0],0)

    def test_detail_host_escape_keeps_listing_seen_but_vetoes_disappearance(self):
        original=SuccessFactorsAdapter.get_detail
        def get_detail(adapter,c,listing):
            if listing['posting_id']=='777':
                raise RMKError('unsafe_url','RMK request left its advertised public hosts',
                    blocked_transition={'from_host':HOST,'to_host':'unapproved.example.com'})
            return original(adapter,c,listing)
        with patch.object(SuccessFactorsAdapter,'get_detail',new=get_detail):
            r=self.run_ingestion(lambda *args:('updated',False))['site_results'][0]
        self.assertFalse(r['lifecycle']['complete'])
        self.assertEqual(r['lifecycle']['deactivated'],0)
        self.assertEqual(r['identity_manifest']['identity_conflicts'],1)
        self.assertIn('ACME:777',r['identity_manifest']['identities'])
        failure=r['identity_manifest']['failure_observations'][0]
        self.assertEqual(failure['diagnostic']['blocked_transition']['to_host'],'unapproved.example.com')
        self.assertTrue(self.conn.execute("select is_active from job_sources where source_job_id='ACME:999'").fetchone()[0])

    def test_unexpected_database_failure_rolls_back_all_lifecycle_effects(self):
        def persist(cur,*args):cur.execute('select * from deliberately_missing_table')
        with self.assertRaises(psycopg.ProgrammingError):self.run_ingestion(persist)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('select count(*) from job_sources where is_active').fetchone()[0],2)
        self.assertEqual(self.conn.execute('select count(*) from job_sources where last_seen_at is not null').fetchone()[0],0)


class AccountingRegressions(unittest.TestCase):
    def test_identity_and_quality_unknown_counters_must_agree(self):
        _,_,m=rmk_scope();m.add(0,'spravazel:123',origin='rmk_native_listing')
        q=RecordQuality();q.failure('missing_identity')
        r=manifest_row(m.summary(),q.summary(1,0,usable=1))
        with self.assertRaisesRegex(ValueError,'accounting disagree'):
            evaluate_source(SOURCE,{'status':'success','sites':1,'failed':1,'site_results':[r],'lifecycle':{'scopes':[r['lifecycle']]}},NOW)

    def test_identity_metrics_reach_structured_source_result(self):
        _,_,m=rmk_scope();o=m.add(0,'spravazel:123',origin='rmk_native_listing');m.success(o)
        q=RecordQuality();r=manifest_row(m.summary(),q.summary(1,1,usable=1))
        d=evaluate_source(SOURCE,{'status':'success','sites':1,'failed':0,'site_results':[r],'lifecycle':{'scopes':[r['lifecycle']]}},NOW)
        self.assertEqual(d['identity_totals']['identities_proved'],1)
        self.assertEqual(d['scopes'][0]['identity_accounting']['details_fetched'],1)

    def test_workday_unknown_diagnostic_has_page_and_position(self):
        body={'total':1,'jobPostings':[{'bulletFields':['R55252']}],'facets':[]}
        with WorkdayAdapter(httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=body)))) as a:
            result=a.discover_candidates(BOARD)
        d=result.identity_diagnostics[0]
        self.assertEqual((d['page'],d['offset'],d['position']),(0,0,0))
        self.assertEqual(d['record_keys'],['bulletFields'])
        self.assertEqual(len(d['diagnostic_fingerprint']),64)
        self.assertNotIn('source_job_id',d)

    def test_workday_wrong_site_manifest_cannot_reuse_a_wid(self):
        store=Store();inv=LifecycleRun(store,'workday_direct','run').scope(*BOARD.scope);m=IdentityManifest(inv)
        with self.assertRaises(ValueError):m.add(0,f'acme:Other:{POSTING_ID}',origin='workday_native_detail')
        self.assertEqual(inv.seen,set())

    def test_failed_canonical_record_not_persisted_later_is_counted_once(self):
        _,_,m=rmk_scope(())
        o=m.add(0,'spravazel:777',origin='rmk_native_listing')
        m.failure(o,RMKError('malformed','title'),stage='canonical_parse',signature='title')
        later=m.add(1,'spravazel:777',origin='rmk_native_listing');m.success(later,persisted=True)
        self.assertEqual(m.summary()['new_identities_unpersisted_due_parse_failure'],0)

class ProductionIdentityRegressions(unittest.TestCase):
    def setUp(self):
        self.fixture=json.loads((Path(__file__).parent/'fixtures/heavy_record_identity.json').read_text())

    def test_all_37_retained_failed_native_ids_resolve_without_detail_content(self):
        from app.adapters.successfactors import RMKConfig
        cases=self.fixture['rmk_retained_failures']+self.fixture['rmk_shells']
        self.assertEqual(len(cases),37)
        for case in cases:
            host,tenant,brand=case['scope']
            prefix='/' + (brand+'/' if brand else '')
            c=RMKConfig(tenant,'https://'+host+prefix+'search/',brand,'en_US',['en_US'],[],{host})
            # Synthetic URL wrapper tests the native contract; original failed slugs were not retained.
            listing={'posting_id':case['posting_id'],'url':'https://'+host+prefix+'job/fixture/'+case['posting_id']+'/'}
            with self.subTest(identity=case['source_job_id']):
                self.assertEqual(rmk_listing_identity(c,listing),case['source_job_id'])

    def test_eight_unretained_pathless_records_are_not_fabricated_into_wids(self):
        cases=self.fixture['workday_pathless']
        self.assertEqual(len(cases),8)
        self.assertTrue(all(r['native_source_job_id'] is None for r in cases))
        self.assertTrue(all(not r['raw_record_retained'] for r in cases))

    def test_missing_history_values_remain_missing_not_synthetic_source_ids(self):
        self.assertEqual(len(self.fixture['rmk_retained_failures'])+self.fixture['unretained_rmk_id_values'],42)
        self.assertEqual(self.fixture['unclassified_rmk_signatures'],11)
        self.assertTrue(all(r['source_job_id'] for r in self.fixture['rmk_retained_failures']))

    def test_vodafone_listing_shell_does_not_change_legitimate_absence_decision(self):
        from app.adapters.successfactors import RMKConfig
        from app.successfactors_scopes import load_scope_gate
        case=next(r for r in self.fixture['rmk_shells'] if r['scope'][0]=='opportunities.vodafone.com')
        scope=case['scope'];rule=next(r for r in load_scope_gate().scopes if r.host==scope[0])
        c=RMKConfig(scope[1],'https://'+scope[0]+'/search/',scope[2],rule.preferred_locale,[rule.preferred_locale],[],{scope[0]})
        native=c.metadata()
        store=Store([row(scope[1]+':123',source=SOURCE,metadata=native,job='seen'),
                     row(scope[1]+':1260754601',source=SOURCE,metadata=native,job='absent')])
        inv=LifecycleRun(store,SOURCE,'vodafone-offline').scope(*scope);m=IdentityManifest(inv)
        m.add(0,scope[1]+':123',origin='rmk_native_listing')
        o=m.add(1,case['source_job_id'],origin='rmk_native_listing')
        m.failure(o,RMKError('withdrawn',case['signature']),stage='canonical_parse',signature=case['signature'])
        result=inv.finish(COMPLETE)
        self.assertTrue(result['complete']);self.assertEqual(result['deactivated'],1)
        self.assertTrue(store.sources[scope[1]+':123']['active'])
        self.assertNotIn(case['source_job_id'],store.sources)
        self.assertEqual(m.summary()['new_identities_unpersisted_due_parse_failure'],1)
