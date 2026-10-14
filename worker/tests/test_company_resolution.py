"""Real PostgreSQL regressions; never run these against production."""
import importlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import unittest

import psycopg

from app.company_resolution import find_existing_company, get_or_create_company

FIXTURES = Path(__file__).parent / 'fixtures'
INCIDENT = json.loads((FIXTURES / 'company_resolution_incident.json').read_text())
URL = os.environ.get('COMPANY_TEST_DATABASE_URL')


class SourcePathTests(unittest.TestCase):
    def test_all_nine_ingestion_families_share_one_resolver(self):
        for module in ['ingestion', 'fantastic_ingestion', 'smartrecruiters_ingestion',
                       'greenhouse_ingestion', 'workable_ingestion', 'ashby_ingestion',
                       'lever_ingestion', 'workday_ingestion', 'successfactors_ingestion']:
            self.assertIs(importlib.import_module('app.' + module).get_or_create_company,
                          get_or_create_company, module)

    def test_no_source_specific_company_inserts(self):
        root = Path(__file__).parents[1] / 'app'
        for path in [root / 'ingestion.py', *root.glob('*_ingestion.py')]:
            self.assertNotIn('insert into public.companies', path.read_text().lower(), str(path))

    def test_none_does_not_query(self):
        class NoSQL:
            def execute(self, *args):
                raise AssertionError('No query permitted')
        self.assertIsNone(get_or_create_company(NoSQL(), None))
        self.assertIsNone(find_existing_company(NoSQL(), ''))


@unittest.skipUnless(URL, 'Separate company test PostgreSQL required')
class CompanyDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Strong guard against mistakenly supplying a production URL.
        with psycopg.connect(URL, autocommit=True) as conn:
            if conn.info.dbname != 'career_os_company_test' or conn.info.host not in {'127.0.0.1', 'localhost'}:
                raise RuntimeError('Dedicated loopback company test database required')
            conn.execute('drop schema public cascade')
            conn.execute('create schema public')
            conn.execute((FIXTURES / 'company_resolution_database.sql').read_text())

    def setUp(self):
        with psycopg.connect(URL, autocommit=True) as conn:
            conn.execute('truncate public.companies')

    def count(self):
        with psycopg.connect(URL) as conn:
            return conn.execute('select count(*) from public.companies').fetchone()[0]

    def test_all_seventeen_incident_keys_match_database(self):
        with psycopg.connect(URL) as conn:
            for case in INCIDENT:
                key = conn.execute('select public.normalize_company_text(%s)', (case['name'],)).fetchone()[0]
                self.assertEqual(key, case['normalized_key'])
                self.assertNotEqual(case['old_python_key'], key)

    def test_old_lookup_misses_every_incident_identity(self):
        with psycopg.connect(URL) as conn:
            for case in INCIDENT:
                cid = conn.execute('insert into public.companies(name) values(%s) returning id', (case['name'],)).fetchone()[0]
                self.assertIsNone(conn.execute('select id from public.companies where normalized_name=%s',
                                               (case['old_python_key'],)).fetchone())
                self.assertEqual(find_existing_company(conn.cursor(), case['name']), cid)

    def test_old_creation_repeats_duplicate_insert(self):
        case = INCIDENT[0]
        with psycopg.connect(URL) as conn:
            for _ in range(3):
                self.assertIsNone(conn.execute('select id from public.companies where normalized_name=%s',
                                               (case['old_python_key'],)).fetchone())
                conn.execute('insert into public.companies(name,normalized_name) values(%s,%s)',
                             (case['name'], case['old_python_key']))
        self.assertEqual(self.count(), 3)

    def test_new_company_returns_native_uuid_and_db_key(self):
        with psycopg.connect(URL) as conn:
            cid = get_or_create_company(conn.cursor(), '  D-ploy LLC  ')
            row = conn.execute('select id,name,normalized_name from public.companies').fetchone()
            self.assertEqual(row, (cid, 'D-ploy LLC', 'd ploy'))
        self.assertEqual(self.count(), 1)

    def test_existing_company_no_insert_no_display_overwrite(self):
        with psycopg.connect(URL) as conn:
            cid = get_or_create_company(conn.cursor(), 'H&M Group')
            for name in ['H&M GROUP', ' h m group ', 'H & M GROUP LLC']:
                self.assertEqual(get_or_create_company(conn.cursor(), name), cid)
            self.assertEqual(conn.execute('select name from public.companies where id=%s', (cid,)).fetchone()[0], 'H&M Group')
        self.assertEqual(self.count(), 1)

    def test_casing_and_whitespace_variants(self):
        with psycopg.connect(URL) as conn:
            cid = get_or_create_company(conn.cursor(), 'Second Foundation Tech a.s.')
            self.assertEqual(get_or_create_company(conn.cursor(), '  SECOND   FOUNDATION\tTECH a.s.  '), cid)
        self.assertEqual(self.count(), 1)

    def test_diacritics_follow_actual_database_contract(self):
        with psycopg.connect(URL) as conn:
            cid = get_or_create_company(conn.cursor(), 'Žluťoučký kůň s.r.o.')
            self.assertEqual(get_or_create_company(conn.cursor(), 'Zlutoucky kun'), cid)
        self.assertEqual(self.count(), 1)

    def test_legal_suffixes_only_follow_native_rule(self):
        with psycopg.connect(URL) as conn:
            cid = get_or_create_company(conn.cursor(), 'Example Company')
            for suffix in ['s.r.o.', 'a.s.', 'spol s r o', 'SE', 'Ltd', 'Limited', 'LLC', 'Inc.', 'GmbH']:
                self.assertEqual(get_or_create_company(conn.cursor(), 'Example Company ' + suffix), cid)
            self.assertNotEqual(get_or_create_company(conn.cursor(), 'Example Company Global'), cid)
        self.assertEqual(self.count(), 2)

    def test_punctuation_variants_do_not_expand_normalization(self):
        with psycopg.connect(URL) as conn:
            cid = get_or_create_company(conn.cursor(), "OAK'S LAB")
            self.assertEqual(get_or_create_company(conn.cursor(), 'OAK S LAB'), cid)
            self.assertNotEqual(get_or_create_company(conn.cursor(), 'OAKSLAB'), cid)

    def test_empty_normalized_identity_abstains(self):
        with psycopg.connect(URL) as conn:
            for value in ['', '   ', '---', '\t']:
                self.assertIsNone(get_or_create_company(conn.cursor(), value))
        self.assertEqual(self.count(), 0)

    def test_oldest_duplicate_is_selected_deterministically(self):
        with psycopg.connect(URL) as conn:
            ids = [conn.execute('insert into public.companies(name,created_at) values(%s,%s) returning id',
                                (name, date)).fetchone()[0] for name, date in
                   [('H&M GROUP', '2025-02-01'), ('H&M Group', '2025-01-01'), ('H&M Group LLC', '2025-03-01')]]
            for _ in range(5):
                self.assertEqual(get_or_create_company(conn.cursor(), 'H M Group'), ids[1])
        self.assertEqual(self.count(), 3)  # Existing duplicates are not repaired here.

    def test_uuid_breaks_created_at_ties(self):
        with psycopg.connect(URL) as conn:
            ids = [conn.execute('insert into public.companies(name,created_at) values(%s,%s) returning id',
                                ('D-ploy', '2025-01-01')).fetchone()[0] for _ in range(3)]
            self.assertEqual(get_or_create_company(conn.cursor(), 'D ploy'), min(ids))

    def test_three_repeated_incident_passes(self):
        identities = None
        counts = []
        for _ in range(3):
            with psycopg.connect(URL) as conn:
                current = {c['name']: get_or_create_company(conn.cursor(), c['name']) for c in INCIDENT}
                # Model repeated per-posting observations, not only one per name.
                for case in INCIDENT:
                    for _ in range(3):
                        self.assertEqual(get_or_create_company(conn.cursor(), case['name']), current[case['name']])
            if identities is None:
                identities = current
            self.assertEqual(current, identities)
            counts.append(self.count())
        self.assertEqual(counts, [17, 17, 17])

    def test_company_id_stable_in_repeated_canonical_writes(self):
        with psycopg.connect(URL) as conn:
            # PostgreSQL forbids temp-table foreign keys to permanent tables.
            conn.execute('create temporary table job_company_probe(id int primary key,company_id uuid)')
            cid = get_or_create_company(conn.cursor(), 'D-ploy')
            conn.execute('insert into job_company_probe values(1,%s)', (cid,))
            for variant in ['D-PLOY', 'D ploy', 'D-ploy GmbH']:
                conn.execute('update job_company_probe set company_id=%s where id=1',
                             (get_or_create_company(conn.cursor(), variant),))
                self.assertEqual(conn.execute('select company_id from job_company_probe').fetchone()[0], cid)

    def test_readonly_preview_does_not_insert_or_lock(self):
        with psycopg.connect(URL) as conn:
            cid = get_or_create_company(conn.cursor(), 'D-ploy')
        with psycopg.connect(URL) as conn:
            conn.read_only = True
            self.assertEqual(find_existing_company(conn.cursor(), 'D ploy'), cid)
            self.assertIsNone(find_existing_company(conn.cursor(), 'Missing Company'))
            self.assertEqual(conn.execute("select count(*) from pg_locks where pid=pg_backend_pid() and locktype='advisory'").fetchone()[0], 0)
        self.assertEqual(self.count(), 1)

    def test_readonly_creation_fails_before_insert(self):
        with psycopg.connect(URL) as conn:
            conn.read_only = True
            with self.assertRaisesRegex(RuntimeError, 'writable READ COMMITTED'):
                get_or_create_company(conn.cursor(), 'Missing')
        self.assertEqual(self.count(), 0)

    def test_autocommit_creation_fails_closed(self):
        with psycopg.connect(URL, autocommit=True) as conn:
            with self.assertRaisesRegex(RuntimeError, 'autocommit disabled'):
                get_or_create_company(conn.cursor(), 'Missing')
        self.assertEqual(self.count(), 0)

    def test_repeatable_read_creation_fails_closed(self):
        with psycopg.connect(URL) as conn:
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            with self.assertRaisesRegex(RuntimeError, 'READ COMMITTED'):
                get_or_create_company(conn.cursor(), 'Missing')
        self.assertEqual(self.count(), 0)

    def test_serializable_creation_fails_closed(self):
        with psycopg.connect(URL) as conn:
            conn.isolation_level = psycopg.IsolationLevel.SERIALIZABLE
            with self.assertRaisesRegex(RuntimeError, 'READ COMMITTED'):
                get_or_create_company(conn.cursor(), 'Missing')

    def test_rollback_removes_creation_and_releases_lock(self):
        with psycopg.connect(URL) as conn:
            cid = get_or_create_company(conn.cursor(), 'D-ploy')
            conn.rollback()
            self.assertIsNone(find_existing_company(conn.cursor(), 'D-ploy'))
        self.assertEqual(self.count(), 0)
        with psycopg.connect(URL) as conn:
            self.assertNotEqual(get_or_create_company(conn.cursor(), 'D-ploy'), cid)
        self.assertEqual(self.count(), 1)

    def test_helper_does_not_commit_callers_transaction(self):
        with psycopg.connect(URL) as conn:
            get_or_create_company(conn.cursor(), 'D-ploy')
            self.assertEqual(self.count(), 0)
            conn.commit()
        self.assertEqual(self.count(), 1)

    def test_transaction_error_is_propagated_without_repair(self):
        with psycopg.connect(URL) as conn:
            get_or_create_company(conn.cursor(), 'D-ploy')
            with self.assertRaises(psycopg.errors.DivisionByZero):
                conn.execute('select 1/0')
            with self.assertRaises(psycopg.errors.InFailedSqlTransaction):
                get_or_create_company(conn.cursor(), 'D-ploy')
            conn.rollback()
        self.assertEqual(self.count(), 0)

    def test_creator_lock_released_on_commit(self):
        with psycopg.connect(URL) as first, psycopg.connect(URL) as second:
            get_or_create_company(first.cursor(), 'D-ploy')
            query = 'select pg_try_advisory_xact_lock(hashtextextended(%s,0))'
            key = 'career-os:company:d ploy'
            self.assertFalse(second.execute(query, (key,)).fetchone()[0])
            second.rollback()
            first.commit()
            self.assertTrue(second.execute(query, (key,)).fetchone()[0])

    def test_existing_resolution_takes_no_creator_lock(self):
        with psycopg.connect(URL) as conn:
            get_or_create_company(conn.cursor(), 'D-ploy')
        with psycopg.connect(URL) as conn:
            get_or_create_company(conn.cursor(), 'D-ploy')
            self.assertEqual(conn.execute("select count(*) from pg_locks where pid=pg_backend_pid() and locktype='advisory'").fetchone()[0], 0)

    def test_two_simultaneous_misses_resolve_same_committed_row(self):
        barrier = threading.Barrier(2, timeout=10)
        class ConcurrentCursor:
            def __init__(self, cur):
                self.cur, self.connection, self.first_lookup = cur, cur.connection, True
            def execute(self, query, params):
                self.wait = self.first_lookup and query.lstrip().startswith('select id from public.companies')
                if self.wait:
                    self.first_lookup = False
                self.cur.execute(query, params)
            def fetchone(self):
                result = self.cur.fetchone()
                if self.wait:
                    self.wait = False
                    if result is not None:
                        raise AssertionError('Both initial snapshots must miss')
                    barrier.wait()
                return result
        def resolve(name):
            with psycopg.connect(URL) as conn:
                conn.execute("set local statement_timeout='10s'")
                cid = get_or_create_company(ConcurrentCursor(conn.cursor()), name)
            return cid
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(resolve, name) for name in ['H&M Group', 'H M GROUP LLC']]
            ids = [f.result(timeout=15) for f in calls]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self.count(), 1)

    def test_uncooperative_sql_can_duplicate_without_unique_constraint(self):
        with psycopg.connect(URL) as conn:
            conn.execute("insert into public.companies(name) values('D-ploy'),('D ploy')")
        self.assertEqual(self.count(), 2)  # Limitation is explicit, never hidden.
