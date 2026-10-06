"""Pure tests plus opt-in PostgreSQL tests, guarded against production DSNs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from app.job_profile.cli import main as cli_main
from app.job_profile.human_evaluation import HumanLabel, HumanReview, evaluate_labels, FIELDS
from app.job_profile.models import Fact, ValueState
from app.job_profile.pipeline import build_profile, input_fingerprint, reprocessing_layers
from app.job_profile.service import process_batch
from app.job_profile.storage import ProfileStore, bounded_ids
from app.merge import merge_duplicate_candidate


TEXT = 'Requirements\nEnglish B2 required.\n3+ years of experience.\nPython and SQL.\n' + 'Build reliable services with the team. '*20


def sample(job_id='sample'):
    return {'id': job_id, 'sources': [{'source_name': 'ashby_direct', 'source_job_id': 'board:native',
        'is_active': True, 'raw_payload': {'id': 'native', 'title': 'Senior Engineer', 'department': 'Engineering',
                                        'descriptionPlain': TEXT}}]}


class IncrementalTests(unittest.TestCase):
    def test_cheap_fingerprint_does_not_parse_text(self):
        with patch('app.job_profile.pipeline.languages', side_effect=AssertionError('text parser called')):
            fp = input_fingerprint(sample())
        self.assertEqual(fp['input_hash'], build_profile(sample()).metadata.input_hash)

    def test_metadata_only_reuses_text_layer(self):
        cache = {}
        first = build_profile(sample(), cache_out=cache)
        data = sample()
        data['sources'][0]['raw_payload']['department'] = 'Product'
        with patch('app.job_profile.pipeline.languages', side_effect=AssertionError('text parser called')):
            second = build_profile(data, text_cache=cache)
        self.assertEqual(first.requirements, second.requirements)
        self.assertEqual(reprocessing_layers(first.metadata, second.metadata), {'native','selection','profile'})

    def test_text_change_invalidates_cache(self):
        cache = {}
        build_profile(sample(), cache_out=cache)
        data = sample()
        data['sources'][0]['raw_payload']['descriptionPlain'] += '\nDocker'
        with patch('app.job_profile.pipeline.languages', wraps=__import__('app.job_profile.pipeline', fromlist=['languages']).languages) as parser:
            result = build_profile(data, text_cache=cache)
        parser.assert_called_once()
        self.assertIn('Docker', {x.technology for x in result.requirements.technologies.value})

    def test_same_text_different_source_never_borrows_evidence(self):
        cache = {}
        build_profile(sample(), cache_out=cache)
        data = sample()
        data['sources'][0]['source_job_id'] = 'board:other'
        result = build_profile(data, text_cache=cache)
        self.assertTrue(all(e.source_job_id == 'board:other' for e in result.evidence))

    def test_parser_version_invalidates_text_cache(self):
        from app.job_profile.versions import versions
        cache = {}
        build_profile(sample(), cache_out=cache)
        changed = {**versions(), 'parser': 'deterministic-v2'}
        with patch('app.job_profile.pipeline.versions', return_value=changed), patch('app.job_profile.pipeline.languages', return_value=[]) as parser:
            build_profile(sample(), text_cache=cache)
        parser.assert_called_once()

    def test_activity_change_reuses_rules_and_refreshes_evidence(self):
        cache = {}
        build_profile(sample(), cache_out=cache)
        data = sample()
        data['sources'][0]['is_active'] = False
        with patch('app.job_profile.pipeline.languages', side_effect=AssertionError('text parser called')):
            result = build_profile(data, text_cache=cache)
        self.assertTrue(all(e.source_active is False for e in result.evidence))

    def test_bounds_and_source_identity(self):
        key = str(uuid4())
        self.assertEqual(bounded_ids([key,key]), [key])
        for values in ([], [str(uuid4()) for _ in range(26)], ['invalid']):
            with self.assertRaises(ValueError):
                bounded_ids(values)

    def test_command_requires_explicit_write_flag(self):
        with patch.dict(os.environ, {'DATABASE_URL': 'not-connected'}), self.assertRaises(SystemExit) as exc:
            cli_main(['process'])
        self.assertEqual(exc.exception.code, 2)


class HumanEvaluationTests(unittest.TestCase):
    def review(self, profile, **values):
        labels = {field: HumanLabel() for field in FIELDS}
        labels.update({field: HumanLabel(state='known', value=value) for field,value in values.items()})
        return HumanReview(job_id=profile.metadata.job_id, profile_input_hash=profile.metadata.input_hash,
            sample_snapshot='2026-10-05', labels=labels, reviewer_id='reviewer', reviewed_at=datetime.now(timezone.utc))

    def test_unlabeled_metrics_are_not_accuracy(self):
        p = build_profile(sample())
        row = HumanReview(job_id='sample', profile_input_hash=p.metadata.input_hash, sample_snapshot='2026-10-05')
        metrics = evaluate_labels({'sample': p}, [row])
        self.assertTrue(all(m['precision'] is None and m['coverage'] is None for m in metrics.values()))

    def test_precision_recall_and_abstention(self):
        p = build_profile(sample())
        row = self.review(p, technologies=['Python','Docker'], workplace='hybrid', career_level='senior')
        metrics = evaluate_labels({'sample': p}, [row])
        self.assertEqual(metrics['technologies']['precision'], .5)
        self.assertEqual(metrics['technologies']['recall'], .5)
        self.assertEqual(metrics['workplace']['abstention_rate'], 1)
        self.assertEqual(metrics['career_level']['precision'], 1)

    def test_conflict_rate(self):
        p = build_profile(sample())
        p.workplace.mode = Fact(state=ValueState.CONFLICT)
        metrics = evaluate_labels({'sample': p}, [self.review(p, workplace='hybrid')])
        self.assertEqual(metrics['workplace']['conflict_rate'], 1)

    def test_review_input_hash_must_match(self):
        p = build_profile(sample())
        row = self.review(p, career_level='senior')
        row.profile_input_hash = '0'*64
        with self.assertRaises(ValueError):
            evaluate_labels({'sample': p}, [row])

    def test_duplicate_review_requires_adjudication(self):
        p = build_profile(sample())
        row = self.review(p, career_level='senior')
        with self.assertRaises(ValueError):
            evaluate_labels({'sample':p}, [row,row])

    def test_absence_is_explicit_not_empty_array(self):
        with self.assertRaises(ValueError):
            HumanLabel(state='known', value=[])
        self.assertIsNone(HumanLabel(state='not_mentioned').value)

    def test_label_metadata_and_vocab_validation(self):
        p = build_profile(sample())
        with self.assertRaises(ValueError):
            HumanReview(job_id='sample', profile_input_hash=p.metadata.input_hash, sample_snapshot='snapshot',
                labels={field: HumanLabel(state='known', value='invented') for field in FIELDS})

    def test_structured_labels_normalize_numbers_without_fake_evidence(self):
        p = build_profile(sample())
        row = self.review(p, experience=[{'min_years':3.0}])
        self.assertEqual(evaluate_labels({'sample':p}, [row])['experience']['precision'],1)
        self.assertNotIn('evidence_ids', row.labels['experience'].value[0])

    def test_invalid_structured_labels_are_rejected(self):
        p = build_profile(sample())
        for field,value in [('experience',[{'min_years':4,'max_years':2}]),
                            ('languages',[{'language':'en','cefr':'fluent'}]),
                            ('compensation',[{'min_amount':100,'max_amount':50}])]:
            with self.assertRaises(ValueError):
                self.review(p, **{field:value})

    def test_explicit_language_level_comparison(self):
        p = build_profile(sample())
        row = self.review(p, languages=[{'language':'en','requirement':'required','cefr':'B2'}])
        self.assertEqual(evaluate_labels({'sample':p},[row])['languages']['precision'],1)


TEST_DSN = os.environ.get('JOB_PROFILE_TEST_DATABASE_URL')


@unittest.skipUnless(TEST_DSN, 'Set JOB_PROFILE_TEST_DATABASE_URL to isolated test database')
class PostgreSQLProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        info = conninfo_to_dict(TEST_DSN)
        if info.get('host') not in {'127.0.0.1','localhost'} or info.get('dbname') != 'career_os_profile_test':
            raise ValueError('Refusing non-isolated integration database')
        cls.store = ProfileStore(TEST_DSN)
        root = Path(__file__).resolve().parents[1]
        with psycopg.connect(TEST_DSN, autocommit=True) as conn:
            if not conn.execute("select to_regclass('public.jobs')").fetchone()[0]:
                conn.execute((root/'tests/fixtures/job_profile_database.sql').read_text())
                conn.execute((root/'migrations/20261006_job_profiles.sql').read_text())

    def setUp(self):
        with self.store.connect() as conn:
            conn.execute('delete from public.jobs')
        self.job_id = str(uuid4())
        self.raw = sample()['sources'][0]['raw_payload']
        self.make_job(self.job_id)

    def make_job(self, key, name='ashby_direct'):
        with self.store.connect() as conn:
            conn.execute('insert into public.jobs(id,title) values (%s,%s)', (key, 'Engineer'))
            conn.execute('''insert into public.job_sources(job_id,source_name,source_job_id,raw_payload)
                values (%s,%s,%s,%s)''', (key,name,'board:'+key,Jsonb(self.raw)))

    def current(self):
        with self.store.connect() as conn:
            return conn.execute('select * from public.job_profile_current where job_id=%s', (self.job_id,)).fetchone()

    def generate(self):
        self.store.enqueue([self.job_id])
        result = process_batch(self.store)
        self.assertEqual(result[0]['state'], 'completed')
        return self.current()['current_version_id']

    def count_versions(self):
        with self.store.connect() as conn:
            return conn.execute('select count(*) as n from public.job_profile_versions where job_id=%s', (self.job_id,)).fetchone()['n']

    def change(self, **raw):
        self.raw.update(raw)
        with self.store.connect() as conn:
            conn.execute('update public.job_sources set raw_payload=%s where job_id=%s', (Jsonb(self.raw),self.job_id))

    def test_current_pointer_and_evidence_round_trip(self):
        vid = self.generate()
        with self.store.connect() as conn:
            row = conn.execute('select * from public.job_profile_versions where id=%s',(vid,)).fetchone()
        self.assertEqual(row['profile']['evidence'], row['evidence'])
        self.assertEqual(row['profile']['metadata']['input_hash'], row['input_hash'])
        self.assertTrue(row['input_snapshot']['text_layer']['evidence'])
        self.assertNotIn('raw_payload', row['input_snapshot'])

    def test_versions_are_immutable_even_to_owner(self):
        vid = self.generate()
        for sql in ('update public.job_profile_versions set profile=profile where id=%s',
                    'delete from public.job_profile_versions where id=%s'):
            with self.assertRaises(psycopg.Error), self.store.connect() as conn:
                conn.execute(sql, (vid,))
        with self.assertRaises(psycopg.Error), self.store.connect() as conn:
            conn.execute('truncate public.job_profile_versions')

    def test_same_hash_noop_creates_no_version(self):
        vid = self.generate()
        with patch('app.job_profile.service.build_profile', side_effect=AssertionError('generation called')):
            self.change(csrf='transient')
            result = process_batch(self.store)
        self.assertTrue(result[0]['no_op'])
        self.assertEqual(self.current()['current_version_id'], vid)
        self.assertEqual(self.count_versions(), 1)

    def test_timestamp_only_updates_never_invalidate(self):
        self.generate()
        with self.store.connect() as conn:
            conn.execute('''update public.job_sources set last_seen_at=now(),
                last_verified_at=now(), updated_at=now() where job_id=%s''', (self.job_id,))
        self.assertEqual(self.current()['processing_state'], 'completed')
        self.assertIsNone(self.store.claim())

    def test_metadata_only_does_not_reparse_description(self):
        self.generate()
        self.change(department='Product')
        with patch('app.job_profile.pipeline.languages', side_effect=AssertionError('text parser called')):
            result = process_batch(self.store)
        self.assertEqual(result[0]['layers'], ['native','profile','selection'])
        self.assertEqual(self.count_versions(), 2)

    def test_changed_description_creates_text_version(self):
        self.generate()
        self.change(descriptionPlain=TEXT+'\nDocker')
        result = process_batch(self.store)
        self.assertEqual(result[0]['layers'], ['profile','selection','text'])
        self.assertEqual(self.count_versions(), 2)

    def test_projector_and_schema_version_bumps(self):
        self.generate()
        from app.job_profile.versions import versions
        for name in ('projector','schema'):
            with patch('app.job_profile.pipeline.versions', return_value={**versions(), name:'next'}):
                self.store.enqueue([self.job_id])
                result = process_batch(self.store)
                self.assertEqual(result[0]['state'], 'completed')
                self.assertIn('profile', result[0]['layers'])

    def test_concurrent_workers_claim_once(self):
        self.store.enqueue([self.job_id])
        with ThreadPoolExecutor(max_workers=2) as pool:
            leases = list(pool.map(lambda _: self.store.claim(), range(2)))
        self.assertEqual(sum(x is not None for x in leases), 1)

    def test_expired_lease_fences_old_worker(self):
        self.store.enqueue([self.job_id])
        old = self.store.claim()
        job, _ = self.store.inputs(old)
        fp = input_fingerprint(job)
        with self.store.connect() as conn:
            conn.execute("update public.job_profile_current set lease_expires_at=now()-interval '1 second' where job_id=%s", (self.job_id,))
        fresh = self.store.claim()
        self.assertNotEqual(fresh.token, old.token)
        self.assertFalse(self.store.publish(old, fp, build_profile(job)))
        self.assertTrue(self.store.publish(fresh, fp, build_profile(job)))

    def test_lease_expiring_while_waiting_for_parent_cannot_publish(self):
        self.store.enqueue([self.job_id])
        lease = self.store.claim()
        job, _ = self.store.inputs(lease)
        with self.store.connect() as conn, ThreadPoolExecutor(max_workers=1) as pool:
            conn.execute('select id from public.jobs where id=%s for update',(self.job_id,))
            conn.execute("update public.job_profile_current set lease_expires_at=clock_timestamp()+interval '.2 seconds' where job_id=%s",(self.job_id,))
            future = pool.submit(self.store.publish,lease,input_fingerprint(job),build_profile(job))
            conn.execute('select pg_sleep(.4)')
            conn.commit()
            self.assertFalse(future.result(timeout=5))
        self.assertEqual(self.count_versions(),0)

    def test_failure_preserves_previous_pointer_and_retries(self):
        vid = self.generate()
        self.change(descriptionPlain=TEXT+'\nDocker')
        with patch('app.job_profile.service.build_profile', side_effect=RuntimeError('sensitive material')):
            self.assertEqual(process_batch(self.store)[0]['state'], 'failed')
        self.assertEqual(self.current()['current_version_id'], vid)
        self.assertEqual(self.current()['last_error'], 'RuntimeError')
        self.assertIsNone(self.store.claim())
        with self.store.connect() as conn:
            conn.execute("update public.job_profile_current set next_retry_at=now()-interval '1 second' where job_id=%s", (self.job_id,))
        self.assertEqual(process_batch(self.store)[0]['state'], 'completed')

    def test_expired_final_attempt_is_failed_not_infinite_retry(self):
        self.store.enqueue([self.job_id])
        self.store.claim()
        with self.store.connect() as conn:
            conn.execute("update public.job_profile_current set attempts=3, lease_expires_at=now()-interval '1 second' where job_id=%s", (self.job_id,))
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.current()['last_error'], 'lease_exhausted')
        self.store.enqueue([self.job_id], retry=True)
        self.assertIsNotNone(self.store.claim())

    def test_source_change_fences_inflight_publication(self):
        self.store.enqueue([self.job_id])
        lease = self.store.claim()
        job, _ = self.store.inputs(lease)
        self.change(department='Product')
        self.assertFalse(self.store.publish(lease, input_fingerprint(job), build_profile(job)))
        self.assertEqual(self.current()['processing_state'], 'stale')

    def test_failed_stale_worker_cannot_overwrite_new_state(self):
        self.store.enqueue([self.job_id])
        lease = self.store.claim()
        self.change(department='Product')
        self.store.fail(lease, RuntimeError())
        self.assertEqual(self.current()['processing_state'], 'stale')

    def test_current_pointer_must_belong_to_same_job(self):
        vid = self.generate()
        other = str(uuid4())
        self.make_job(other)
        self.store.enqueue([other])
        with self.assertRaises(psycopg.errors.ForeignKeyViolation), self.store.connect() as conn:
            conn.execute('update public.job_profile_current set current_version_id=%s where job_id=%s',(vid,other))

    def test_delete_removes_current_but_preserves_audit_versions(self):
        self.generate()
        with self.store.connect() as conn:
            conn.execute('delete from public.jobs where id=%s', (self.job_id,))
        self.assertIsNone(self.current())
        self.assertEqual(self.count_versions(), 1)

    def test_real_merge_invalidates_keeper_and_retires_removed_pointer(self):
        vid = self.generate()
        removed = str(uuid4())
        self.make_job(removed, name='jooble_direct')
        self.store.enqueue([removed])
        process_batch(self.store)
        with self.store.connect() as conn:
            cid = conn.execute('''insert into public.duplicate_candidates(job_a_id,job_b_id,confidence,reason)
                values (%s,%s,.99,'{}') returning id''',(self.job_id,removed)).fetchone()['id']
        with patch.dict(os.environ, {'DATABASE_URL': TEST_DSN}):
            result = merge_duplicate_candidate(str(cid), dry_run=False)
        self.assertTrue(result['merged'])
        self.assertEqual(self.current()['processing_state'], 'stale')
        self.assertEqual(self.current()['current_version_id'], vid)
        with self.store.connect() as conn:
            self.assertIsNone(conn.execute('select job_id from public.job_profile_current where job_id=%s',(removed,)).fetchone())
            self.assertEqual(conn.execute('select count(*) as n from public.job_profile_versions where job_id=%s',(removed,)).fetchone()['n'],1)
        self.assertEqual(process_batch(self.store)[0]['state'], 'completed')

    def test_unenrolled_source_changes_do_not_enqueue_jobs(self):
        self.change(department='Product')
        self.assertIsNone(self.current())

    def test_merge_into_unenrolled_keeper_inherits_work_not_old_profile(self):
        self.generate()
        keeper = str(uuid4())
        self.make_job(keeper)
        with self.store.connect() as conn:
            conn.execute('update public.job_sources set job_id=%s where job_id=%s',(keeper,self.job_id))
            conn.execute('delete from public.jobs where id=%s',(self.job_id,))
            current = conn.execute('select * from public.job_profile_current where job_id=%s',(keeper,)).fetchone()
        self.assertEqual(current['processing_state'], 'stale')
        self.assertIsNone(current['current_version_id'])
        self.assertEqual(process_batch(self.store)[0]['state'],'completed')

    def test_browser_roles_have_no_profile_access(self):
        self.generate()
        for role in ('anon','authenticated'):
            with self.store.connect() as conn:
                self.assertFalse(conn.execute("select has_table_privilege(%s,'public.job_profile_versions','INSERT') as ok", (role,)).fetchone()['ok'])
            with self.assertRaises(psycopg.errors.InsufficientPrivilege), self.store.connect() as conn:
                conn.execute('set local role '+role)
                conn.execute('select * from public.job_profile_versions')

    def test_service_permissions_and_database_immutability(self):
        vid = self.generate()
        with self.store.connect() as conn:
            conn.execute('set local role service_role')
            self.assertTrue(conn.execute('select id from public.job_profile_versions where id=%s',(vid,)).fetchone())
            self.assertTrue(conn.execute("select has_table_privilege('public.job_profile_current','UPDATE') as ok").fetchone()['ok'])
        with self.assertRaises(psycopg.errors.InsufficientPrivilege), self.store.connect() as conn:
            conn.execute('set local role service_role')
            conn.execute('update public.job_profile_versions set profile=profile where id=%s',(vid,))

    def test_service_role_can_enqueue_lease_and_publish(self):
        original = self.store.connect
        def service_connection():
            conn = original()
            conn.execute('set role service_role')
            conn.commit()
            return conn
        with patch.object(self.store, 'connect', side_effect=service_connection):
            self.store.enqueue([self.job_id])
            self.assertEqual(process_batch(self.store)[0]['state'], 'completed')

    def test_rls_still_denies_browser_after_accidental_select_grant(self):
        self.generate()
        with self.store.connect() as conn:
            conn.execute('grant select on public.job_profile_versions to anon')
            conn.execute('set local role anon')
            self.assertEqual(conn.execute('select count(*) as n from public.job_profile_versions').fetchone()['n'],0)
            conn.execute('reset role')
            conn.execute('revoke select on public.job_profile_versions from anon')

    def test_claim_skips_locked_work_instead_of_waiting(self):
        self.store.enqueue([self.job_id])
        with self.store.connect() as conn:
            conn.execute('select job_id from public.job_profile_current where job_id=%s for update',(self.job_id,))
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertIsNone(pool.submit(self.store.claim).result(timeout=5))

    def test_terminal_lease_cleanup_is_bounded(self):
        keys = [self.job_id]+[str(uuid4()) for _ in range(25)]
        for key in keys[1:]:
            self.make_job(key)
        with self.store.connect() as conn:
            for key in keys:
                conn.execute('''insert into public.job_profile_current(job_id,processing_state,attempts,lease_token,lease_expires_at)
                    values (%s,'processing',3,%s,now()-interval '1 second')''',(key,str(uuid4())))
        self.assertIsNone(self.store.claim())
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("select count(*) as n from public.job_profile_current where processing_state='failed'").fetchone()['n'],25)
        self.assertIsNone(self.store.claim())
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("select count(*) as n from public.job_profile_current where processing_state='failed'").fetchone()['n'],26)

    def test_terminal_lease_cleanup_skips_locked_rows(self):
        self.store.enqueue([self.job_id])
        self.store.claim()
        with self.store.connect() as conn:
            conn.execute("update public.job_profile_current set attempts=3,lease_expires_at=now()-interval '1 second' where job_id=%s",(self.job_id,))
        with self.store.connect() as conn, ThreadPoolExecutor(max_workers=1) as pool:
            conn.execute('select job_id from public.job_profile_current where job_id=%s for update',(self.job_id,))
            self.assertIsNone(pool.submit(self.store.claim).result(timeout=5))
