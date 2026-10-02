"""Lever regression tests: fake PostgreSQL and mocked public API responses only."""
import json
import sqlite3
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from app import dedupe, lever_ingestion as lever, main, merge
from app.adapters.lever import (
    LeverAdapter, LeverIdentity, parse_lever_url, source_job_id,
)
from app.canonical import canonical_field_updates
from app.ingestion_status import ALL_TENANTS_FAILED
from app.source_priority import SOURCE_PRIORITY
import test_stabilization as baseline


POSTING = "12345678-1234-1234-1234-123456789abc"
OTHER_POSTING = "12345678-1234-1234-1234-123456789def"


def payload(posting_id=POSTING, country="CZ", site="acme", instance="global"):
    host = "jobs.eu.lever.co" if instance == "eu" else "jobs.lever.co"
    return {
        "id": posting_id, "text": "Software Engineer", "country": country,
        "categories": {"location": "Prague", "allLocations": ["Prague"],
                       "commitment": "Fulltime", "team": "Engineering", "department": "Product"},
        "workplaceType": "hybrid", "descriptionPlain": "Opening and full body",
        "openingPlain": "Opening", "lists": [{"text": "Requirements", "content": "<li>Python</li>"}],
        "additionalPlain": "Benefits", "hostedUrl": f"https://{host}/{site}/{posting_id}",
        "applyUrl": f"https://{host}/{site}/{posting_id}/apply",
        "salaryRange": {"currency": "CZK", "interval": "month", "min": 0, "max": 100000},
        "salaryDescriptionPlain": "Negotiable", "createdAt": 1700000000000,
    }


def fantastic_row(job_id="fantastic-job", site="acme", instance="global", posting_id=POSTING):
    raw = payload(posting_id, site=site, instance=instance)
    return (job_id, "Acme", raw["hostedUrl"], raw["applyUrl"], raw["hostedUrl"], None)


class LeverCursor(baseline.FakeCursor):
    def __init__(self, rows=()):
        super().__init__(existing_source=False)
        self.fantastic_rows = list(rows)
        self.identities = {}

    def execute(self, query, params=()):
        query = query if isinstance(query, str) else query.as_string()
        normalized = " ".join(query.split())
        if normalized.startswith("select js.job_id, coalesce"):
            self.calls.append((normalized, params))
            self.rows = self.fantastic_rows
        elif normalized.startswith("select js.id, js.job_id"):
            self.calls.append((normalized, params))
            existing = self.identities.get(tuple(params))
            self.rows = [existing] if existing else []
        else:
            super().execute(query, params)
            if normalized.startswith("insert into public.job_sources"):
                self.identities[tuple(params[1:3])] = (f"source-{len(self.identities)}", params[0])


class LeverIdentityTests(baseline.OfflineTest):
    def test_global_and_eu_native_urls_and_apply_urls(self):
        for instance, host in (("global", "jobs.lever.co"), ("eu", "jobs.eu.lever.co")):
            for suffix in ("", "/apply", "/?utm=one#apply"):
                with self.subTest(instance=instance, suffix=suffix):
                    self.assertEqual(parse_lever_url(f"https://{host}/ACME/{POSTING}{suffix}"),
                                     LeverIdentity(instance, "acme", POSTING))
        self.assertEqual(parse_lever_url("https://jobs.eu.lever.co/acme"), LeverIdentity("eu", "acme", None))

    def test_invalid_hosts_paths_and_id_substrings_are_rejected(self):
        for url in (
            f"https://jobs.lever.co.evil.example/acme/{POSTING}",
            f"https://example.com/acme/{POSTING}",
            f"https://jobs.lever.co/acme/{POSTING}0",
            f"https://jobs.lever.co/acme/{POSTING}/other",
            f"https://user@jobs.lever.co/acme/{POSTING}",
            f"https://jobs.lever.co:444/acme/{POSTING}",
            "https://jobs.lever.co/acme/not-an-id", None,
        ):
            with self.subTest(url=url):
                self.assertIsNone(parse_lever_url(url))

    def test_source_identity_is_stable_and_instance_scoped(self):
        self.assertEqual(source_job_id("global", "ACME", POSTING.upper()), f"global:acme:{POSTING}")
        self.assertNotEqual(source_job_id("global", "acme", POSTING), source_job_id("eu", "acme", POSTING))
        for instance, site, native_id in (("other", "acme", POSTING), ("global", "../acme", POSTING), ("global", "acme", "missing")):
            with self.assertRaises(ValueError):
                source_job_id(instance, site, native_id)

    def test_discovery_and_attachment_execute_real_sql(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript("""create table companies(id text,name text);
            create table jobs(id text,company_id text);
            create table job_sources(job_id text,source_name text,source_url text,apply_url text,raw_payload text);
            insert into companies values ('company','Acme');
            insert into jobs values ('global-job','company'),('eu-job','company'),('jooble-job','company');""")
        for job_id, instance, source in (("global-job", "global", "fantastic_jobs_apify"),
                                         ("eu-job", "eu", "fantastic_jobs_apify"),
                                         ("jooble-job", "global", "jooble_direct")):
            raw = payload(instance=instance)
            db.execute("insert into job_sources values (?,?,?,?,?)", (job_id, source, raw["hostedUrl"], raw["applyUrl"],
                       json.dumps({"source": "lever.co", "organization": "Acme", "url": raw["hostedUrl"]})))
        class Cursor(baseline.SQLiteCursor):
            def execute(self, query, params=()):
                return super().execute(query.replace("ilike", "like"), params)
        cur = Cursor(db)
        self.assertEqual(lever.discover_sites(cur), [lever.LeverSite("eu", "acme", "Acme"), lever.LeverSite("global", "acme", "Acme")])
        self.assertEqual(lever.find_existing_lever_job(cur, "global", "acme", POSTING), "global-job")
        self.assertEqual(lever.find_existing_lever_job(cur, "eu", "acme", POSTING), "eu-job")

    def test_exact_attachment_zero_unique_ambiguous_and_wrong_scope(self):
        a = fantastic_row("a")
        for rows, expected in (([], None), ([a, a], "a"), ([a, fantastic_row("b")], None),
                               ([fantastic_row("b", site="other")], None),
                               ([fantastic_row("b", instance="eu")], None),
                               ([fantastic_row("b", posting_id=OTHER_POSTING)], None)):
            with self.subTest(rows=rows):
                self.assertEqual(lever.find_existing_lever_job(LeverCursor(rows), "global", "acme", POSTING), expected)

    def test_conflicting_native_urls_are_not_attachment_evidence(self):
        row = list(fantastic_row())
        row[3] = payload(instance="eu")["applyUrl"]
        self.assertIsNone(lever.find_existing_lever_job(LeverCursor([tuple(row)]), "global", "acme", POSTING))

    def test_site_selection_infers_eu_but_never_guesses_unknown_instance(self):
        eu = lever.LeverSite("eu", "acme", "Acme")
        global_site = lever.LeverSite("global", "acme", "Acme")
        self.assertEqual(lever.select_sites([eu], "ACME", None, None), [eu])
        with self.assertRaises(ValueError):
            lever.select_sites([eu, global_site], "acme", None, None)
        with self.assertRaises(ValueError):
            lever.select_sites([], "unknown", None, None)
        self.assertEqual(lever.select_sites([], "unknown", "eu", None), [lever.LeverSite("eu", "unknown", "unknown")])

    def test_priority_and_lower_source_protection_and_merge_registration(self):
        self.assertEqual(SOURCE_PRIORITY[lever.SOURCE_NAME], 300)
        self.assertIs(merge.SOURCE_PRIORITY, SOURCE_PRIORITY)
        self.assertIs(dedupe.SOURCE_PRIORITY, SOURCE_PRIORITY)
        self.assertEqual(canonical_field_updates({"title": "Lever"}, {"title": "Lower"}, "jooble_direct", [lever.SOURCE_NAME]), {})
        self.assertEqual(canonical_field_updates({"title": "Lower"}, {"title": "Lever"}, lever.SOURCE_NAME, ["fantastic_jobs_apify"]), {"title": "Lever"})

    def test_lever_is_eligible_for_generalized_dedupe_with_other_sources(self):
        fixture = baseline.DedupeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.job("a", [lever.SOURCE_NAME])
        fixture.job("b", ["fantastic_jobs_apify", "greenhouse_direct"])
        fixture.job("c", ["jooble_direct"])
        self.assertEqual(set(fixture.pairs()), {("a", "b"), ("a", "c"), ("b", "c")})


class LeverMappingTests(baseline.OfflineTest):
    def test_czech_structured_country_accepts_remote_without_city(self):
        self.assertTrue(lever.is_czech_job({"country": "CZ", "categories": {"location": "Remote"}, "workplaceType": "remote"}))

    def test_czech_locations_without_country(self):
        for location in ("Prague", "Praha", "Brno", "Plzeň", "Czechia", "Remote - Czech Republic", "CZ"):
            with self.subTest(location=location):
                self.assertTrue(lever.is_czech_job({"categories": {"location": location}}))

    def test_foreign_and_generic_remote_postings_are_rejected(self):
        for raw in (
            {"country": "US", "categories": {"location": "Prague", "allLocations": ["Prague"]}},
            {"country": "DE", "categories": {"location": "Berlin"}},
            {"categories": {"location": "Worldwide"}, "workplaceType": "remote"},
            {"categories": {"location": "Europe", "allLocations": ["EMEA", "Remote"]}, "workplaceType": "remote"},
            {"categories": {"location": "New Prague"}, "country": "US"}, {},
        ):
            with self.subTest(raw=raw):
                self.assertFalse(lever.is_czech_job(raw))

    def test_multilocation_czech_secondary_is_retained_and_projected(self):
        raw = {"country": "GB", "categories": {"location": "London", "allLocations": ["London", "Praha", "Brno"]}, "workplaceType": "remote"}
        self.assertTrue(lever.is_czech_job(raw))
        self.assertEqual(lever.build_location_text(raw), "Praha | Brno")

    def test_missing_locations_and_salary_are_not_invented(self):
        self.assertIsNone(lever.build_location_text({"country": "CZ"}))
        self.assertIsNone(lever.salary_text({}))
        self.assertIsNone(lever.build_description({}))
        self.assertEqual(lever.normalize_remote_type({}), "unknown")

    def test_description_salary_and_workplace_mapping(self):
        raw = payload()
        self.assertEqual(lever.build_description(raw), "Opening and full body\n\nRequirements\nPython\n\nBenefits")
        self.assertIn('"min": 0', lever.salary_text(raw))
        self.assertIn("Negotiable", lever.salary_text(raw))
        self.assertEqual(lever.normalize_remote_type({"workplaceType": "on-site"}), "onsite")
        self.assertEqual(lever.normalize_remote_type({"workplaceType": "onsite"}), "onsite")
        self.assertEqual(lever.build_description({"description": "<p>Hello</p><p>World</p>"}), "Hello World")


class LeverAdapterTests(baseline.OfflineTest):
    def request_pages(self, pages):
        mock = self.stack.enter_context(patch("app.adapters.lever.httpx.Client"))
        client = mock.return_value.__enter__.return_value
        client.get.side_effect = [httpx.Response(status, json=data) for status, data in pages]
        return client

    def test_global_eu_hosts_and_json_parameters(self):
        for instance, host in (("global", "api.lever.co"), ("eu", "api.eu.lever.co")):
            with self.subTest(instance=instance):
                client = self.request_pages([(200, [payload(instance=instance)])])
                self.assertEqual(len(LeverAdapter().list_postings("ACME", instance)), 1)
                self.assertEqual(client.get.call_args.args[0], f"https://{host}/v0/postings/acme")
                self.assertEqual(client.get.call_args.kwargs["params"], {"mode": "json", "limit": 100, "skip": 0})

    def test_pagination_loads_all_pages(self):
        client = self.request_pages([(200, [{"id": "one"}, {"id": "two"}]), (200, [{"id": "three"}])])
        adapter = LeverAdapter()
        adapter.PAGE_SIZE = 2
        self.assertEqual(len(adapter.list_postings("acme")), 3)
        self.assertEqual(client.get.call_args.kwargs["params"]["skip"], 2)

    def test_pagination_that_does_not_advance_fails(self):
        self.request_pages([(200, [{"id": "one"}]), (200, [{"id": "one"}])])
        adapter = LeverAdapter()
        adapter.PAGE_SIZE = 1
        with self.assertRaisesRegex(RuntimeError, "did not advance"):
            adapter.list_postings("acme")

    def test_http_errors_and_malformed_payloads_fail(self):
        for status, data in ((503, {}), (200, {}), (200, ["invalid"]), (200, None)):
            with self.subTest(status=status, data=data):
                self.request_pages([(status, data)])
                with self.assertRaises(RuntimeError):
                    LeverAdapter().list_postings("acme")


class LeverIngestionTests(baseline.OfflineTest):
    def setUp(self):
        super().setUp()
        self.api = self.stack.enter_context(patch.object(LeverAdapter, "list_postings", side_effect=AssertionError("External API forbidden")))
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def request(self, cur, request=None):
        conn = self.fake_database(cur)
        response = self.client.post("/ingest/ats/lever", json=request if request is not None else {"site": "acme", "instance": "global"})
        return response, conn

    def test_exact_attachment_persists_full_payload_and_updates_canonical(self):
        raw = payload()
        self.api.side_effect = None
        self.api.return_value = [raw]
        cur = LeverCursor([fantastic_row()])
        response, _ = self.request(cur)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["attached"], 1)
        self.assertEqual(response.json()["created"], 0)
        self.assertEqual(cur.source_writes[0][1][5].obj, raw)
        self.assertEqual(cur.canonical_writes[0]["title"], raw["text"])
        self.assertEqual(cur.canonical_writes[0]["employment_type"], "Fulltime")
        self.assertNotIn("published_at", cur.canonical_writes[0])
        self.assertEqual(cur.run_update["records_updated"], 1)

    def test_idempotent_rerun_updates_same_identity_without_new_source(self):
        self.api.side_effect = None
        self.api.return_value = [payload()]
        cur = LeverCursor()
        first, _ = self.request(cur)
        second, _ = self.request(cur)
        self.assertEqual(first.json()["created"], 1)
        self.assertEqual(second.json()["created"], 0)
        self.assertEqual(second.json()["updated"], 1)
        self.assertEqual(list(cur.identities), [(lever.SOURCE_NAME, f"global:acme:{POSTING}")])
        self.assertEqual(len(cur.source_writes), 2)
        self.assertTrue(cur.source_writes[1][0].startswith("update public.job_sources"))

    def test_ambiguous_attachment_creates_separate_canonical_job(self):
        self.api.side_effect = None
        self.api.return_value = [payload()]
        response, _ = self.request(LeverCursor([fantastic_row("a"), fantastic_row("b")]))
        self.assertEqual(response.json()["created"], 1)
        self.assertEqual(response.json()["attached"], 0)

    def test_all_sites_failed_returns_502_and_persists_failure(self):
        self.api.side_effect = RuntimeError("Fixture unavailable")
        cur = LeverCursor()
        response, conn = self.request(cur)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(len(response.json()["site_errors"]), 1)
        self.assertEqual(cur.run_update["error_message"], ALL_TENANTS_FAILED)
        self.assertEqual(cur.run_update["status"], "failed")
        self.assertEqual(conn.commit.call_count, 2)

    def test_partial_failure_is_success_with_per_site_errors(self):
        self.stack.enter_context(patch.object(lever, "discover_sites", return_value=[
            lever.LeverSite("eu", "one", "One"), lever.LeverSite("global", "two", "Two")]))
        self.api.side_effect = [RuntimeError("Fixture unavailable"), []]
        response, _ = self.request(LeverCursor(), {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["successful_sites"], 1)
        self.assertEqual(response.json()["site_errors"][0]["instance"], "eu")

    def test_valid_empty_and_non_czech_boards_are_successful(self):
        for jobs in ([], [{"country": "US", "categories": {"location": "Worldwide"}, "workplaceType": "remote"}]):
            with self.subTest(jobs=jobs):
                self.api.side_effect = None
                self.api.return_value = jobs
                response, _ = self.request(LeverCursor())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["czech_jobs"], 0)
                self.assertEqual(response.json()["successful_sites"], 1)

    def test_missing_id_or_title_and_conflicting_api_url_fail_unusable_site(self):
        for mutation in ({"id": None}, {"text": None}, {"hostedUrl": payload(instance="eu")["hostedUrl"]}):
            with self.subTest(mutation=mutation):
                self.api.side_effect = None
                self.api.return_value = [{**payload(), **mutation}]
                cur = LeverCursor()
                response, _ = self.request(cur)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["failed"], 1)
                self.assertEqual(cur.source_writes, [])

    def test_automatic_discovery_preserves_eu_instance(self):
        self.api.side_effect = None
        self.api.return_value = [payload(instance="eu")]
        cur = LeverCursor([fantastic_row(instance="eu")])
        response, _ = self.request(cur, {})
        self.assertEqual(response.status_code, 200)
        self.api.assert_called_once_with("acme", instance="eu")
        self.assertIn((lever.SOURCE_NAME, f"eu:acme:{POSTING}"), cur.identities)

    def test_missing_or_ambiguous_instance_returns_400_and_invalid_request_422(self):
        response, _ = self.request(LeverCursor(), {"site": "unknown"})
        self.assertEqual(response.status_code, 400)
        response, _ = self.request(LeverCursor([fantastic_row(), fantastic_row(instance="eu")]), {"site": "acme"})
        self.assertEqual(response.status_code, 400)
        for body in ({"instance": "invalid"}, {"site": "../bad"}, {"max_sites": 0}):
            self.assertEqual(self.client.post("/ingest/ats/lever", json=body).status_code, 422)
        self.api.assert_not_called()
