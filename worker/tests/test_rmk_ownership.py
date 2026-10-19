"""Offline ownership contract regressions; optional isolated PostgreSQL tests."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest

import psycopg
from psycopg.types.json import Jsonb

from app.adapters.successfactors import RMKConfig
from app.freshness_report import proven_scope, classify, generate_report, full_complete
from app.lifecycle import LifecycleRun, InventoryCompletion
from app.rmk_ownership import RMKScopeIdentity, resolve_ownership
from app.successfactors_scopes import load_scope_gate
from test_lifecycle import Store, row, COMPLETE

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'rmk_ownership.json').read_text())
RULE = next(r for r in load_scope_gate().scopes if r.native_tenant == 'spravazel')
SCOPE = [RULE.host, RULE.native_tenant, RULE.native_search_brand]
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


def metadata(scalar=False):
    result = deepcopy(FIXTURE['observed']['rmk_site'])
    if scalar:
        result['host'] = RULE.host
    return result


def source(native=None, identity=None):
    return {'job_id': 'job', 'source_name': 'successfactors_direct',
            'source_job_id': identity or FIXTURE['observed']['source_job_id'], 'is_active': True,
            'native_scope': metadata() if native is None else native,
            'last_seen_at': NOW, 'last_verified_at': NOW,
            'lifecycle': {'run_id': 'run', 'scope': SCOPE, 'state': 'observed', 'inventory_complete': True}}


def history():
    return {'run': {'id': 'run', 'source_name': 'successfactors_direct', 'status': 'success',
                    'started_at': NOW, 'finished_at': NOW, 'metadata': {'sites': 1, 'validated_scopes': True,
                    'lifecycle': {'scopes': [{'scope': SCOPE, 'successful': True, 'complete': True}]}}}}


class OwnershipTests(unittest.TestCase):
    def resolve(self, m, identity=None):
        return resolve_ownership(m, identity or FIXTURE['observed']['source_job_id'])

    def test_scalar_exact(self):
        self.assertEqual(self.resolve(metadata(True)).state, 'EXACT')
        self.assertTrue(self.resolve(metadata(True)).matches(SCOPE))

    def test_new_write_preserves_transport_hosts(self):
        m = metadata()
        config = RMKConfig(m['tenant'], m['search_url'], m['brand'], m['locale'], m['locales'], [], set(m['hosts']))
        written = config.metadata()
        self.assertEqual(written['host'], RULE.host)
        self.assertEqual(written['hosts'], m['hosts'])
        self.assertEqual(self.resolve(written).state, 'EXACT')

    def test_owner_from_catalog_not_first_transport_host(self):
        config = RMKConfig('spravazel', 'https://'+RULE.host+'/search/', '', 'cs_CZ', ['cs_CZ'], [], {'aaa.example.com', RULE.host})
        self.assertEqual(config.metadata()['host'], RULE.host)
        self.assertFalse(self.resolve(config.metadata()).authoritative)

    def test_legacy_single_owner(self):
        self.assertEqual(self.resolve(metadata()).state, 'LEGACY_DETERMINISTIC')

    def test_legacy_catalog_owner_with_unreviewed_alias_requires_review(self):
        m = metadata(); m['hosts'].append('alias.example.com')
        result = self.resolve(m)
        self.assertEqual(result.identity.key, tuple(SCOPE))
        self.assertEqual(result.reason, 'unreviewed_transport_aliases')
        self.assertFalse(result.authoritative)

    def test_multiple_hosts_without_catalog_ambiguous_or_malformed(self):
        m = metadata(); m.pop('search_url'); m['hosts'].append('alias.example.com')
        self.assertIsNone(self.resolve(m).identity)
        self.assertFalse(self.resolve(m).matches(SCOPE))

    def test_singleton_hosts_alone_does_not_elect_owner(self):
        self.assertFalse(self.resolve({'hosts': [RULE.host]}).authoritative)

    def test_conflicting_scalar_host_abstains(self):
        m = metadata(True); m['host'] = 'other.example.com'
        self.assertEqual(self.resolve(m).state, 'AMBIGUOUS')

    def test_catalog_outside_hosts_abstains(self):
        m = metadata(); m['hosts'] = ['other.example.com']
        self.assertEqual(self.resolve(m).state, 'AMBIGUOUS')

    def test_malformed_metadata(self):
        for m in (None, [], {}, {'host': 42}, {**metadata(), 'hosts': RULE.host},
                  {**metadata(), 'hosts': [None]}, {**metadata(), 'locales': 'cs_CZ'}):
            with self.subTest(m=m):
                self.assertEqual(self.resolve(m).state, 'MALFORMED')

    def test_duplicate_or_invalid_hosts_abstain(self):
        for hosts in ([RULE.host, RULE.host], ['127.0.0.1'], [RULE.host, 1], [RULE.host, 'UPPER.example.com']):
            self.assertFalse(self.resolve({**metadata(), 'hosts': hosts}).authoritative)

    def test_wrong_brand_and_wrong_tenant(self):
        for m in ({**metadata(), 'brand': 'other'}, {**metadata(), 'tenant': 'other'}):
            self.assertFalse(self.resolve(m).matches(SCOPE))

    def test_unvalidated_scope_is_not_authoritative(self):
        m = metadata(); m.update(search_url='https://unknown.example.com/search/', hosts=['unknown.example.com'])
        self.assertEqual(self.resolve(m).state, 'REVIEW_REQUIRED')

    def test_wrong_locale_is_not_authoritative(self):
        m = metadata(); m.update(locale='fr_FR', locales=['fr_FR'])
        self.assertEqual(self.resolve(m).reason, 'unreviewed_locale_views')

    def test_unreviewed_extra_locale_is_not_authoritative(self):
        m = metadata(); m['locales'].append('fr_FR')
        self.assertFalse(self.resolve(m).authoritative)

    def test_locale_is_view_not_extra_source_identity_dimension(self):
        m = metadata(); m['locales'] = ['cs_CZ']
        self.assertEqual(self.resolve(m).identity.key, self.resolve(metadata()).identity.key)

    def test_native_id_validation(self):
        for identity in ('other:123', 'spravazel:1:2', 'spravazel:', 'spravazel:not-native', None):
            self.assertFalse(resolve_ownership(metadata(), identity).authoritative)

    def test_no_input_mutation(self):
        m = metadata(); before = deepcopy(m)
        self.resolve(m); self.assertEqual(m, before)

    def test_unsafe_catalog_rejected(self):
        for url in ('http://'+RULE.host+'/search/', 'https://127.0.0.1/search/',
                    'https://'+RULE.host+'/other/search/', 'https://'+RULE.host+'/job/123/'):
            self.assertFalse(self.resolve({**metadata(), 'search_url': url}).authoritative)

    def test_reviewed_locale_union_legacy_primary_allowed(self):
        rule = next(r for r in load_scope_gate().scopes if r.native_tenant == 'lasselsber')
        m = {'tenant': rule.native_tenant, 'brand': rule.native_search_brand, 'search_url': 'https://'+rule.host+'/search/',
             'hosts': [rule.host], 'locale': 'de_DE', 'locales': rule.locale_union, 'locale_home_used': True}
        self.assertTrue(resolve_ownership(m, 'lasselsber:123').authoritative)


class LifecycleOwnershipTests(unittest.TestCase):
    def reconcile(self, native=None, complete=COMPLETE, scope=None, observed=False):
        item = FIXTURE['unseen']
        db = Store([row(item['source_job_id'], 'successfactors_direct', metadata=metadata() if native is None else native)])
        inventory = LifecycleRun(db, 'successfactors_direct', 'run').scope(*(scope or SCOPE))
        if observed: inventory.observe(item['source_job_id'])
        result = inventory.finish(complete)
        return db, result

    def test_real_unseen_legacy_row_is_owned_and_can_reconcile(self):
        db, result = self.reconcile()
        self.assertEqual(result['deactivated'], 1)
        self.assertEqual(next(iter(db.jobs.values())), 'inactive')

    def test_new_scalar_can_reconcile(self):
        self.assertEqual(self.reconcile(metadata(True))[1]['deactivated'], 1)

    def test_incomplete_cannot_reconcile(self):
        self.assertEqual(self.reconcile(complete=InventoryCompletion(True, False, 'filtered'))[1]['deactivated'], 0)

    def test_failed_cannot_reconcile(self):
        self.assertEqual(self.reconcile(complete=InventoryCompletion(False, True, 'failed'))[1]['deactivated'], 0)

    def test_ambiguous_cannot_reconcile(self):
        m = metadata(True); m['host'] = 'other.example.com'
        self.assertEqual(self.reconcile(m)[1]['deactivated'], 0)

    def test_wrong_host_scope_cannot_reconcile(self):
        self.assertEqual(self.reconcile(scope=['wrong.example.com', *SCOPE[1:]])[1]['deactivated'], 0)

    def test_wrong_brand_scope_cannot_reconcile(self):
        self.assertEqual(self.reconcile(scope=[*SCOPE[:2], 'other'])[1]['deactivated'], 0)

    def test_wrong_locale_cannot_reconcile(self):
        self.assertEqual(self.reconcile({**metadata(), 'locale': 'fr_FR', 'locales': ['fr_FR']})[1]['deactivated'], 0)

    def test_unvalidated_scope_cannot_claim_complete(self):
        db, result = self.reconcile(scope=['wrong.example.com', *SCOPE[1:]], observed=True)
        self.assertFalse(result['complete']); self.assertTrue(next(iter(db.sources.values()))['active'])

    def test_metadata_change_after_snapshot_preserves_unseen(self):
        m = metadata(); item = FIXTURE['unseen']['source_job_id']
        db = Store([row(item, 'successfactors_direct', metadata=m)])
        inv = LifecycleRun(db, 'successfactors_direct', 'run').scope(*SCOPE)
        db.sources[item]['metadata'] = {**m, 'host': 'wrong.example.com'}
        self.assertEqual(inv.finish(COMPLETE)['deactivated'], 0)

    def test_wrong_native_brand_cannot_reconcile(self):
        self.assertEqual(self.reconcile({**metadata(), 'brand': 'other',
                         'search_url': 'https://'+RULE.host+'/other/search/'})[1]['deactivated'], 0)

    def test_missing_ownership_cannot_reconcile(self):
        self.assertEqual(self.reconcile({})[1]['deactivated'], 0)

    def test_absence_update_has_exact_metadata_preimage_predicate(self):
        db, _ = self.reconcile()
        deletion = next(q for q, _ in db.calls if q.startswith('update public.job_sources set is_active = false'))
        self.assertIn('jsonb_each', deletion)


class FreshnessOwnershipTests(unittest.TestCase):
    def test_real_123_positive_pattern_is_verified(self):
        self.assertTrue(proven_scope(source(), history(), 'observed'))
        self.assertEqual(classify({'status': 'active'}, [source()], history(), NOW)[0], 'VERIFIED_ACTIVE')

    def test_new_exact_format_is_verified(self):
        self.assertTrue(proven_scope(source(metadata(True)), history(), 'observed'))

    def test_ambiguous_stays_needs_verification(self):
        m = metadata(True); m['host'] = 'wrong.example.com'
        self.assertEqual(classify({'status': 'active'}, [source(m)], history(), NOW)[0], 'NEEDS_VERIFICATION')

    def test_incomplete_and_bounded_run_not_verified(self):
        for bounded in (False, True):
            runs = history()
            if bounded: runs['run']['metadata']['host'] = RULE.host
            else: runs['run']['metadata']['lifecycle']['scopes'][0]['complete'] = False
            self.assertFalse(proven_scope(source(), runs, 'observed'))

    def test_wrong_lifecycle_scope_not_verified(self):
        s = source(); s['lifecycle']['scope'] = ['wrong.example.com', *SCOPE[1:]]
        self.assertFalse(proven_scope(s, history(), 'observed'))

    def test_unvalidated_run_not_verified(self):
        runs = history(); runs['run']['metadata']['validated_scopes'] = False
        self.assertFalse(proven_scope(source(), runs, 'observed'))

    def test_full_run_flag_requires_validated_execution(self):
        run = history()['run']
        self.assertTrue(full_complete(run))
        run['metadata']['validated_scopes'] = False
        self.assertFalse(full_complete(run))

    def test_full_run_flag_requires_reviewed_scopes(self):
        run = history()['run']
        run['metadata']['lifecycle']['scopes'][0]['scope'] = ['wrong.example.com', *SCOPE[1:]]
        self.assertFalse(full_complete(run))

    def test_positive_and_absence_use_identical_ownership(self):
        for scalar in (True, False):
            s = source(metadata(scalar)); self.assertTrue(proven_scope(s, history(), 'observed'))
            s['lifecycle']['state'] = 'absent'; s['is_active'] = False
            self.assertTrue(proven_scope(s, history(), 'absent'))

    def test_scope_health_distinguishes_exact_legacy_and_review(self):
        sources = [source(), source(metadata(True)), source({**metadata(), 'hosts': ['wrong.example.com']})]
        report = generate_report([{'id': 'job', 'status': 'active'}], sources, list(history().values()), NOW)
        health = report['scope_health']['successfactors_direct']
        self.assertEqual(health['states'], {'AMBIGUOUS': 1, 'EXACT': 1, 'LEGACY_DETERMINISTIC': 1})
        self.assertEqual(health['authoritative_rows'], 2)


URL = os.environ.get('RMK_TEST_DATABASE_URL')


@unittest.skipUnless(URL, 'Isolated RMK PostgreSQL required')
class RMKDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(URL)
        if self.conn.info.dbname != 'career_os_rmk_test' or self.conn.info.host != '127.0.0.1':
            self.conn.close(); raise RuntimeError('Dedicated loopback RMK test database required')
        self.addCleanup(self.conn.close)
        self.conn.execute('drop schema public cascade; create schema public')
        self.conn.execute('''create table jobs(id text primary key,status text,last_seen_at timestamptz,
            last_verified_at timestamptz,updated_at timestamptz);
            create table job_sources(source_name text,source_job_id text,job_id text references jobs,
            is_active boolean,raw_payload jsonb,last_seen_at timestamptz,last_verified_at timestamptz,updated_at timestamptz)''')
        self.conn.commit()

    def insert(self, m):
        self.conn.execute("insert into jobs(id,status) values('job','active')")
        self.conn.execute("insert into job_sources(source_name,source_job_id,job_id,is_active,raw_payload) values(%s,%s,'job',true,%s)",
                          ('successfactors_direct', FIXTURE['unseen']['source_job_id'], Jsonb({'rmk_site': m})))

    def test_legacy_absence_executes_exact_sql_and_closes(self):
        self.insert(metadata())
        result = LifecycleRun(self.conn.cursor(), 'successfactors_direct', 'run').scope(*SCOPE).finish(COMPLETE)
        self.assertEqual(result['deactivated'], 1)
        self.assertEqual(self.conn.execute('select status from jobs').fetchone()[0], 'inactive')

    def test_ambiguous_legacy_is_untouched(self):
        self.insert({**metadata(), 'host': 'wrong.example.com'})
        result = LifecycleRun(self.conn.cursor(), 'successfactors_direct', 'run').scope(*SCOPE).finish(COMPLETE)
        self.assertEqual(result['deactivated'], 0)
        self.assertTrue(self.conn.execute('select is_active from job_sources').fetchone()[0])

    def test_scope_failure_rolls_back(self):
        self.insert(metadata()); self.conn.commit()
        LifecycleRun(self.conn.cursor(), 'successfactors_direct', 'run').scope(*SCOPE).finish(COMPLETE)
        self.conn.rollback()
        self.assertTrue(self.conn.execute('select is_active from job_sources').fetchone()[0])

    def test_metadata_change_after_read_is_guarded_in_sql(self):
        self.insert(metadata())
        conn = self.conn
        class Cursor:
            changed = False
            def __init__(self): self.cur = conn.cursor()
            def execute(self, query, args=()):
                if query.startswith('select id from public.jobs') and not self.changed:
                    self.changed = True
                    conn.execute("update job_sources set raw_payload=jsonb_set(raw_payload,'{rmk_site,host}',%s)",
                                 (Jsonb('wrong.example.com'),))
                return self.cur.execute(query, args)
            def fetchone(self): return self.cur.fetchone()
            def fetchall(self): return self.cur.fetchall()
        result = LifecycleRun(Cursor(), 'successfactors_direct', 'run').scope(*SCOPE).finish(COMPLETE)
        self.assertEqual(result['deactivated'], 0)
        self.assertTrue(conn.execute('select is_active from job_sources').fetchone()[0])
