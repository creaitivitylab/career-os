"""Offline tests: no real PostgreSQL connections or external adapters.

Run from worker/: python -m unittest discover -s tests -v
SQLite executes the production blocking SQL and safe-candidate SQL, with only
PostgreSQL placeholder/schema/JSON syntax adapted. Scoring still needs a
PostgreSQL integration check; SQLite does not implement pg_trgm.
"""
import json
import re
import sqlite3
import unicodedata
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import (
    ashby_ingestion as ashby,
    dedupe,
    fantastic_ingestion as fantastic,
    greenhouse_ingestion as greenhouse,
    ingestion as jooble,
    main,
    merge,
    smartrecruiters_ingestion as smartrecruiters,
    workable_ingestion as workable,
)
from app.canonical import canonical_field_updates, update_canonical_job
from app.ingestion_status import ALL_TENANTS_FAILED, direct_run_status
from app.source_priority import SOURCE_PRIORITY, source_priority


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict("os.environ", {
            "DATABASE_URL": "postgresql://offline.invalid/test",
        }))
        self.connect = self.stack.enter_context(patch(
            "psycopg.connect", side_effect=AssertionError("Real DB access forbidden")
        ))
        self.apis = {}
        for adapter, method in (
            (smartrecruiters.SmartRecruitersAdapter, "list_postings"),
            (smartrecruiters.SmartRecruitersAdapter, "get_posting"),
            (greenhouse.GreenhouseAdapter, "list_jobs"),
            (greenhouse.GreenhouseAdapter, "get_job"),
            (workable.WorkableAdapter, "get_account"),
            (ashby.AshbyAdapter, "list_jobs"),
            (jooble.JoobleDirectAdapter, "search"),
            (fantastic.FantasticJobsApifyAdapter, "fetch"),
        ):
            self.apis[adapter, method] = self.stack.enter_context(patch.object(
                adapter, method,
                side_effect=AssertionError("External API access forbidden"),
            ))

    def fake_database(self, cursor):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value = cursor
        self.connect.side_effect = None
        self.connect.return_value = conn
        return conn

    def set_api(self, adapter, method, result=None, side_effect=None):
        mock = self.apis[adapter, method]
        mock.side_effect = side_effect
        mock.return_value = result
        return mock


class FakeCursor:
    """Records SQL writes and supplies rows; never opens a connection."""
    def __init__(self, backing_sources=(), existing_source=True):
        self.backing_sources = list(backing_sources)
        self.existing_source = existing_source
        self.current = {
            "company_id": "company", "title": "ATS title",
            "description": "ATS description", "location_text": "Prague",
            "country_code": "CZ", "remote_type": "hybrid",
            "employment_type": "full-time", "salary_text": "100000 CZK",
            "skills": ["Python"], "canonical_url": "https://ats.example/job",
            "published_at": "2026-01-01", "expires_at": "2026-12-31",
        }
        self.rows = []
        self.calls = []
        self.canonical_writes = []
        self.source_writes = []
        self.run_update = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        query = query if isinstance(query, str) else query.as_string()
        normalized = " ".join(query.split())
        self.calls.append((normalized, params))
        self.rows = []
        if normalized.startswith("insert into public.ingestion_runs"):
            self.rows = [("run-id",)]
        elif normalized.startswith("select id from public.companies"):
            self.rows = [("company",)]
        elif normalized.startswith("select js.id, js.job_id"):
            self.rows = [("source-id", "job-id")] if self.existing_source else []
        elif normalized.startswith("insert into public.jobs"):
            self.rows = [("new-job",)]
        elif normalized.startswith('select "') and "for update" in normalized:
            fields = re.findall(r'"([a-z_]+)"', normalized)
            self.rows = [tuple(self.current[field] for field in fields)]
        elif normalized.startswith("select source_name from public.job_sources"):
            self.rows = [(name,) for name in self.backing_sources]
        elif normalized.startswith("update public.jobs"):
            fields = re.findall(r'"([a-z_]+)" = %s', normalized)
            update = dict(zip(fields, params[:-1]))
            self.canonical_writes.append(update)
        elif normalized.startswith(("update public.job_sources", "insert into public.job_sources")):
            self.source_writes.append((normalized, params))
        elif normalized.startswith("update public.ingestion_runs"):
            fields = re.findall(r"([a-z_]+) = %s", normalized)
            self.run_update = dict(zip(fields, params[:-1]))
        else:
            raise AssertionError(f"Unexpected SQL in fake: {normalized}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class CanonicalPriorityTests(OfflineTest):
    def test_priority_registry_and_merge_semantics_unchanged(self):
        self.assertIs(merge.SOURCE_PRIORITY, SOURCE_PRIORITY)
        self.assertIs(merge.source_priority, source_priority)
        self.assertEqual(source_priority([]), 0)
        self.assertEqual(source_priority(["unknown"]), 0)
        self.assertEqual(source_priority(["jooble_direct", "ashby_direct"]), 300)

    def test_lower_sources_cannot_replace_populated_fields(self):
        current = FakeCursor().current
        incoming = {field: None for field in current}
        incoming.update(title="Aggregator title", remote_type="remote", skills=[])
        for source in ("jooble_direct", "fantastic_jobs_apify"):
            for direct in (name for name, priority in SOURCE_PRIORITY.items() if priority == 300):
                with self.subTest(source=source, direct=direct):
                    self.assertEqual(canonical_field_updates(current, incoming, source, [direct]), {})
        self.assertEqual(canonical_field_updates(
            current, incoming, "jooble_direct", ["fantastic_jobs_apify"]
        ), {})

    def test_lower_source_fills_only_useful_empty_fields(self):
        current = {"description": "  ", "skills": [], "salary_text": None,
                   "remote_type": "unknown", "title": "", "company_id": None}
        incoming = {"description": "Full description", "skills": ["SQL"],
                    "salary_text": "", "remote_type": "remote",
                    "title": "Unknown position", "company_id": "company"}
        self.assertEqual(canonical_field_updates(
            current, incoming, "fantastic_jobs_apify", ["ashby_direct"]
        ), {"description": "Full description", "skills": ["SQL"],
            "remote_type": "remote", "company_id": "company"})

    def test_equal_and_higher_sources_keep_existing_update_semantics(self):
        incoming = {"title": "New title", "description": None, "salary_text": None}
        for backing in (["jooble_direct"], ["fantastic_jobs_apify"], ["ashby_direct"]):
            with self.subTest(backing=backing):
                self.assertEqual(canonical_field_updates(
                    {"description": "Keep?"}, incoming, "greenhouse_direct", backing,
                    preserve_if_none=("salary_text",),
                ), {"title": "New title", "description": None})

    def test_unknown_remote_value_does_not_fill_a_gap(self):
        self.assertEqual(canonical_field_updates(
            {"remote_type": None}, {"remote_type": "unknown"},
            "jooble_direct", ["ashby_direct"],
        ), {})

    def test_helper_locks_before_reading_sources_and_preserves_lifecycle(self):
        cur = FakeCursor(["ashby_direct"])
        now = datetime.now(timezone.utc)
        update_canonical_job(cur, "job-id", "jooble_direct", {"title": "Jooble"}, now)
        self.assertIn("for update", cur.calls[0][0])
        self.assertIn("select source_name", cur.calls[1][0])
        self.assertEqual(cur.canonical_writes, [{
            "last_seen_at": now, "last_verified_at": now,
            "status": "active", "updated_at": now,
        }])

    def test_helper_wraps_skills_as_json_and_rejects_unknown_fields(self):
        cur = FakeCursor()
        update_canonical_job(cur, "job-id", "fantastic_jobs_apify",
                             {"skills": ["SQL"]}, datetime.now(timezone.utc))
        self.assertEqual(cur.canonical_writes[0]["skills"].obj, ["SQL"])
        with self.assertRaises(ValueError):
            update_canonical_job(cur, "job-id", "jooble_direct", {"id": "oops"},
                                 datetime.now(timezone.utc))

    def test_both_aggregators_update_source_rows_without_downgrading_canonical(self):
        for module in (jooble, fantastic):
            with self.subTest(source=module.SOURCE_NAME):
                cur = FakeCursor(["ashby_direct", module.SOURCE_NAME])
                self.fake_database(cur)
                if module is jooble:
                    self.stack.enter_context(patch.object(jooble.JoobleDirectAdapter, "__init__", return_value=None))
                    payload = {"id": "posting", "title": "Lower title", "snippet": "Lower description"}
                    self.set_api(jooble.JoobleDirectAdapter, "search", {"data": {"jobs": [payload]}})
                    result = jooble.ingest_jooble_search("offline")
                else:
                    self.stack.enter_context(patch.object(fantastic.FantasticJobsApifyAdapter, "__init__", return_value=None))
                    payload = {"id": "posting", "title": "Lower title", "description_text": "Lower description"}
                    self.set_api(fantastic.FantasticJobsApifyAdapter, "fetch", [payload])
                    result = fantastic.ingest_fantastic_jobs()
                self.assertEqual(result["updated"], 1)
                self.assertNotIn("title", cur.canonical_writes[0])
                self.assertNotIn("description", cur.canonical_writes[0])
                self.assertEqual(len(cur.source_writes), 1)
                self.assertEqual(cur.source_writes[0][1][2].obj, payload)


class SQLiteCursor:
    def __init__(self, db):
        self.cursor = db.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        query = query.replace("public.", "").replace("%s", "?")
        query = query.replace("array_agg(distinct source_name order by source_name)",
                              "group_concat(distinct source_name)")
        query = query.replace("bool_or(", "max(")
        query = query.replace("%%", "%")
        query = query.replace("(reason ->> 'description_contained')::boolean",
                              "json_extract(reason, '$.description_contained')")
        self.cursor.execute(query, params)

    def fetchall(self):
        return self.cursor.fetchall()


class DedupeTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.create_function("btrim", 1, lambda value: value.strip() if value else value)
        self.db.create_function("normalize_job_text", 1, self.normalize)
        self.db.create_function("similarity", 2, self.title_similarity)
        self.db.create_function("greatest", -1, max)
        self.db.executescript("""
            create table companies (id text, name text, normalized_name text);
            create table jobs (id text primary key, company_id text, title text,
                normalized_title text, description text, normalized_location text,
                location_text text, canonical_url text, remote_type text);
            create table job_sources (job_id text, source_name text, source_url text);
            create table duplicate_candidates (id text, job_a_id text, job_b_id text,
                status text, title_similarity real, location_similarity real,
                confidence real, reason text);
            insert into companies values ('company', 'Company', 'company');
        """)

    @staticmethod
    def normalize(value):
        unaccented = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]+", " ", unaccented.lower()).strip()

    @staticmethod
    def title_similarity(a, b):
        # ASCII pg_trgm-style word padding, sufficient for these offline
        # fixtures. Real pg_trgm behavior is also checked by read-only preflight.
        def trigrams(value):
            result = set()
            for word in re.findall(r"[a-z0-9]+", value.lower()):
                padded = "  " + word + " "
                result.update(padded[i:i+3] for i in range(len(padded)-2))
            return result
        a, b = trigrams(a), trigrams(b)
        return len(a & b) / len(a | b) if a | b else 0.0

    def job(self, job_id, sources, company="company", title="Engineer"):
        self.db.execute("insert into jobs (id, company_id, title, normalized_title) values (?, ?, ?, ?)",
                        (job_id, company, title, self.normalize(title)))
        self.db.executemany("insert into job_sources (job_id, source_name) values (?, ?)",
                            [(job_id, source) for source in sources])

    def pairs(self):
        cur = SQLiteCursor(self.db)
        cur.execute(dedupe.DEDUPE_BLOCKING_SQL + " select job_a_id, job_b_id from blocked_pairs",
                    tuple(SOURCE_PRIORITY))
        return cur.fetchall()

    def test_all_six_sources_are_compared_without_self_or_reversed_pairs(self):
        for i, source in enumerate(SOURCE_PRIORITY):
            self.job(str(i), [source])
        pairs = self.pairs()
        self.assertEqual(len(pairs), 15)
        self.assertEqual(len(set(pairs)), 15)
        self.assertTrue(all(a < b for a, b in pairs))
        self.assertTrue(all((b, a) not in pairs for a, b in pairs))

    def test_multisource_job_is_one_entity_and_can_match_another_multisource_job(self):
        self.job("a", ["jooble_direct", "ashby_direct", "ashby_direct"])
        self.job("b", ["fantastic_jobs_apify", "workable_direct"])
        self.assertEqual(self.pairs(), [("a", "b")])

    def test_identical_title_case_punctuation_dashes_and_minor_suffix_survive(self):
        self.job("a", ["ashby_direct"], title="Senior Software Engineer - Backend")
        for job_id, title in (
            ("b", "Senior Software Engineer - Backend"),
            ("c", "SENIOR SOFTWARE ENGINEER - BACKEND"),
            ("d", "Senior Software Engineer — Backend!"),
            ("e", "Senior Software Engineer, Backend (Prague)"),
        ):
            self.job(job_id, ["greenhouse_direct"], title=title)
        pairs = self.pairs()
        for job_id in "bcde":
            self.assertIn(("a", job_id), pairs)

    def test_unrelated_titles_in_large_company_stop_before_scoring(self):
        for job_id, title in enumerate((
            "Senior Software Engineer", "Payroll Accountant", "Warehouse Driver",
            "Dentist", "Marketing Director", "CNC Machinist",
        )):
            self.job(str(job_id), ["jooble_direct"], title=title)
        self.assertEqual(self.pairs(), [])

    def test_supported_cross_source_pairs_survive_title_block(self):
        for sources in (
            ("ashby_direct", "workable_direct"),
            ("smartrecruiters_direct", "fantastic_jobs_apify"),
            ("jooble_direct", "greenhouse_direct"),
        ):
            with self.subTest(sources=sources):
                self.db.execute("delete from job_sources")
                self.db.execute("delete from jobs")
                self.job("a", [sources[0]], title="Backend Engineer")
                self.job("b", [sources[1]], title="Backend Engineer (Prague)")
                self.assertEqual(self.pairs(), [("a", "b")])

    def test_empty_titles_do_not_form_a_company_wide_block(self):
        self.job("a", ["jooble_direct"], title="")
        self.job("b", ["greenhouse_direct"], title="")
        self.assertEqual(self.pairs(), [])

    def test_pending_pairs_remain_visible_for_read_only_preflight(self):
        self.job("a", ["jooble_direct"])
        self.job("b", ["ashby_direct"])
        self.candidate("pending", "b", "a")
        self.assertEqual(self.pairs(), [("a", "b")])

    def test_loose_location_guard_excludes_unrelated_known_places(self):
        self.job("a", ["jooble_direct"])
        self.job("b", ["fantastic_jobs_apify"])
        self.db.executemany("update jobs set normalized_location=? where id=?",
                            [("praha", "a"), ("ostrava", "b")])
        self.assertEqual(self.pairs(), [])

    def test_location_guard_keeps_missing_contained_and_remote_locations(self):
        self.job("a", ["jooble_direct"])
        self.job("b", ["ashby_direct"])
        for a_location, b_location, remote in (
            ("praha", None, None), (None, "ostrava", None),
            ("praha", "praha hlavni mesto", None),
            ("praha", "ostrava", "remote"),
        ):
            with self.subTest(locations=(a_location, b_location), remote=remote):
                self.db.executemany("update jobs set normalized_location=? where id=?",
                                    [(a_location, "a"), (b_location, "b")])
                self.db.execute("update jobs set remote_type=? where id='a'", (remote,))
                self.assertEqual(self.pairs(), [("a", "b")])

    def test_company_block_and_supported_sources_limit_pairing(self):
        self.db.executemany("insert into companies values (?, ?, ?)", [
            ("other", "Other", "other"), ("empty", "Empty", " "),
            ("same", "COMPANY", "company"),
        ])
        self.job("a", ["jooble_direct"])
        self.job("b", ["ashby_direct"], "other")
        self.job("c", ["ashby_direct"], "empty")
        self.job("d", ["ashby_direct"], "empty")
        self.job("e", ["unrecognized_source"])
        self.job("f", ["workable_direct"], "same")
        self.assertEqual(self.pairs(), [("a", "f")])

    def candidate(self, candidate_id, a, b, status="pending", title=0.98,
                  location=0.90, confidence=0.95, contained=True):
        self.db.execute("insert into duplicate_candidates values (?, ?, ?, ?, ?, ?, ?, ?)",
                        (candidate_id, a, b, status, title, location, confidence,
                         json.dumps({"description_contained": contained})))

    def test_reviewed_pairs_are_not_recreated_in_either_orientation(self):
        self.job("a", ["jooble_direct"])
        self.job("b", ["ashby_direct"])
        self.candidate("reviewed", "b", "a", status="rejected")
        self.assertEqual(self.pairs(), [])

    def safe_ids(self):
        self.fake_database(SQLiteCursor(self.db))
        return [row[0] for row in dedupe.get_safe_auto_merge_candidates()]

    def test_safe_candidates_count_ambiguity_across_both_pair_positions(self):
        self.candidate("ab", "a", "b")
        self.candidate("bc", "b", "c")
        self.candidate("de", "d", "e")
        self.assertEqual(self.safe_ids(), ["de"])

    def test_safe_thresholds_and_description_containment_are_preserved(self):
        self.candidate("good", "a", "b")
        self.candidate("title", "c", "d", title=0.979)
        self.candidate("location", "e", "f", location=0.899)
        self.candidate("confidence", "g", "h", confidence=0.949)
        self.candidate("description", "i", "j", contained=False)
        self.candidate("reviewed", "k", "l", status="rejected")
        self.assertEqual(self.safe_ids(), ["good"])

    def test_rebuild_uses_bound_sources_and_unchanged_weights(self):
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchall.return_value = []
        self.fake_database(cur)
        dedupe.rebuild_duplicate_candidates()
        query, params = cur.execute.call_args_list[1].args
        self.assertEqual(params, tuple(SOURCE_PRIORITY))
        self.assertIn("title_similarity * 0.62", query)
        self.assertIn("location_similarity * 0.18", query)
        self.assertIn("description_similarity * 0.20", query)
        self.assertIn("scored as materialized", query)
        self.assertIn("source_sets as materialized", query)
        self.assertEqual(dedupe.TITLE_BLOCK_MIN_SIMILARITY, 0.45)
        self.assertNotIn("where js.job_id = j.id", query)
        # Once a psycopg query has parameters, literal percents must be escaped.
        self.assertIsNone(re.search(r"%(?![%s])", query.replace("%%", "")))

    def scoring_query(self):
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchall.return_value = []
        self.fake_database(cur)
        dedupe.rebuild_duplicate_candidates()
        query = cur.execute.call_args_list[1].args[0].split("\n                insert into", 1)[0]
        return query.replace("position(p.description_a in p.description_b)",
                             "instr(p.description_b, p.description_a)").replace(
                             "position(p.description_b in p.description_a)",
                             "instr(p.description_a, p.description_b)")

    def test_equal_length_descriptions_preserve_directional_word_similarity(self):
        self.job("a", ["jooble_direct"])
        self.job("b", ["greenhouse_direct"])
        self.db.executemany("update jobs set description=? where id=?",
                            [("z" * 120, "a"), ("a" * 120, "b")])
        self.db.create_function("word_similarity", 2, lambda a, b: 0.8 if a.startswith("z") else 0.2)
        cur = SQLiteCursor(self.db)
        cur.execute(self.scoring_query() + " select description_similarity from scored", tuple(SOURCE_PRIORITY))
        self.assertEqual(cur.fetchall(), [(0.8,)])

    def test_repeated_description_pair_is_scored_once(self):
        self.job("a", ["jooble_direct"])
        self.job("b", ["ashby_direct"])
        self.job("c", ["workable_direct"])
        self.db.executemany("update jobs set description=? where id=?", [
            ("snippet " * 20, "a"), ("prefix " + "snippet " * 20, "b"),
            ("prefix " + "snippet " * 20, "c"),
        ])
        word_similarity = MagicMock(return_value=1.0)
        self.db.create_function("word_similarity", 2, word_similarity)
        cur = SQLiteCursor(self.db)
        cur.execute(self.scoring_query() + " select description_similarity from scored", tuple(SOURCE_PRIORITY))
        self.assertEqual(cur.fetchall(), [(1.0,), (1.0,), (1.0,)])
        self.assertEqual(word_similarity.call_count, 1)

    def test_empty_and_identical_description_fast_paths_preserve_scores(self):
        self.job("a", ["jooble_direct"])
        self.job("b", ["ashby_direct"])
        word_similarity = MagicMock(side_effect=AssertionError("Fast path should skip expensive scoring"))
        self.db.create_function("word_similarity", 2, word_similarity)
        query = self.scoring_query() + " select description_similarity, description_contained from scored"
        for description, score, contained in (("", 0.0, False), ("word", 1.0, False), ("x" * 120, 1.0, True)):
            with self.subTest(description_length=len(description)):
                self.db.execute("update jobs set description=?", (description,))
                cur = SQLiteCursor(self.db)
                cur.execute(query, tuple(SOURCE_PRIORITY))
                self.assertEqual(cur.fetchall(), [(score, contained)])
        word_similarity.assert_not_called()

    def test_scoring_containment_keeps_120_character_rule_in_both_pair_directions(self):
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchall.return_value = []
        self.fake_database(cur)
        dedupe.rebuild_duplicate_candidates()
        query = cur.execute.call_args_list[1].args[0]
        # Execute all actual scoring CTEs, adapting PostgreSQL position syntax.
        scoring = query.split("\n                insert into", 1)[0]
        scoring = scoring.replace("position(p.description_a in p.description_b)",
                                  "instr(p.description_b, p.description_a)")
        scoring = scoring.replace("position(p.description_b in p.description_a)",
                                  "instr(p.description_a, p.description_b)")
        scoring = scoring.replace("%%", "%")
        self.db.create_function("greatest", -1, max)
        self.db.create_function("least", -1, min)
        self.db.create_function("similarity", 2, lambda a, b: 1.0 if a and a == b else 0.0)
        self.db.create_function("word_similarity", 2,
                                lambda snippet, full: 1.0 if snippet and snippet in full else 0.0)
        self.job("a", ["jooble_direct"])
        self.job("b", ["ashby_direct"])
        for length, expected in ((119, False), (120, True)):
            snippet = "x" * length
            full = "prefix " + snippet + " suffix"
            for a_description, b_description in ((snippet, full), (full, snippet)):
                with self.subTest(length=length, short_first=a_description == snippet):
                    self.db.executemany("update jobs set description = ? where id = ?",
                                        [(a_description, "a"), (b_description, "b")])
                    cursor = SQLiteCursor(self.db)
                    cursor.execute(scoring + " select description_similarity, description_contained from scored",
                                   tuple(SOURCE_PRIORITY))
                    self.assertEqual(cursor.fetchall(), [(1.0, expected)])


class AttachmentTests(OfflineTest):
    def test_smartrecruiters_zero_unique_and_ambiguous_matches(self):
        for rows, expected in (([], None), ([("a",)], "a"),
                               ([("a",), ("a",)], "a"),
                               ([("a",), ("b",)], None)):
            with self.subTest(rows=rows):
                cur = MagicMock()
                cur.fetchall.return_value = rows
                self.assertEqual(smartrecruiters.find_existing_job_by_url(
                    cur, "https://jobs.smartrecruiters.com/company/posting"
                ), expected)

    def test_smartrecruiters_real_query_normalizes_query_fragment_and_whitespace(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.create_function("btrim", 1, lambda value: value.strip())
        db.create_function("split_part", 3, lambda value, sep, part: value.split(sep)[part - 1])
        db.executescript("""
            create table jobs (id text, canonical_url text);
            create table job_sources (job_id text, source_url text);
            insert into jobs values ('a', null);
            insert into job_sources values ('a', ' HTTPS://jobs.smartrecruiters.com/Company/123/#anchor?tracking=1 ');
            insert into job_sources values ('a', 'https://jobs.smartrecruiters.com/Company/123/?tracking=2');
        """)
        url = "https://jobs.smartrecruiters.com/company/123?tracking=3#anchor"
        self.assertEqual(smartrecruiters.find_existing_job_by_url(SQLiteCursor(db), url), "a")
        db.execute("insert into jobs values ('b', ?)", (url,))
        self.assertIsNone(smartrecruiters.find_existing_job_by_url(SQLiteCursor(db), url))

    def test_greenhouse_url_requires_exact_posting_and_board(self):
        cases = [
            ("https://boards.greenhouse.io/acme/jobs/123?x=1#apply", False, True),
            ("https://job-boards.eu.greenhouse.io/acme/jobs/123/", False, True),
            ("https://boards.greenhouse.io/acme/jobs/1234", False, False),
            ("https://boards.greenhouse.io/other/jobs/123", False, False),
            ("https://boards.greenhouse.io/embed/job_app?for=acme&token=123", False, True),
            ("https://boards.greenhouse.io/embed/job_app?for=other&token=123", False, False),
            ("https://careers.example/job?gh_jid=123", False, False),
            ("https://careers.example/job?gh_jid=123", True, True),
            ("https://careers.example/job?gh_jid=1234", True, False),
            ("https://careers.example/job?gh_jid=123&gh_jid=456", True, False),
            ("https://boards.greenhouse.io.evil.example/acme/jobs/123", False, False),
        ]
        for url, known, expected in cases:
            with self.subTest(url=url, board_is_known=known):
                self.assertEqual(greenhouse.greenhouse_url_matches(url, "acme", "123", known), expected)

    def test_greenhouse_combines_all_evidence_before_checking_ambiguity(self):
        native = ("a", "jooble_direct", "https://boards.greenhouse.io/acme/jobs/123", {})
        metadata = ("b", "fantastic_jobs_apify", "https://careers.example/job?gh_jid=123",
                    {"source": "greenhouse", "source_slug": "acme", "job_id": "123"})
        substring = ("c", "jooble_direct", "https://boards.greenhouse.io/acme/jobs/1234", {})
        for rows, expected in (([], None), ([native, native, substring], "a"),
                               ([metadata], "b"), ([native, metadata], None)):
            with self.subTest(rows=rows):
                cur = MagicMock()
                cur.fetchall.return_value = rows
                self.assertEqual(greenhouse.find_existing_greenhouse_job(cur, "acme", "123"), expected)

    def test_ashby_unique_uuid_behavior_is_preserved(self):
        for rows, expected in (([], None), ([("a",), ("a",)], "a"),
                               ([("a",), ("b",)], None)):
            cur = MagicMock()
            cur.fetchall.return_value = rows
            self.assertEqual(ashby.find_existing_ashby_job(cur, "uuid"), expected)

    def test_workable_exact_matching_and_existing_source_guard_are_preserved(self):
        cur = MagicMock()
        cur.fetchall.return_value = [("a", "  SOFTWARE Engineer "), ("b", "Engineer")]
        self.assertEqual(workable.find_unique_fantastic_match(cur, "Acme", "software engineer"), "a")
        cur.fetchall.return_value = [("a", "Engineer"), ("b", "Engineer")]
        self.assertIsNone(workable.find_unique_fantastic_match(cur, "Acme", "Engineer"))
        cur.fetchone.return_value = (True,)
        self.assertTrue(workable.job_already_has_workable(cur, "a"))


class IngestionFailureTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(smartrecruiters, "discover_tenants", return_value=["one", "two"]))
        self.stack.enter_context(patch.object(greenhouse, "discover_boards", return_value=["one", "two"]))
        self.stack.enter_context(patch.object(workable, "WORKABLE_TENANTS", {"One": "one", "Two": "two"}))
        self.stack.enter_context(patch.object(ashby, "discover_boards", return_value=[("One", "one"), ("Two", "two")]))
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.sources = (
            ("smartrecruiters", smartrecruiters.SmartRecruitersAdapter, "list_postings", [], "tenant_errors"),
            ("greenhouse", greenhouse.GreenhouseAdapter, "list_jobs", [], "board_errors"),
            ("workable", workable.WorkableAdapter, "get_account", {"jobs": []}, "tenant_errors"),
            ("ashby", ashby.AshbyAdapter, "list_jobs", [], "board_errors"),
        )

    def request(self, name, cur):
        conn = self.fake_database(cur)
        response = self.client.post(f"/ingest/ats/{name}", json={})
        return response, conn

    def test_shared_status_handles_empty_discovery_and_partial_success(self):
        self.assertEqual(direct_run_status(0, 0), "success")
        self.assertEqual(direct_run_status(2, 0), "failed")
        self.assertEqual(direct_run_status(2, 1), "success")

    def test_all_tenant_fetch_failures_persist_failure_and_return_http_502(self):
        for name, adapter, method, empty, errors_key in self.sources:
            with self.subTest(source=name):
                self.set_api(adapter, method, side_effect=RuntimeError("Fixture upstream unavailable"))
                cur = FakeCursor()
                response, conn = self.request(name, cur)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["status"], "failed")
                self.assertEqual(len(response.json()[errors_key]), 2)
                self.assertEqual(cur.run_update["status"], "failed")
                self.assertEqual(cur.run_update["error_message"], ALL_TENANTS_FAILED)
                self.assertEqual(cur.run_update["metadata"].obj["successful_tenants"], 0)
                self.assertIn("records_failed", cur.run_update)
                self.assertEqual(conn.commit.call_count, 2)
                conn.rollback.assert_not_called()

    def test_partial_tenant_failure_keeps_success_and_reports_errors(self):
        for name, adapter, method, empty, errors_key in self.sources:
            with self.subTest(source=name):
                self.set_api(adapter, method, side_effect=[RuntimeError("Fixture failure"), empty])
                cur = FakeCursor()
                response, _ = self.request(name, cur)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "success")
                self.assertEqual(len(response.json()[errors_key]), 1)
                self.assertEqual(cur.run_update["status"], "success")
                self.assertIsNone(cur.run_update["error_message"])
                self.assertEqual(cur.run_update["metadata"].obj["successful_tenants"], 1)

    def test_valid_empty_boards_are_successful(self):
        for name, adapter, method, empty, errors_key in self.sources:
            with self.subTest(source=name):
                self.set_api(adapter, method, empty)
                cur = FakeCursor()
                response, _ = self.request(name, cur)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()[errors_key], [])
                self.assertEqual(cur.run_update["metadata"].obj["successful_tenants"], 2)

    def test_all_detail_fetch_failures_are_not_mistaken_for_success(self):
        for name, adapter, listing, detail, summary in (
            ("smartrecruiters", smartrecruiters.SmartRecruitersAdapter, "list_postings", "get_posting", {"id": "123"}),
            ("greenhouse", greenhouse.GreenhouseAdapter, "list_jobs", "get_job", {"id": 123, "location": {"name": "Prague"}}),
        ):
            with self.subTest(source=name):
                self.set_api(adapter, listing, [summary])
                self.set_api(adapter, detail, side_effect=RuntimeError("Fixture detail failure"))
                cur = FakeCursor()
                response, _ = self.request(name, cur)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["failed"], 2)
                self.assertEqual(cur.run_update["records_failed"], 2)

    def test_missing_posting_identifiers_fail_when_no_board_can_process_data(self):
        for name, adapter, method, listing in (
            ("smartrecruiters", smartrecruiters.SmartRecruitersAdapter, "list_postings", [{}]),
            ("greenhouse", greenhouse.GreenhouseAdapter, "list_jobs", [{"location": {"name": "Prague"}}]),
            ("workable", workable.WorkableAdapter, "get_account", {"jobs": [{"country": "CZ"}]}),
            ("ashby", ashby.AshbyAdapter, "list_jobs", [{"location": "Prague", "isListed": True}]),
        ):
            with self.subTest(source=name):
                self.set_api(adapter, method, listing)
                cur = FakeCursor()
                response, _ = self.request(name, cur)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["failed"], 2)

    def test_zero_discovered_tenants_is_a_successful_noop_for_all_sources(self):
        self.stack.enter_context(patch.object(smartrecruiters, "discover_tenants", return_value=[]))
        self.stack.enter_context(patch.object(greenhouse, "discover_boards", return_value=[]))
        self.stack.enter_context(patch.object(workable, "WORKABLE_TENANTS", {}))
        self.stack.enter_context(patch.object(ashby, "discover_boards", return_value=[]))
        for name, adapter, method, empty, errors_key in self.sources:
            with self.subTest(source=name):
                cur = FakeCursor()
                response, _ = self.request(name, cur)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "success")
                self.assertEqual(cur.run_update["metadata"].obj["successful_tenants"], 0)

    def test_no_czech_postings_is_a_successful_processed_board(self):
        for name, adapter, method, listing in (
            ("greenhouse", greenhouse.GreenhouseAdapter, "list_jobs", [{"id": 123, "location": {"name": "Berlin"}}]),
            ("workable", workable.WorkableAdapter, "get_account", {"jobs": [{"shortcode": "123", "country": "DE"}]}),
            ("ashby", ashby.AshbyAdapter, "list_jobs", [{"location": "Berlin", "isListed": True}]),
        ):
            with self.subTest(source=name):
                self.set_api(adapter, method, listing)
                cur = FakeCursor()
                response, _ = self.request(name, cur)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(cur.run_update["metadata"].obj["successful_tenants"], 2)

    def test_all_four_direct_sources_can_update_lower_priority_canonical_fields(self):
        fixtures = (
            ("smartrecruiters", smartrecruiters.SmartRecruitersAdapter, "list_postings", [{"id": "123"}],
             "get_posting", {"name": "Incoming title", "company": {"name": "Company"}, "location": {"country": "cz"}}),
            ("greenhouse", greenhouse.GreenhouseAdapter, "list_jobs", [{"id": 123, "location": {"name": "Prague"}}],
             "get_job", {"title": "Incoming title", "location": {"name": "Prague"}}),
            ("workable", workable.WorkableAdapter, "get_account", {"jobs": [{"shortcode": "123", "title": "Incoming title", "country": "CZ"}]},
             None, None),
            ("ashby", ashby.AshbyAdapter, "list_jobs", [{"title": "Incoming title", "location": "Prague", "jobUrl": "https://jobs.ashbyhq.com/one/12345678-1234-1234-1234-123456789abc"}],
             None, None),
        )
        for name, adapter, method, listing, detail_method, detail in fixtures:
            with self.subTest(source=name):
                self.set_api(adapter, method, listing)
                if detail_method:
                    self.set_api(adapter, detail_method, detail)
                cur = FakeCursor(["jooble_direct"])
                response, _ = self.request(name, cur)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["updated"], 2)
                self.assertEqual([write["title"] for write in cur.canonical_writes],
                                 ["Incoming title", "Incoming title"])
                self.assertEqual(len(cur.source_writes), 2)


if __name__ == "__main__":
    unittest.main()
