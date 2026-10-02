"""Offline Workday tests: mocked HTTP, fake PostgreSQL, real shared helpers."""
import copy
import json
import sqlite3
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from app import dedupe, main, merge, workday_ingestion as workday
from app.adapters.workday import (
    PAGE_SIZE, WorkdayAdapter, WorkdayAPIError, WorkdayDiscovery, WorkdaySite,
    clearly_foreign_listing, czech_evidence, czech_location, discover_czech_facet, discover_czech_facets,
    normalize_external_path, parse_workday_url, source_job_id,
)
from app.canonical import canonical_field_updates
from app.source_priority import SOURCE_PRIORITY
import test_stabilization as baseline


BOARD = WorkdaySite("acme.wd3.myworkdayjobs.com", "acme", "External_Careers", "Acme")
PATH = "/job/Prague/Software-Engineer_R123-1"
POSTING_ID = "1234567890abcdef1234567890abcdef"


def detail(country="CZ", additional=None, native_id=POSTING_ID, path=PATH):
    return {"jobPostingInfo": {
        "id": native_id, "title": "Software Engineer", "jobReqId": "R123",
        "jobPostingId": "Software-Engineer_R123-1", "jobPostingSiteId": BOARD.site,
        "jobDescription": "<p>Engineering &amp; testing</p>", "location": "Prague",
        "jobRequisitionLocation": {"country": {"alpha2Code": country, "descriptor": "Country"}},
        "additionalLocations": additional or [], "timeType": "Full time", "remoteType": "Hybrid",
        "startDate": "2026-10-01", "postedOn": "Posted Yesterday",
        "externalUrl": BOARD.public_base + path}, "hiringOrganization": {"descriptor": "Acme"}}


def listing(path=PATH):
    return {"title": "Software Engineer", "externalPath": path, "locationsText": "Prague", "bulletFields": ["R123"]}


def facet(parameter="locationCountry", descriptor="Country", name="Czechia"):
    return {"facetParameter": parameter, "descriptor": descriptor,
            "values": [{"id": "cz-value", "descriptor": name, "count": 3},
                       {"id": "us-value", "descriptor": "United States", "count": 100}]}


def page(count=0, start=0, total=0, facets=None):
    return {"total": total, "jobPostings": [listing(f"/job/Prague/Engineer_R{n}") for n in range(start, start + count)],
            "facets": facets or []}


def fantastic_row(job="fantastic-job", url=None):
    url = url or BOARD.public_base + PATH
    return (job, "Acme", url, url, url, None)


class WorkdayCursor(baseline.FakeCursor):
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


class WorkdayOfflineTest(baseline.OfflineTest):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("Real HTTP forbidden")))


class WorkdayIdentityTests(WorkdayOfflineTest):
    def test_myworkdayjobs_host_tenant_site_and_path(self):
        parsed = parse_workday_url(BOARD.public_base + PATH)
        self.assertEqual(parsed.board.scope, BOARD.scope)
        self.assertEqual(parsed.external_path, PATH)
        self.assertEqual(parsed.board.cluster, "wd3")
        self.assertEqual(parsed.board.api_base, "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External_Careers")

    def test_myworkdaysite_recruiting_url(self):
        parsed = parse_workday_url("https://wd103.myworkdaysite.com/recruiting/acme/External_Careers" + PATH)
        self.assertEqual(parsed.board.scope, ("wd103.myworkdaysite.com", "acme", "External_Careers"))
        self.assertEqual(parsed.external_path, PATH)
        self.assertEqual(parsed.board.family, "myworkdaysite")

    def test_case_sensitive_site_and_locale_preserved(self):
        parsed = parse_workday_url("https://acme.wd502.myworkdayjobs.com/cs-CZ/External_Careers" + PATH)
        self.assertEqual(parsed.board.site, "External_Careers")
        self.assertEqual(parsed.board.locale, "cs-CZ")
        self.assertEqual(parsed.board.cluster, "wd502")
        self.assertNotEqual(source_job_id(parsed.board, POSTING_ID),
                            source_job_id(WorkdaySite(parsed.board.host, "acme", "external_careers"), POSTING_ID))

    def test_tracking_apply_and_percent_encoding_normalize(self):
        for suffix in ("", "/", "/apply", "?source=one#apply"):
            self.assertEqual(parse_workday_url(BOARD.public_base + PATH + suffix).external_path, PATH)
        self.assertEqual(normalize_external_path("/job/Prague/Software%2DEngineer_R123-1"), PATH)

    def test_observed_locationless_shsjb_path(self):
        path = "/job/Senior-Director--Legal---Employee-Benefits_R-31048"
        self.assertEqual(normalize_external_path(path), path)
        self.assertEqual(parse_workday_url("https://onehealthineers.wd3.myworkdayjobs.com/SHSJB" + path).external_path, path)
        self.assertEqual(normalize_external_path(path + "/apply?source=test#apply"), path)

    def test_locationless_paths_keep_security_validation(self):
        for path in ("/job/", "/job/apply", "/job/..", "/job/%2Fescape", "/job/%5Cescape",
                     "/job/%00escape", "https://evil.example/job/posting", "//evil.example/job/posting"):
            self.assertIsNone(normalize_external_path(path))

    def test_unsafe_host_components_and_paths_rejected(self):
        for url in ("https://acme.wd3.myworkdayjobs.com.evil.example/External_Careers" + PATH,
                    "https://user@acme.wd3.myworkdayjobs.com/External_Careers" + PATH,
                    "http://acme.wd3.myworkdayjobs.com/External_Careers" + PATH,
                    "https://acme.wd3.myworkdayjobs.com:444/External_Careers" + PATH,
                    BOARD.public_base + "/job/%2E%2E/Engineer", BOARD.public_base + "/job/Prague/%2FEngineer"):
            with self.subTest(url=url):
                self.assertIsNone(parse_workday_url(url))
        with self.assertRaises(ValueError):
            WorkdaySite(BOARD.host, "other", BOARD.site)
        self.assertIsNone(normalize_external_path("//evil.example/job/Prague/Engineer"))

    def test_native_identity_stable_not_requisition_or_url_token(self):
        self.assertEqual(source_job_id(BOARD, POSTING_ID.upper()), f"acme:External_Careers:{POSTING_ID}")
        for value in (None, "R123", "R123-1", "Software-Engineer_R123-1", "a" * 31):
            with self.assertRaises(ValueError):
                source_job_id(BOARD, value)

    def test_discovery_uses_parsed_scopes_without_registry(self):
        rows = [fantastic_row(), fantastic_row("another"),
                fantastic_row("third", "https://wd12.myworkdaysite.com/recruiting/other/Jobs" + PATH)]
        boards = workday.discover_sites(rows)
        self.assertEqual(len(boards), 2)
        self.assertEqual({board.scope for board in boards}, {BOARD.scope, ("wd12.myworkdaysite.com", "other", "Jobs")})
        self.assertEqual(workday.select_sites(boards, BOARD.host, "acme", BOARD.site), [boards[0]])
        with self.assertRaises(ValueError):
            workday.select_sites(boards, host=BOARD.host)

    def test_exact_path_attachment_zero_unique_and_ambiguous(self):
        for rows, expected in (([], None), ([fantastic_row()], "fantastic-job"),
                               ([fantastic_row(), fantastic_row()], "fantastic-job"),
                               ([fantastic_row(), fantastic_row("other")], None),
                               ([fantastic_row(url=BOARD.public_base + PATH.replace("-1", "-2"))], None)):
            with self.subTest(rows=rows):
                index = workday.build_attachment_index(rows)
                self.assertEqual(workday.find_existing_workday_job(index, BOARD, PATH), expected)

    def test_attachment_scopes_host_tenant_site_and_case(self):
        index = workday.build_attachment_index([fantastic_row()])
        for board in (WorkdaySite("acme.wd5.myworkdayjobs.com", "acme", BOARD.site),
                      WorkdaySite(BOARD.host, "acme", "external_careers"),
                      WorkdaySite("other.wd3.myworkdayjobs.com", "other", BOARD.site)):
            self.assertIsNone(workday.find_existing_workday_job(index, board, PATH))
        self.assertIsNone(workday.find_existing_workday_job(index, BOARD, PATH.lower()))

    def test_conflicting_fantastic_paths_cannot_attach(self):
        row = list(fantastic_row())
        row[3] = BOARD.public_base + PATH.replace("-1", "-2")
        self.assertEqual(workday.build_attachment_index([row]), {})

    def test_fantastic_discovery_executes_real_sql(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript("""create table companies(id text,name text); create table jobs(id text,company_id text);
            create table job_sources(job_id text,source_name text,source_url text,apply_url text,raw_payload text);
            insert into companies values ('company','Acme'); insert into jobs values ('job','company');""")
        url = BOARD.public_base + PATH
        for source in ("fantastic_jobs_apify", "jooble_direct"):
            db.execute("insert into job_sources values (?,?,?,?,?)",
                ("job", source, url, url, json.dumps({"source": "workday", "url": url})))
        class Cursor(baseline.SQLiteCursor):
            def execute(self, query, params=()):
                return super().execute(query.replace("ilike", "like"), params)
        rows = workday.fantastic_workday_rows(Cursor(db))
        self.assertEqual(len(rows), 1)
        self.assertEqual(workday.discover_sites(rows)[0].scope, BOARD.scope)

    def test_shared_priority_merge_dedupe_and_canonical_protection(self):
        self.assertEqual(SOURCE_PRIORITY[workday.SOURCE_NAME], 300)
        self.assertIs(merge.SOURCE_PRIORITY, SOURCE_PRIORITY)
        self.assertIs(dedupe.SOURCE_PRIORITY, SOURCE_PRIORITY)
        self.assertEqual(canonical_field_updates({"title": "Workday"}, {"title": "Lower"},
            "fantastic_jobs_apify", [workday.SOURCE_NAME]), {})
        self.assertEqual(canonical_field_updates({"title": "Lower"}, {"title": "Workday"},
            workday.SOURCE_NAME, ["fantastic_jobs_apify"]), {"title": "Workday"})

    def test_workday_participates_in_generic_dedupe(self):
        fixture = baseline.DedupeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.job("a", [workday.SOURCE_NAME, "fantastic_jobs_apify"])
        fixture.job("b", ["lever_direct"])
        fixture.job("c", ["jooble_direct"])
        self.assertEqual(set(fixture.pairs()), {("a", "b"), ("a", "c"), ("b", "c")})


class WorkdayGeographyTests(WorkdayOfflineTest):
    def test_primary_country_cz(self):
        self.assertEqual(czech_evidence(detail()), {"kind": "primary_country", "country_code": "CZ"})

    def test_czech_secondary_location_with_foreign_primary(self):
        for location in ("Prague", "Praha, Česká republika", "FTN-CZE-Brno", "Remote, Czech Republic"):
            with self.subTest(location=location):
                self.assertEqual(czech_evidence(detail("US", [location]))["kind"], "additional_location")

    def test_foreign_only_and_generic_remote_rejected(self):
        for locations in ([], ["Berlin, Germany"], ["Remote"], ["Europe", "EU", "EMEA", "Worldwide"]):
            self.assertIsNone(czech_evidence(detail("US", locations)))
        self.assertFalse(czech_location("Prague, USA"))

    def test_url_slug_title_and_primary_city_cannot_supply_eligibility(self):
        raw = detail("US")
        raw["jobPostingInfo"]["title"] = "Czech remote role"
        self.assertIsNone(czech_evidence(raw))

    def test_dynamic_country_facet_names_and_nested_hierarchies(self):
        for name in ("locationCountry", "Location_Country", "Country", "locationHierarchy2"):
            facets = [{"facetParameter": "locationMainGroup", "values": [facet(name)]}]
            selected = discover_czech_facet(facets)
            self.assertEqual(selected["parameter"], name)
            self.assertEqual(selected["ids"], ["cz-value"])

    def test_country_preferred_over_locations_without_intersection(self):
        selected = discover_czech_facet([facet("Locations", "Locations", "Prague"), facet()])
        self.assertEqual(selected["parameter"], "locationCountry")

    def test_location_facet_and_multiple_czech_values(self):
        values = facet("primaryLocation", "Location", "Praha")
        values["values"].append({"id": "brno", "descriptor": "Brno", "count": 2})
        self.assertEqual(discover_czech_facet([values])["ids"], ["cz-value", "brno"])

    def test_no_geography_or_no_czech_value_uses_fallback(self):
        self.assertIsNone(discover_czech_facet([]))
        self.assertIsNone(discover_czech_facet([facet("language", "Language", "Czechia")]))
        self.assertIsNone(discover_czech_facet([facet(name="Germany")]))

    def test_fallback_only_prunes_explicit_foreign_single_location(self):
        for location in ("Berlin, Germany", "Prague, USA", "Tokyo, Japan"):
            self.assertTrue(clearly_foreign_listing({"locationsText": location}))
        for location in ("Prague", "2 Locations", "London", "Remote", "Europe", "Unknown"):
            self.assertFalse(clearly_foreign_listing({"locationsText": location}))


class WorkdayAPITests(WorkdayOfflineTest):
    def adapter(self, responses, max_pages=1000):
        client = MagicMock()
        client.request.side_effect = [httpx.Response(status, json=data) for status, data in responses]
        return WorkdayAdapter(client, max_pages=max_pages), client

    def test_observed_city_dimension_survives_empty_nested_country_dimension(self):
        pilsen = {"id": "e54b6f0977e910010e35e6b534eb0000", "descriptor": "Pilsen", "count": 22}
        facets = [
            {"facetParameter": "locationHierarchy", "descriptor": "City", "values": [pilsen]},
            {"facetParameter": "locationMainGroup", "values": [
                {"facetParameter": "locationHierarchy1", "descriptor": "Country", "values": [pilsen]}]},
        ]
        known = ["/job/CZ-DOUDLEVCE-PILSEN-EDVARDA-BENESE-56439/ASPIRE---Supply-Chain---2027_R169537",
                 "/job/CZ-DOUDLEVCE-PILSEN-EDVARDA-BENESE-56439/Assembler-Generator-winder_R149704"]
        selected = page(2)
        selected["jobPostings"] = [listing(path) for path in known]
        adapter, client = self.adapter([(200, page(facets=facets)), (200, page()), (200, selected)])
        result = adapter.discover_candidates(BOARD)
        self.assertEqual({job["externalPath"] for job in result.jobs}, set(known))
        self.assertEqual(result.counters["facet_queries"], 2)
        applied = [call.kwargs["json"]["appliedFacets"] for call in client.request.call_args_list[1:]]
        self.assertEqual(applied, [{"locationHierarchy1": [pilsen["id"]]}, {"locationHierarchy": [pilsen["id"]]}])

    def test_observed_country_and_city_siblings_all_selected(self):
        values = [
            {"id": "54c59631019f01ace6587a87a3778735", "descriptor": "Czech Republic", "count": 3},
            {"id": "54c59631019f013e9231c717a777b36e", "descriptor": "Prague", "count": 3},
            {"id": "68d4fcd5904c1000e6bd2f6f479f0000", "descriptor": "Brno", "count": 1},
            {"id": "foreign", "descriptor": "Berlin, Germany", "count": 50},
        ]
        node = {"facetParameter": "locations", "descriptor": "Locations", "values": values}
        facets = [node, {"facetParameter": "locationMainGroup", "values": [copy.deepcopy(node)]}]
        queries = discover_czech_facets(facets)
        self.assertEqual(queries, [{"parameter": "locations", "ids": [v["id"] for v in values[:3]],
                                   "descriptors": [v["descriptor"] for v in values[:3]]}])
        records = page(7)
        records["jobPostings"][0] = listing("/job/Prague/Supply-Chain-Specialist---Customer-Support-Specialist--Maternity-cover-_202609-124221")
        records["jobPostings"][1] = listing("/job/Prague/Local-Safety-Officer--1-year-contract--flexible-working-hours-_202609-124513")
        adapter, client = self.adapter([(200, page(facets=facets)), (200, records)])
        result = adapter.discover_candidates(BOARD)
        self.assertEqual(len(result.jobs), 7)
        self.assertEqual(result.counters["facet_queries"], 1)
        self.assertEqual(client.request.call_args_list[1].kwargs["json"]["appliedFacets"],
                         {"locations": [v["id"] for v in values[:3]]})

    def test_multi_dimension_union_deduplicates_paths_before_details(self):
        facets = [facet(), facet("primaryLocation", "Location", "Prague")]
        adapter, client = self.adapter([(200, page(facets=facets)), (200, page(2)), (200, page(2, start=1))])
        result = adapter.discover_candidates(BOARD)
        self.assertEqual(len(result.jobs), 3)
        self.assertEqual(result.counters["facet_candidates"], 3)
        self.assertEqual(result.counters["facet_listing_records"], 4)
        self.assertTrue(all(len(call.kwargs["json"]["appliedFacets"]) == 1
                            for call in client.request.call_args_list[1:]))

    def test_each_dimension_has_independent_pagination_seen_set(self):
        facets = [facet(), facet("primaryLocation", "Location", "Prague")]
        adapter, _ = self.adapter([(200, page(facets=facets)), (200, page(20)), (200, page()),
                                   (200, page(20)), (200, page())])
        self.assertEqual(len(adapter.discover_candidates(BOARD).jobs), 20)

    def test_normal_single_facet_has_one_query_and_unchanged_cost(self):
        adapter, client = self.adapter([(200, page(facets=[facet()])), (200, page(3))])
        result = adapter.discover_candidates(BOARD)
        self.assertEqual((len(result.jobs), client.request.call_count, result.counters["facet_queries"]), (3, 2, 1))

    def test_geography_union_can_discover_secondary_location_but_details_decide(self):
        facets = [facet(), facet("locations", "Locations", "Prague")]
        adapter, _ = self.adapter([(200, page(facets=facets)), (200, page()), (200, page(1))])
        self.assertEqual(len(adapter.discover_candidates(BOARD).jobs), 1)
        self.assertEqual(czech_evidence(detail("DE", ["Prague, Czech Republic"]))["kind"], "additional_location")
        self.assertIsNone(czech_evidence(detail("DE", ["Europe", "EMEA", "Remote", "Worldwide"])))

    def test_foreign_and_generic_remote_facet_values_are_not_selected(self):
        self.assertEqual(discover_czech_facets([facet(name=name) for name in
                         ("Germany", "Remote", "Europe", "EMEA", "Worldwide")]), [])

    def test_pagination_ignores_misleading_total(self):
        adapter, client = self.adapter([(200, page(20, total=1)), (200, page(3, start=20, total=0))])
        result = adapter.discover_candidates(BOARD)
        self.assertEqual(len(result.jobs), 23)
        self.assertEqual([call.kwargs["json"]["offset"] for call in client.request.call_args_list], [0, 20])
        self.assertTrue(all(call.kwargs["json"]["limit"] == PAGE_SIZE for call in client.request.call_args_list))

    def test_full_final_page_requires_empty_page(self):
        adapter, _ = self.adapter([(200, page(20)), (200, page())])
        self.assertEqual(len(adapter.discover_candidates(BOARD).jobs), 20)
        self.assertEqual(adapter.counters["listing_calls"], 2)

    def test_stalled_page_rejected_even_when_reordered(self):
        first = page(20)
        second = copy.deepcopy(first)
        second["jobPostings"].reverse()
        adapter, _ = self.adapter([(200, first), (200, second)])
        with self.assertRaises(WorkdayAPIError) as error:
            adapter.discover_candidates(BOARD)
        self.assertEqual(error.exception.kind, "stalled")

    def test_medtronic_out_of_range_reset_requires_confirmed_tail(self):
        first = page(20, total=20)
        adapter, client = self.adapter([(200, page(20, facets=[facet()])), (200, first),
                                       (200, first), (200, page(10, start=10, total=0))])
        result = adapter.discover_candidates(BOARD)
        self.assertEqual(len(result.jobs), 20)
        self.assertEqual(result.counters["pagination_boundary_confirmed"], 1)
        self.assertEqual([c.kwargs["json"]["offset"] for c in client.request.call_args_list], [0, 0, 20, 10])
        self.assertTrue(all(c.kwargs["json"]["limit"] == 20 for c in client.request.call_args_list))

    def test_real_ignored_offset_still_fails_even_if_total_says_twenty(self):
        first = page(20, total=20)
        adapter, _ = self.adapter([(200, first), (200, first), (200, first)])
        with self.assertRaises(WorkdayAPIError) as error:
            adapter.discover_candidates(BOARD)
        self.assertEqual(error.exception.kind, "stalled")

    def test_boundary_tail_must_match_exact_previous_tail(self):
        first = page(20, total=20)
        for tail in (page(10, start=0), page(9, start=10), page(10, start=30)):
            adapter, _ = self.adapter([(200, first), (200, first), (200, tail)])
            with self.assertRaises(WorkdayAPIError):
                adapter.discover_candidates(BOARD)

    def test_repeat_before_reported_boundary_is_not_completion(self):
        first = page(20, total=40)
        adapter, client = self.adapter([(200, first), (200, first)])
        with self.assertRaises(WorkdayAPIError):
            adapter.discover_candidates(BOARD)
        self.assertEqual(client.request.call_count, 2)

    def test_multi_page_boundary_confirmation_does_not_use_later_total(self):
        first, second = page(20, total=40), page(20, start=20, total=0)
        adapter, _ = self.adapter([(200, first), (200, second), (200, first),
                                   (200, page(10, start=30, total=0))])
        self.assertEqual(len(adapter.discover_candidates(BOARD).jobs), 40)

    def test_locationless_detail_and_exact_attachment(self):
        path = "/job/Senior-Director--Legal---Employee-Benefits_R-31048"
        raw = detail(path=path)
        adapter, _ = self.adapter([(200, raw)])
        self.assertEqual(adapter.get_detail(BOARD, path), raw)
        index = workday.build_attachment_index([fantastic_row(url=BOARD.public_base + path)])
        self.assertEqual(workday.find_existing_workday_job(index, BOARD, path), "fantastic-job")

    def test_observed_ghr_pathless_stub_skipped_and_counted(self):
        first = page(19)
        first["jobPostings"].insert(5, {"bulletFields": ["26031902"]})
        adapter, client = self.adapter([(200, first), (200, page(1, start=20))])
        result = adapter.discover_candidates(BOARD)
        self.assertEqual(len(result.jobs), 20)
        self.assertEqual(result.counters["listing_missing_path"], 1)
        self.assertEqual(result.counters["listing_records"], 21)
        self.assertEqual(client.request.call_args_list[1].kwargs["json"]["offset"], 20)

    def test_repeated_all_pathless_page_still_stalls(self):
        stub_page = {"jobPostings": [{"bulletFields": ["26031902"]}] * 20}
        adapter, _ = self.adapter([(200, stub_page), (200, stub_page)])
        with self.assertRaises(WorkdayAPIError) as error:
            adapter.discover_candidates(BOARD)
        self.assertEqual(error.exception.kind, "stalled")

    def test_empty_string_or_unsafe_supplied_path_is_not_a_missing_path(self):
        for path in ("", "https://evil.example/job/posting", "/job/%2Fescape"):
            data = {"jobPostings": [listing(path)]}
            adapter, _ = self.adapter([(200, data)])
            with self.assertRaises(WorkdayAPIError):
                adapter.discover_candidates(BOARD)

    def test_redirect_not_followed_without_evidence_of_safe_target(self):
        adapter, client = self.adapter([(302, {})])
        with self.assertRaises(WorkdayAPIError) as error:
            adapter.get_detail(BOARD, PATH)
        self.assertEqual(error.exception.http_status, 302)
        self.assertEqual(client.request.call_count, 1)

    def test_overlap_deduplicated_and_offset_uses_actual_page_length(self):
        adapter, client = self.adapter([(200, page(20)), (200, page(20, start=10)), (200, page())])
        self.assertEqual(len(adapter.discover_candidates(BOARD).jobs), 30)
        self.assertEqual([call.kwargs["json"]["offset"] for call in client.request.call_args_list], [0, 20, 40])

    def test_page_limit_explicit_failure_not_silent_truncation(self):
        adapter, _ = self.adapter([(200, page(20))], max_pages=1)
        with self.assertRaises(WorkdayAPIError) as error:
            adapter.discover_candidates(BOARD)
        self.assertEqual(error.exception.kind, "page_limit")

    def test_geography_filter_requeries_first_page_before_details(self):
        adapter, client = self.adapter([(200, page(20, facets=[facet()])), (200, page(2))])
        result = adapter.discover_candidates(BOARD)
        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(result.counters["facet_candidates"], 2)
        self.assertEqual(client.request.call_args_list[1].kwargs["json"]["appliedFacets"], {"locationCountry": ["cz-value"]})

    def test_fallback_traversal_reuses_initial_page(self):
        first = page(2)
        first["jobPostings"][0]["locationsText"] = "Berlin, Germany"
        adapter, client = self.adapter([(200, first)])
        result = adapter.discover_candidates(BOARD)
        self.assertIsNone(result.geography)
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.counters["fallback_skipped_foreign"], 1)
        self.assertEqual(client.request.call_count, 1)

    def test_empty_catalog_successful_discovery(self):
        adapter, _ = self.adapter([(200, page())])
        self.assertEqual(adapter.discover_candidates(BOARD).jobs, [])

    def test_403_s22_separate_from_404(self):
        for status, kind, code in ((403, "forbidden", "S22"), (404, "unavailable", None)):
            adapter, _ = self.adapter([(status, {"errorCode": code})])
            with self.assertRaises(WorkdayAPIError) as error:
                adapter.get_detail(BOARD, PATH)
            self.assertEqual(error.exception.kind, kind)
            self.assertEqual(error.exception.http_status, status)
            self.assertEqual(error.exception.error_code, code)

    def test_missing_identity_never_falls_back_to_req_or_url(self):
        for identity in (None, "R123", "not-a-native-id"):
            adapter, _ = self.adapter([(200, detail(native_id=identity))])
            with self.assertRaises(WorkdayAPIError) as error:
                adapter.get_detail(BOARD, PATH)
            self.assertEqual(error.exception.kind, "missing_identity")

    def test_malformed_response_and_listing_path_rejected(self):
        for data in ([], {}, {"jobPostingInfo": []}):
            adapter, _ = self.adapter([(200, data)])
            with self.assertRaises(WorkdayAPIError):
                adapter.get_detail(BOARD, PATH)
        data = page(1)
        data["jobPostings"][0]["externalPath"] = "https://evil.example/job/one/two"
        adapter, _ = self.adapter([(200, data)])
        with self.assertRaises(WorkdayAPIError):
            adapter.discover_candidates(BOARD)

    def test_detail_identity_scope_and_full_payload(self):
        raw = detail()
        adapter, _ = self.adapter([(200, raw)])
        self.assertEqual(adapter.get_detail(BOARD, PATH), raw)
        raw["jobPostingInfo"]["externalUrl"] = BOARD.public_base + PATH.replace("-1", "-2")
        adapter, _ = self.adapter([(200, raw)])
        with self.assertRaises(WorkdayAPIError):
            adapter.get_detail(BOARD, PATH)

    def test_detail_site_mismatch_rejected(self):
        raw = detail()
        raw["jobPostingInfo"]["jobPostingSiteId"] = "Other_Careers"
        adapter, _ = self.adapter([(200, raw)])
        with self.assertRaises(WorkdayAPIError):
            adapter.get_detail(BOARD, PATH)

    def test_transient_http_and_transport_failures_reported(self):
        adapter, _ = self.adapter([(503, {})])
        with self.assertRaises(WorkdayAPIError) as error:
            adapter.get_detail(BOARD, PATH)
        self.assertEqual(error.exception.kind, "transient")
        client = MagicMock()
        client.request.side_effect = httpx.ReadTimeout("test")
        with self.assertRaises(WorkdayAPIError):
            WorkdayAdapter(client).get_detail(BOARD, PATH)


class WorkdayIngestionTests(WorkdayOfflineTest):
    def setup_ingestion(self, rows=(), raw=None, jobs=None):
        cursor = WorkdayCursor(rows)
        conn = self.fake_database(cursor)
        factory = self.stack.enter_context(patch.object(workday, "WorkdayAdapter"))
        adapter = factory.return_value.__enter__.return_value
        records = [listing()] if jobs is None else jobs
        counters = dict.fromkeys(WorkdayAdapter(client=MagicMock()).counters, 0)
        counters.update(listing_calls=2, listing_records=len(records), candidate_records=len(records), facet_candidates=len(records))
        adapter.discover_candidates.return_value = WorkdayDiscovery(records, counters, {"parameter": "Country", "ids": ["cz"]})
        adapter.get_detail.return_value = raw or detail()
        return cursor, conn, adapter

    def ingest(self):
        return workday.ingest_workday_jobs(host=BOARD.host, tenant=BOARD.tenant, site=BOARD.site)

    def test_new_job_creation_full_payload_and_evidence(self):
        cursor, _, _ = self.setup_ingestion()
        result = self.ingest()
        self.assertEqual((result["created"], result["attached"], result["failed"]), (1, 0, 0))
        params = cursor.source_writes[0][1]
        self.assertEqual(params[2], source_job_id(BOARD, POSTING_ID))
        self.assertEqual(params[5].obj["detail"], detail())
        self.assertEqual(params[5].obj["listing"], listing())
        self.assertEqual(params[5].obj["czech_eligibility"]["kind"], "primary_country")
        self.assertEqual(params[5].obj["detail"]["jobPostingInfo"]["jobReqId"], "R123")

    def test_deterministic_attachment_uses_canonical_helper(self):
        cursor, _, _ = self.setup_ingestion([fantastic_row()])
        result = self.ingest()
        self.assertEqual((result["created"], result["attached"]), (0, 1))
        self.assertEqual(cursor.source_writes[0][1][0], "fantastic-job")
        self.assertEqual(cursor.canonical_writes[0]["title"], "Software Engineer")

    def test_ambiguous_attachment_creates_separate_canonical(self):
        _, _, _ = self.setup_ingestion([fantastic_row(), fantastic_row("other")])
        result = self.ingest()
        self.assertEqual((result["created"], result["attached"]), (1, 0))

    def test_idempotent_source_identity_updates_existing(self):
        cursor, _, _ = self.setup_ingestion([fantastic_row()])
        first = self.ingest()
        second = self.ingest()
        self.assertEqual(first["attached"], 1)
        self.assertEqual((second["created"], second["attached"], second["updated"]), (0, 0, 1))
        self.assertEqual(len(cursor.identities), 1)

    def test_empty_site_is_success_and_requests_no_details(self):
        _, _, adapter = self.setup_ingestion(jobs=[])
        self.assertEqual(self.ingest()["status"], "success")
        adapter.get_detail.assert_not_called()

    def test_only_pathless_records_are_failed_not_empty_site_success(self):
        _, _, adapter = self.setup_ingestion(jobs=[])
        adapter.discover_candidates.return_value.counters["listing_missing_path"] = 1
        result = self.ingest()
        self.assertEqual((result["status"], result["failed"], result["listing_missing_path"]), ("failed", 1, 1))
        adapter.get_detail.assert_not_called()

    def test_pathless_stub_partial_failure_is_visible_with_usable_data(self):
        _, _, adapter = self.setup_ingestion(raw=detail("US"))
        adapter.discover_candidates.return_value.counters["listing_missing_path"] = 1
        result = self.ingest()
        self.assertEqual((result["status"], result["failed"], result["listing_missing_path"]), ("success", 1, 1))

    def test_foreign_only_details_are_success_without_writes(self):
        cursor, _, _ = self.setup_ingestion(raw=detail("US"))
        result = self.ingest()
        self.assertEqual((result["status"], result["czech_jobs"]), ("success", 0))
        self.assertEqual(cursor.source_writes, [])

    def test_all_403_s22_fail_without_expiring_or_writing_jobs(self):
        cursor, _, adapter = self.setup_ingestion()
        adapter.get_detail.side_effect = WorkdayAPIError("forbidden", "HTTP 403 / S22", 403, "S22")
        result = self.ingest()
        self.assertEqual((result["status"], result["forbidden_403"], result["s22"]), ("failed", 1, 1))
        self.assertEqual(cursor.source_writes, [])
        self.assertEqual(cursor.canonical_writes, [])
        self.assertEqual(cursor.run_update["status"], "failed")

    def test_all_current_details_404_not_site_failure(self):
        _, _, adapter = self.setup_ingestion()
        adapter.get_detail.side_effect = WorkdayAPIError("unavailable", "HTTP 404", 404)
        result = self.ingest()
        self.assertEqual((result["status"], result["unavailable_404"], result["failed"]), ("success", 1, 0))

    def test_missing_identity_all_failed_no_source_rows(self):
        cursor, _, adapter = self.setup_ingestion()
        adapter.get_detail.side_effect = WorkdayAPIError("missing_identity", "Missing native ID")
        result = self.ingest()
        self.assertEqual((result["status"], result["missing_identity"]), ("failed", 1))
        self.assertEqual(cursor.source_writes, [])

    def test_all_listing_sites_failed_http_502(self):
        cursor, _, adapter = self.setup_ingestion()
        adapter.counters = dict.fromkeys(WorkdayAdapter(client=MagicMock()).counters, 0)
        adapter.discover_candidates.side_effect = WorkdayAPIError("transient", "API unavailable")
        with TestClient(main.app) as client:
            response = client.post("/ingest/ats/workday", json={"host": BOARD.host, "tenant": "acme", "site": BOARD.site})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(cursor.run_update["status"], "failed")

    def test_partial_site_failure_keeps_success_and_errors(self):
        other = fantastic_row("other", "https://other.wd3.myworkdayjobs.com/Jobs" + PATH)
        _, _, adapter = self.setup_ingestion([fantastic_row(), other])
        good = adapter.discover_candidates.return_value
        adapter.counters = dict.fromkeys(WorkdayAdapter(client=MagicMock()).counters, 0)
        adapter.discover_candidates.side_effect = [WorkdayAPIError("transient", "API unavailable"),
                                                 WorkdayDiscovery([], good.counters, None)]
        result = workday.ingest_workday_jobs()
        self.assertEqual((result["status"], result["successful_sites"], len(result["site_errors"])), ("success", 1, 1))

    def test_duplicate_native_id_one_source_write(self):
        cursor, _, _ = self.setup_ingestion(jobs=[listing(), listing(PATH.replace("-1", "-2"))])
        result = self.ingest()
        self.assertEqual((result["created"], result["duplicate_native_ids"]), (1, 1))
        self.assertEqual(len(cursor.source_writes), 1)

    def test_request_rejects_partial_or_nonpublic_scope_without_db(self):
        with TestClient(main.app) as client:
            for body in ({"host": BOARD.host}, {"host": "internal.example", "tenant": "acme", "site": "Jobs"}):
                self.assertEqual(client.post("/ingest/ats/workday", json=body).status_code, 400)
            self.assertEqual(client.post("/ingest/ats/workday", json={"max_sites": 0}).status_code, 422)
        self.connect.assert_not_called()

    def test_mapping_absent_fields_not_invented(self):
        self.assertIsNone(workday.remote_type(None))
        self.assertIsNone(workday.published_at("Posted Yesterday"))
        self.assertEqual(workday.remote_type("Fully Remote"), "remote")
        self.assertEqual(workday.description_text("<p>A &amp; B</p>"), "A & B")

    def test_detail_errors_capped_but_counters_complete(self):
        _, _, adapter = self.setup_ingestion(jobs=page(15)["jobPostings"])
        adapter.get_detail.side_effect = WorkdayAPIError("forbidden", "HTTP 403 / S22", 403, "S22")
        result = self.ingest()
        self.assertEqual((result["details_requested"], result["s22"], result["failed"]), (15, 15, 15))
        self.assertEqual(len(result["site_results"][0]["detail_errors"]), 10)

    def test_403_listing_is_counted_as_real_site_failure(self):
        _, _, adapter = self.setup_ingestion()
        adapter.counters = dict.fromkeys(WorkdayAdapter(client=MagicMock()).counters, 0)
        adapter.discover_candidates.side_effect = WorkdayAPIError("forbidden", "HTTP 403 / S22", 403, "S22")
        result = self.ingest()
        self.assertEqual((result["status"], result["forbidden_403"], result["s22"]), ("failed", 1, 1))

    def test_absent_remote_date_preserves_existing_canonical_values(self):
        raw = detail()
        raw["jobPostingInfo"].pop("remoteType")
        raw["jobPostingInfo"].pop("startDate")
        cursor, _, _ = self.setup_ingestion([fantastic_row()], raw=raw)
        self.ingest()
        self.assertNotIn("remote_type", cursor.canonical_writes[0])
        self.assertNotIn("published_at", cursor.canonical_writes[0])
