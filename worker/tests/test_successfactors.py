"""Offline RMK contracts. All HTTP uses MockTransport; PostgreSQL uses fakes."""
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app import dedupe, main, merge, successfactors_ingestion as ingestion
from app.adapters.successfactors import (
    RMKConfig, RMKError, RMKSite, SuccessFactorsAdapter, classify_platform,
    configuration, czech_evidence, geography_queries, parse_detail, parse_listing,
    parse_rmk_url, public_url, source_job_id,
    clearly_foreign_listing,
)
from app.canonical import canonical_field_updates
from app.source_priority import SOURCE_PRIORITY
from app.successfactors_scopes import ScopeGate, apply_scope_gate, load_scope_gate
import test_stabilization as baseline


HOST = "careers.example.com"
ROOT = f"https://{HOST}"


def native(posting="123", countries=("CZ",), locations=("Praha",), tenant="ACME", locale="cs_CZ", brand=""):
    # Structures copied from the live discovery, with synthetic identities,
    # no third-party prose, session credentials or archived CSRF values.
    addresses = "".join(f'<div itemscope itemtype="https://schema.org/PostalAddress">'
                        f'<meta itemprop="addressCountry" content="{country}">'
                        f'<span itemprop="streetAddress">{location}<style>.junk {{ location:CZ }}</style></span></div>'
                        for country, location in zip(countries, locations))
    prefix = "/" + brand if brand else ""
    return f'''<html lang="{locale.replace('_', '-')}"><head>
        <link rel="canonical" href="{ROOT}{prefix}/job/Prague-Engineer/{posting}/"></head><body>
        <form name="keywordsearch" method="get" action="{prefix}/search/"></form>
        <script>j2w.init({{"ssoCompanyId":"{tenant}"}});
        j2w.Apply.init({{jobID:{posting},locale:"{locale}",applyWithLinkedIn2Config:{{"internalId":"987-en_US"}}}});
        j2w.search.options={{facets:["country","city"],showPicklistAllLocales:false}};
        $.ajaxSetup({{"X-CSRF-Token":"offline-token"}});</script>
        <span itemprop="title">Engineer</span><div itemprop="description">Native description</div>
        <meta itemprop="datePosted" content="2026-10-01">{addresses}</body></html>'''


def table(ids, total=None, offset=0, size=2, locale="cs_CZ"):
    total = len(ids) if total is None else total
    links = "".join(f'<tr><td><a class="jobTitle-link" href="/job/Prague-Engineer/{i}/">Engineer</a></td></tr>' for i in ids)
    # Native links omit locale. The adapter must rebuild requests with state.
    nxt = f'<a href="?startrow={size}">Next</a>' if total > size else ""
    return f'''<script>j2w.init({{"ssoCompanyId":"ACME"}});</script><table>{links}</table>
        <span class="paginationLabel">Results <b>{offset + 1} – {offset + len(ids)}</b> of <b>{total}</b></span>{nxt}'''


def tiles(ids, total=2, size=2, full=True):
    config = f'''<script>j2w.init({{"ssoCompanyId":"ACME"}});j2w.SearchResults.init({{
        apiEndpoint:"tile-search-results",searchQuery:"?q=&optionsFacetsDD_country=CZ",
        jobRecordsPerPage:parseInt("{size}"),jobRecordsFound:parseInt("{total}")}});</script>''' if full else ""
    return config + "".join(f'<article><a href="/job/Prague-Engineer/{i}/">Engineer</a></article>' for i in ids)


def config(locales=None):
    return configuration(ROOT + "/job/Prague-Engineer/123/", native(), "cs_CZ") if locales is None else RMKConfig(
        "ACME", ROOT + "/search/", "", "cs_CZ", locales, ["country", "city"], {HOST})


def row(job="fantastic", posting="123", slug="Prague-Engineer", host=HOST, brand=""):
    url = f"https://{host}/" + (brand + "/" if brand else "") + f"job/{slug}/{posting}/"
    return job, "Acme", url, url, url, None


class PlatformConfigTests(unittest.TestCase):
    def test_native_platform_classification(self):
        self.assertEqual(classify_platform(ROOT, native()), "rmk")
        self.assertEqual(classify_platform("https://jobs.sap.com/job/Old/123/", native()), "migrated_sap")
        self.assertEqual(classify_platform(ROOT, '<script>"jobs.smartrecruiters.com/tenant"</script>'), "migrated_sap")
        self.assertEqual(classify_platform(ROOT, '<script id="__NEXT_DATA__">"clientConfiguration-custom"</script>'), "dream_jobs")
        self.assertEqual(classify_platform(ROOT, "ordinary website"), "unknown")

    def test_url_parsing_retains_brand_and_native_id(self):
        p = parse_rmk_url(ROOT + "/Czech/job/Dobris-Title/123/?utm=test")
        self.assertEqual((p["host"], p["brand"], p["posting_id"]), (HOST, "Czech", "123"))
        for url in ("http://careers.example.com/job/x/123/", "https://user@careers.example.com/job/x/123/",
                    "https://127.0.0.1/job/x/123/", ROOT + "/job/title/provider-id/", ROOT + "/../job/x/123/"):
            self.assertIsNone(parse_rmk_url(url))

    def test_config_tenant_brand_locale_and_dynamic_fields(self):
        c = configuration(ROOT + "/Czech/job/x/123/", native(brand="Czech"))
        self.assertEqual(c.tenant, "ACME")
        self.assertEqual(c.brand, "Czech")
        self.assertEqual(c.locale, "cs_CZ")
        self.assertEqual(c.fields, ["country", "city"])
        self.assertNotIn("offline-token", repr(c))
        self.assertNotIn("offline-token", json.dumps(c.metadata()))

    def test_advertised_locales_are_discovered_and_explicit_scope_is_bounded(self):
        text = native() + '<a href="/?locale=en_US">English</a><a href="/?locale=cs_CZ">Czech</a>'
        self.assertEqual(configuration(ROOT, text).locales, ["cs_CZ", "en_US"])
        self.assertEqual(configuration(ROOT, text, "cs_CZ").locales, ["cs_CZ"])

    def test_alias_requires_explicit_public_form_and_same_tenant(self):
        alias = "search.example.com"
        text = native().replace('action="/search/"', f'action="https://{alias}/search/"')
        calls = []
        def handler(req):
            calls.append(req.url.host)
            return httpx.Response(200, text=text if req.url.host == HOST else native())
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            adapter = SuccessFactorsAdapter(client)
            _, c = adapter.load_config(RMKSite(HOST, "", ROOT + "/job/x/123/", "Acme"), "cs_CZ")
        self.assertEqual(c.hosts, {HOST, alias})
        self.assertEqual(calls, [HOST, alias])

    def test_alias_wrong_tenant_is_rejected(self):
        text = native().replace('action="/search/"', 'action="https://search.example.com/search/"')
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=text if req.url.host == HOST else native(tenant="OTHER")))) as client:
            with self.assertRaisesRegex(RMKError, "different native tenant"):
                SuccessFactorsAdapter(client).load_config(RMKSite(HOST, "", ROOT + "/job/x/123/", "Acme"))

    def test_search_form_omitted_from_detail_uses_normal_public_homepage(self):
        calls = []
        def handler(req):
            calls.append(req.url.path)
            text = native().replace('<form name="keywordsearch" method="get" action="/search/"></form>', "") if "/job/" in req.url.path else native()
            return httpx.Response(200, text=text)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            _, c = SuccessFactorsAdapter(client).load_config(RMKSite(HOST, "", ROOT + "/job/x/123/", "Acme"), "cs_CZ")
        self.assertEqual(c.search_url, ROOT + "/search/")
        self.assertEqual(calls, ["/job/x/123/", "/"])

    def test_unadvertised_redirect_host_and_access_challenge_are_rejected(self):
        for response in (httpx.Response(302, headers={"location": "https://other.example.com/"}),
                         httpx.Response(200, text="verify you are human")):
            with httpx.Client(transport=httpx.MockTransport(lambda req: response)) as client:
                with self.assertRaises(RMKError):
                    SuccessFactorsAdapter(client).load_config(RMKSite(HOST, "", ROOT, "Acme"))

    def test_brand_homepage_adds_locales_omitted_from_posting_layout(self):
        # Observed structure: branded posting uses a root search form and
        # German-only menu; its form-less brand homepage also advertises Czech.
        home = native(locale="de_DE").replace(
            '<form name="keywordsearch" method="get" action="/search/"></form>', "")
        home += (f'<a href="{ROOT}/Czech/?locale=cs_CZ">Czech</a>'
                 f'<a href="{ROOT}/Czech/?locale=en_US">English</a>'
                 '<a href="https://unverified.example.com/?locale=fr_FR">Other</a>'
                 '<a href="/?locale=invalid">Invalid</a>')
        calls = []
        def serve(req):
            calls.append(req.url.path)
            return httpx.Response(200, text=home if req.url.path == "/Czech/" else native(locale="de_DE"))
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            _, c = SuccessFactorsAdapter(client).load_config(
                RMKSite(HOST, "Czech", ROOT + "/Czech/job/x/123/", "Acme"))
        self.assertEqual(c.locales, ["cs_CZ", "de_DE", "en_US"])
        self.assertTrue(c.metadata()["locale_home_used"])
        self.assertEqual(calls, ["/Czech/job/x/123/", "/Czech/"])

    def test_explicit_locale_does_not_expand_from_brand_homepage(self):
        calls = []
        def serve(req):
            calls.append(req.url.path)
            return httpx.Response(200, text=native(locale="de_DE"))
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            _, c = SuccessFactorsAdapter(client).load_config(
                RMKSite(HOST, "Czech", ROOT + "/Czech/job/x/123/", "Acme"), "cs_CZ")
        self.assertEqual(c.locales, ["cs_CZ"])
        self.assertFalse(c.locale_home_used)
        self.assertEqual(len(calls), 1)

    def test_brand_homepage_must_confirm_same_native_tenant(self):
        def serve(req):
            return httpx.Response(200, text=native(tenant="OTHER" if req.url.path == "/Czech/" else "ACME"))
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            with self.assertRaisesRegex(RMKError, "confirm the native RMK tenant"):
                SuccessFactorsAdapter(client).load_config(
                    RMKSite(HOST, "Czech", ROOT + "/Czech/job/x/123/", "Acme"))

    def test_homepage_locale_union_discovers_native_jobs_in_both_languages(self):
        calls = []
        def serve(req):
            if req.url.path == "/Czech/":
                return httpx.Response(200, text=native(locale="de_DE") + '<a href="/?locale=cs_CZ">Czech</a>')
            if "/job/" in req.url.path:
                return httpx.Response(200, text=native(locale="de_DE"))
            if req.method == "POST":
                return httpx.Response(200, json={"facets": {"map": {"country": [{"name": "CZ", "count": 1}]}}})
            locale = req.url.params["locale"]
            calls.append(locale)
            return httpx.Response(200, text=table(["123" if locale == "de_DE" else "124"], locale=locale))
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            a = SuccessFactorsAdapter(client)
            _, c = a.load_config(RMKSite(HOST, "Czech", ROOT + "/Czech/job/x/123/", "Acme"))
            jobs, _ = a.discover_candidates(c)
        self.assertEqual({j["posting_id"] for j in jobs}, {"123", "124"})
        self.assertEqual(calls, ["cs_CZ", "de_DE"])

    def test_identity_uses_native_posting_and_preserves_tenant_case(self):
        self.assertEqual(source_job_id("ACME", "123"), "ACME:123")
        self.assertNotEqual(source_job_id("ACME", "123"), source_job_id("OTHER", "123"))
        for native_id in (None, 123, "987-en_US", "provider-id", ""):
            with self.assertRaises(ValueError):
                source_job_id("ACME", native_id)


class ListingFacetTests(unittest.TestCase):
    def test_table_page_size_and_real_links_only(self):
        p = parse_listing(table(["123", "124"], 5) + '<style>https://careers.example.com/job/test/999/</style>', ROOT, {HOST})
        self.assertEqual(p["handler"], "table")
        self.assertEqual(p["total"], 5)
        self.assertEqual(p["page_size"], 2)
        self.assertEqual([j["posting_id"] for j in p["jobs"]], ["123", "124"])

    def test_pagination_preserves_locale_filters_and_board_page_sizes(self):
        for size in (2, 3):
            calls = []
            def serve(req):
                calls.append(dict(req.url.params))
                offset = int(req.url.params["startrow"])
                ids = [str(123 + i) for i in range(offset, min(offset + size, size + 1))]
                return httpx.Response(200, text=table(ids, size + 1, offset, size))
            with httpx.Client(transport=httpx.MockTransport(serve)) as client:
                jobs = list(SuccessFactorsAdapter(client).traverse(config(), "cs_CZ", {"optionsFacetsDD_country": "CZ"}))
            self.assertEqual(len(jobs), size + 1)
            self.assertEqual([q["startrow"] for q in calls], ["0", str(size)])
            self.assertTrue(all(q["locale"] == "cs_CZ" and q["optionsFacetsDD_country"] == "CZ" for q in calls))

    def test_tile_handler_uses_advertised_endpoint_and_short_tail(self):
        calls = []
        def serve(req):
            calls.append((req.url.path, dict(req.url.params)))
            return httpx.Response(200, text=tiles(["123", "124"], 3) if req.url.path == "/search/" else tiles(["125"], full=False))
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            jobs = list(SuccessFactorsAdapter(client).traverse(config(), "cs_CZ", {}))
        self.assertEqual(len(jobs), 3)
        self.assertEqual(calls[1][0], "/tile-search-results/")
        self.assertEqual(calls[1][1]["locale"], "cs_CZ")
        self.assertEqual(calls[1][1]["startrow"], "2")

    def test_repeated_page_and_page_limit_never_silently_truncate(self):
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=table(["123", "124"], 5)))) as client:
            with self.assertRaisesRegex(RMKError, "repeated"):
                list(SuccessFactorsAdapter(client).traverse(config(), "cs_CZ", {}))
            with self.assertRaisesRegex(RMKError, "safety limit"):
                list(SuccessFactorsAdapter(client, max_pages=1).traverse(config(), "cs_CZ", {}))

    def test_mismatched_short_page_fails_instead_of_truncating(self):
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=tiles(["123"], 9, 2)))) as client:
            with self.assertRaisesRegex(RMKError, "contradicts"):
                list(SuccessFactorsAdapter(client).traverse(config(), "cs_CZ", {}))

    def test_paths_are_deduplicated_by_native_id(self):
        text = table(["123", "123"]) + '<a href="/job/Other-slug/123/">Renamed</a>'
        self.assertEqual(len(parse_listing(text, ROOT, {HOST})["jobs"]), 1)

    def test_fallback_prunes_only_explicit_complete_native_locations(self):
        text = '''<article itemscope itemtype="https://schema.org/JobPosting"><a href="/job/Berlin-Engineer/123/">Engineer</a>
            <div itemscope itemtype="https://schema.org/PostalAddress"><meta itemprop="addressCountry" content="DE">
            <span itemprop="streetAddress">Berlin</span></div></article>'''
        job = parse_listing(text, ROOT, {HOST})["jobs"][0]
        self.assertTrue(clearly_foreign_listing(job))
        job["locations_complete"] = False
        self.assertFalse(clearly_foreign_listing(job))
        job["locations_complete"] = True;job["locations"].append({"country": None, "text": "Multiple locations"})
        self.assertFalse(clearly_foreign_listing(job))

    def test_dynamic_country_city_and_geography_union(self):
        facets = {"Country": [{"name": "Cz", "translated": "Česká republika", "count": 2}],
                  "customLocation": [{"name": "Prague", "count": 1}, {"name": "Brno", "count": 1}],
                  "brand": [{"name": "Czech"}], "city": [{"name": "EMEA"}, {"name": "Remote"}]}
        queries = geography_queries(facets, list(facets))
        self.assertEqual({(q["field"], q["value"]) for q in queries}, {("Country", "Cz"), ("customLocation", "Prague"), ("customLocation", "Brno")})

    def test_facet_overlap_deduplicates_before_details(self):
        f = {"country": [{"name": "CZ", "count": 1}], "city": [{"name": "Praha", "count": 1}]}
        def serve(req):
            if req.method == "POST":
                self.assertEqual(json.loads(req.content)["facetquery"]["fields"], ["country", "city"])
                self.assertEqual(req.headers["X-CSRF-Token"], "offline-token")
                return httpx.Response(200, json={"facets": {"map": f}})
            return httpx.Response(200, text=table(["123"]))
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            adapter = SuccessFactorsAdapter(client)
            jobs, modes = adapter.discover_candidates(config())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(modes[0]["queries"]), 2)
        self.assertEqual(adapter.counters["listing_calls"], 2)

    def test_no_facet_falls_back_and_detail_still_rejects_foreign_job(self):
        def serve(req):
            return httpx.Response(200, json={"facets": {"map": {}}}) if req.method == "POST" else httpx.Response(200, text=table(["123"]))
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            adapter = SuccessFactorsAdapter(client)
            jobs, modes = adapter.discover_candidates(config())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(modes[0]["mode"], "fallback")
        self.assertIsNone(czech_evidence(parse_detail(ROOT + "/job/x/123/", native(countries=("DE",), locations=("Berlin",)), config())))

    def test_erste_like_positive_facet_empty_search_is_unresolved(self):
        def serve(req):
            return httpx.Response(200, json={"facets": {"map": {"country": [{"name": "CZ", "count": 187}]}}}) if req.method == "POST" else httpx.Response(200, text=table([], 0))
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            with self.assertRaisesRegex(RMKError, "facet advertises") as cm:
                SuccessFactorsAdapter(client).discover_candidates(config())
        self.assertEqual(cm.exception.kind, "unresolved")

    def test_http_and_malformed_facet_failures(self):
        for response in (httpx.Response(403), httpx.Response(200, json={}), httpx.Response(200, text="not json")):
            with httpx.Client(transport=httpx.MockTransport(lambda req: response)) as client:
                with self.assertRaises(RMKError):
                    SuccessFactorsAdapter(client).get_facets(config(), "cs_CZ")


class DetailAttachmentTests(unittest.TestCase):
    def test_native_identity_is_distinct_from_requisition(self):
        d = parse_detail(ROOT + "/job/x/123/", native(), config())
        self.assertEqual(d["posting_id"], "123")
        self.assertEqual(d["internal_requisition_locale_id"], "987-en_US")
        self.assertEqual(d["title"], "Engineer")
        self.assertEqual(d["description"], "Native description")
        self.assertNotIn("offline-token", d["native_html"])

    def test_multiple_native_addresses_preserved_and_czech_secondary_accepts(self):
        d = parse_detail(ROOT + "/job/x/123/", native(countries=("DE", "CZ"), locations=("Berlin", "Praha")), config())
        self.assertEqual(len(d["locations"]), 2)
        self.assertEqual(czech_evidence(d)["matches"][0]["location"]["country"], "CZ")

    def test_foreign_country_defeats_ambiguous_primary_city(self):
        d = parse_detail(ROOT + "/job/x/123/", native(countries=("US",), locations=("Prague",)), config())
        self.assertIsNone(czech_evidence(d))

    def test_czech_structured_address_without_country_and_css_excluded(self):
        d = parse_detail(ROOT + "/job/x/123/", native(countries=("",), locations=("Praha, Czech Republic",)), config())
        self.assertIsNotNone(czech_evidence(d))
        self.assertNotIn("junk", d["locations"][0]["text"])

    def test_generic_remote_and_malformed_uninformative_geo_rejected(self):
        for country, location in (("", "Remote"), ("", "Europe"), ("", "EMEA"), ("", "Worldwide"), ("Ce", "Unknown"), ("Ce", "Prague")):
            d = parse_detail(ROOT + "/job/x/123/", native(countries=(country,), locations=(location,)), config())
            self.assertIsNone(czech_evidence(d))

    def test_jsonld_native_multiple_locations_without_microdata(self):
        text = native().replace('<meta itemprop="addressCountry" content="CZ">', "").replace('itemtype="https://schema.org/PostalAddress"', "")
        data = {"@type": "JobPosting", "jobLocation": [{"@type": "Place", "address": {"addressCountry": "DE", "addressLocality": "Berlin"}},
                                                     {"@type": "Place", "address": {"addressCountry": "CZ", "addressLocality": "Praha"}}]}
        text += '<script type="application/ld+json">' + json.dumps(data) + '</script>'
        detail = parse_detail(ROOT + "/job/x/123/", text, config())
        self.assertTrue(any(v["country"] == "CZ" for v in detail["locations"]))
        self.assertIsNotNone(czech_evidence(detail))

    def test_native_country_cz_qualifies_remote_but_remote_alone_does_not(self):
        detail = parse_detail(ROOT + "/job/x/123/", native(countries=("CZ",), locations=("Remote",)), config())
        self.assertIsNotNone(czech_evidence(detail))

    def test_withdrawn_and_missing_identity_counters_are_distinct(self):
        # Both kinds are tested through the native parser rather than treating
        # an HTTP-200 empty shell as a live posting.
        for text, kind in ((native().replace("jobID:123,", ""), "missing_identity"),
                           ('<script>j2w.init({"ssoCompanyId":"ACME"});</script>', "withdrawn")):
            with self.assertRaises(RMKError) as cm:
                parse_detail(ROOT + "/job/x/123/", text, config())
            self.assertEqual(cm.exception.kind, kind)

    def test_missing_native_id_is_not_replaced_by_url_or_provider_or_reqid(self):
        text = native().replace("jobID:123,", "")
        with self.assertRaises(RMKError) as cm:
            parse_detail(ROOT + "/job/x/123/", text, config())
        self.assertEqual(cm.exception.kind, "missing_identity")

    def test_wrong_tenant_wrong_native_id_and_empty_shell(self):
        for text, kind in ((native(tenant="OTHER"), "missing_identity"), (native(posting="124"), "missing_identity"),
                           ('<script>j2w.init({"ssoCompanyId":"ACME"});</script>', "withdrawn")):
            with self.assertRaises(RMKError) as cm:
                parse_detail(ROOT + "/job/x/123/", text, config())
            self.assertEqual(cm.exception.kind, kind)

    def test_unique_zero_and_two_canonical_matches(self):
        for rows, expected in (([], None), ([row()], "fantastic"), ([row(), row()], "fantastic"),
                               ([row("one"), row("two", slug="Renamed")], None)):
            self.assertEqual(ingestion.find_existing_successfactors_job(rows, config(), "123"), expected)

    def test_exact_slug_does_not_override_native_ambiguity(self):
        rows = [row("exact"), row("other", slug="Other-slug", brand="Other-brand")]
        self.assertIsNone(ingestion.find_existing_successfactors_job(rows, config(), "123"))
        self.assertEqual(len(ingestion.attachment_candidates(rows, config(), "123")), 2)

    def test_verified_alias_participates_in_ambiguity_and_wrong_host_does_not(self):
        c = config()
        self.assertEqual(ingestion.find_existing_successfactors_job([row("a"), row("b", host="alias.example.com")], c, "123"), "a")
        c.hosts.add("alias.example.com")
        self.assertIsNone(ingestion.find_existing_successfactors_job([row("a"), row("b", host="alias.example.com")], c, "123"))

    def test_conflicting_fantastic_urls_are_not_identity_evidence(self):
        r = list(row());r[3] = row(posting="124")[3]
        self.assertIsNone(ingestion.find_existing_successfactors_job([r], config(), "123"))


class RMKCursor(baseline.FakeCursor):
    def __init__(self, rows=()):
        super().__init__(existing_source=False)
        self.fantastic_rows = list(rows)
        self.identities = {}

    def execute(self, query, params=()):
        normalized = " ".join(query.split()) if isinstance(query, str) else query.as_string()
        if normalized.startswith("select js.job_id, coalesce"):
            self.rows = self.fantastic_rows
            self.calls.append((normalized, params))
        elif normalized.startswith("select js.id, js.job_id"):
            self.rows = [self.identities[tuple(params)]] if tuple(params) in self.identities else []
            self.calls.append((normalized, params))
        else:
            super().execute(query, params)
            if normalized.startswith("insert into public.job_sources"):
                self.identities[tuple(params[1:3])] = ("source-id", params[0])


class IngestionTests(baseline.OfflineTest):
    def setUp(self):
        super().setUp()
        self.http = httpx.Client(transport=httpx.MockTransport(self.serve))
        self.addCleanup(self.http.close)
        self.factory = self.stack.enter_context(patch.object(ingestion, "SuccessFactorsAdapter", side_effect=lambda: SuccessFactorsAdapter(self.http)))
        self.api = TestClient(main.app)
        self.addCleanup(self.api.close)
        self.detail = native()
        self.facet_count = 1
        self.listing = table(["123"])

    def serve(self, req):
        if req.method == "POST":
            return httpx.Response(200, json={"facets": {"map": {"country": [{"name": "CZ", "count": self.facet_count}]}}})
        if "/search/" in req.url.path:
            return httpx.Response(200, text=self.listing)
        return httpx.Response(200, text=self.detail)

    def request(self, cur, **body):
        conn = self.fake_database(cur)
        response = self.api.post("/ingest/ats/successfactors", json={"host": HOST, "brand": "", "locale": "cs_CZ", **body})
        return response, conn

    def test_unique_attachment_raw_retention_and_canonical_helper(self):
        cur = RMKCursor([row()]);response, _ = self.request(cur)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["attached"], 1)
        raw = cur.source_writes[0][1][5].obj
        self.assertEqual(raw["detail"]["posting_id"], "123")
        self.assertIn("native_html", raw["detail"])
        self.assertEqual(cur.canonical_writes[0]["title"], "Engineer")
        self.assertEqual(cur.run_update["records_updated"], 1)

    def test_ambiguous_attachment_creates_and_counts_separate_job(self):
        cur = RMKCursor([row("a"), row("b", slug="Renamed")]);response, _ = self.request(cur)
        self.assertEqual(response.json()["created"], 1)
        self.assertEqual(response.json()["attached"], 0)
        self.assertEqual(response.json()["ambiguous_attachment"], 1)

    def test_zero_native_match_creates_new_posting_without_provider_identity(self):
        response, _ = self.request(RMKCursor([row(posting="999")]))
        self.assertEqual(response.json()["created"], 1)
        self.assertEqual(response.json()["attached"], 0)
        self.assertEqual(response.json()["ambiguous_attachment"], 0)

    def test_idempotent_source_identity_updates_without_second_insert(self):
        cur = RMKCursor([row()]);first, _ = self.request(cur);second, _ = self.request(cur)
        self.assertEqual(first.json()["attached"], 1)
        self.assertEqual(second.json()["updated"], 1)
        self.assertEqual(len(cur.identities), 1)
        self.assertIn((ingestion.SOURCE_NAME, "ACME:123"), cur.identities)

    def test_unresolved_site_is_visible_and_not_claimed_empty_success(self):
        self.facet_count = 187;self.listing = table([], 0)
        response, _ = self.request(RMKCursor([row()]))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["unresolved_sites"], 1)
        self.assertEqual(response.json()["failed_sites"], 0)
        self.assertFalse(response.json()["coverage_complete"])

    def test_all_sites_http_failed_returns_existing_502_semantics(self):
        self.factory.side_effect = lambda: SuccessFactorsAdapter(httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(503))))
        response, conn = self.request(RMKCursor([row()]))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["failed_sites"], 1)
        self.assertEqual(conn.commit.call_count, 2)

    def test_valid_empty_site_succeeds(self):
        self.facet_count = 0;self.listing = table([], 0)
        response, _ = self.request(RMKCursor([row()]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["czech_jobs"], 0)

    def test_validated_mode_selects_only_approved_live_discovery_scope(self):
        gate = synthetic_gate()
        self.stack.enter_context(patch.object(ingestion, "load_scope_gate", return_value=gate))
        cur = RMKCursor([row(), row("excluded", host="excluded.example.com"),
                         row("new", host="new.example.com")])
        self.fake_database(cur)
        response = self.api.post("/ingest/ats/successfactors", json={"validated_scopes": True})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["sites"], 1)
        self.assertEqual(result["successful_sites"], 1)
        self.assertEqual(result["unapproved_discovered_scopes"], [{"host": "new.example.com", "brand": ""}])
        self.assertEqual([s["host"] for s in result["site_results"]], [HOST])
        self.assertFalse(result["coverage_complete"])
        self.assertTrue(result["requested_scope_complete"])

    def test_explicit_reviewed_recovery_uses_bounded_locale_gate(self):
        self.stack.enter_context(patch.object(ingestion, "load_scope_gate", return_value=synthetic_gate()))
        self.detail += '<a href="/?locale=en_US">English</a>'
        cur = RMKCursor([row()]);self.fake_database(cur)
        response = self.api.post("/ingest/ats/successfactors", json={"host": HOST, "brand": ""})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["sites"], 1)
        self.assertEqual(result["site_results"][0]["config"]["locales"], ["cs_CZ", "en_US"])
        self.assertEqual(result["candidate_records"], 1)
        self.assertEqual(result["details_requested"], 1)
        self.assertEqual(result["attached"], 1)

    def test_validated_mode_rejects_request_overrides_before_database_access(self):
        for override in ({"host": HOST}, {"brand": ""}, {"locale": "cs_CZ"}, {"max_sites": 1}):
            with patch.object(ingestion.psycopg, "connect") as connect:
                response = self.api.post("/ingest/ats/successfactors", json={"validated_scopes": True, **override})
                self.assertEqual(response.status_code, 400)
                connect.assert_not_called()

    def test_validated_config_drift_does_not_fetch_listings_or_write_jobs(self):
        gate = synthetic_gate()
        gate.scopes[0].native_tenant = "CHANGED"
        self.stack.enter_context(patch.object(ingestion, "load_scope_gate", return_value=gate))
        cur = RMKCursor([row()]);self.fake_database(cur)
        response = self.api.post("/ingest/ats/successfactors", json={"validated_scopes": True})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["site_errors"][0]["kind"], "config_regression")
        self.assertEqual(response.json()["listing_calls"], 0)
        self.assertEqual(cur.source_writes, [])

    def test_partial_success_preserves_unresolved_diagnostics(self):
        original = self.serve
        def serve(req):
            if req.url.host == "unresolved.example.com":
                if req.method == "POST":
                    return httpx.Response(200, json={"facets": {"map": {"country": [{"name": "CZ", "count": 187}]}}})
                return httpx.Response(200, text=table([], 0) if "/search/" in req.url.path else native())
            return original(req)
        self.http.close();self.http = httpx.Client(transport=httpx.MockTransport(serve));self.addCleanup(self.http.close)
        cur = RMKCursor([row(), row("other", host="unresolved.example.com")]);self.fake_database(cur)
        response = self.api.post("/ingest/ats/successfactors", json={"locale": "cs_CZ"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["successful_sites"], 1)
        self.assertEqual(response.json()["unresolved_sites"], 1)
        self.assertFalse(response.json()["coverage_complete"])

    def test_migrated_and_custom_rows_are_not_rmk_sites(self):
        rows = [row(), ("sap", "SAP", "https://jobs.sap.com/job/Old/123/", None, None, None),
                ("custom", "Custom", "https://custom.example.com/jobs/detail/55", None, None, None)]
        sites, classes = ingestion.discover_sites(rows)
        self.assertEqual(len(sites), 1)
        self.assertEqual(classes, {"rmk_candidate": 1, "migrated_sap": 1, "dream_jobs": 1})

    def test_priority_protection_and_shared_merge_dedupe_registration(self):
        self.assertEqual(SOURCE_PRIORITY[ingestion.SOURCE_NAME], 300)
        self.assertIs(merge.SOURCE_PRIORITY, SOURCE_PRIORITY)
        self.assertIs(dedupe.SOURCE_PRIORITY, SOURCE_PRIORITY)
        self.assertEqual(canonical_field_updates({"title": "RMK"}, {"title": "Lower"}, "jooble_direct", [ingestion.SOURCE_NAME]), {})
        fixture = baseline.DedupeTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.job("a", [ingestion.SOURCE_NAME]);fixture.job("b", ["fantastic_jobs_apify"])
        self.assertEqual(fixture.pairs(), [("a", "b")])


def synthetic_gate():
    return ScopeGate.model_validate({"version": 1, "scopes": [{"host": HOST, "brand": "",
        "native_tenant": "ACME", "native_search_brand": "", "preferred_locale": "cs_CZ"}],
        "excluded_scopes": [{"host": "excluded.example.com", "brand": "", "reason": "Unreviewed frontend"}]})


class ValidatedScopeTests(unittest.TestCase):
    def test_repository_gate_matches_exact_approved_plan_shape(self):
        gate = load_scope_gate()
        self.assertEqual(len(gate.scopes), 62)
        self.assertEqual(len(gate.excluded_scopes), 7)
        self.assertEqual(sum(s.locale_union is not None for s in gate.scopes), 1)
        self.assertEqual(sum(len(s.locale_union or [s.preferred_locale]) for s in gate.scopes), 68)
        self.assertEqual(len({(s.native_tenant, s.native_search_brand) for s in gate.scopes}), 62)

    def test_missing_reviewed_scope_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "absent"):
            synthetic_gate().select([])

    def test_duplicates_excluded_overlap_and_extra_job_data_are_rejected(self):
        gate = synthetic_gate().model_dump()
        for change in ("duplicate", "native_duplicate", "excluded", "job_url"):
            data = json.loads(json.dumps(gate))
            if change == "duplicate":data["scopes"].append(dict(data["scopes"][0]))
            elif change == "native_duplicate":
                data["scopes"].append({**data["scopes"][0], "host": "other.example.com"})
            elif change == "excluded":data["excluded_scopes"][0]["host"] = HOST
            else:data["scopes"][0]["job_url"] = ROOT + "/job/x/123/"
            with self.assertRaises(ValueError):ScopeGate.model_validate(data)

    def test_native_scope_and_alias_drift_rejected(self):
        rule = synthetic_gate().scopes[0]
        for c in (replace(config(), tenant="OTHER"), replace(config(), brand="Other"),
                  replace(config(), hosts={HOST, "unreviewed.example.com"}),
                  replace(config(), search_url="https://unreviewed.example.com/search/")):
            with self.assertRaises(RMKError):apply_scope_gate(c, rule)

    def test_preferred_locale_does_not_expand_advertised_views(self):
        c = config(locales=["cs_CZ", "en_US", "de_DE"])
        apply_scope_gate(c, synthetic_gate().scopes[0])
        self.assertEqual(c.locales, ["cs_CZ"])

    def test_reviewed_homepage_union_does_not_admit_new_locale(self):
        data = synthetic_gate().model_dump()
        data["scopes"][0].update(brand="Czech", locale_union=["cs_CZ", "en_US"])
        rule = ScopeGate.model_validate(data).scopes[0]
        c = replace(config(locales=["cs_CZ", "en_US", "de_DE"]), locale_home_used=True)
        apply_scope_gate(c, rule)
        self.assertEqual(c.locales, ["cs_CZ", "en_US"])
        with self.assertRaises(RMKError):apply_scope_gate(replace(c, locale_home_used=False), rule)
        with self.assertRaises(RMKError):apply_scope_gate(replace(c, locales=["cs_CZ"]), rule)



class RecoveryTests(unittest.TestCase):
    def test_preferred_plus_one_advertised_english_locale(self):
        from app.adapters.successfactors import bounded_locale_views
        self.assertEqual(bounded_locale_views('cs_CZ', ['cs_CZ','en_GB','en_US','de_DE','fr_FR']), ['cs_CZ','en_US'])
        self.assertEqual(bounded_locale_views('de_DE', ['de_DE','en_US']), ['de_DE','en_US'])
        self.assertEqual(bounded_locale_views('cs_CZ', ['en_GB']), ['cs_CZ','en_GB'])
        self.assertEqual(bounded_locale_views('en_US', ['en_US','en_GB','de_DE']), ['en_US'])
        self.assertEqual(bounded_locale_views('cs_CZ', ['de_DE']), ['cs_CZ'])

    def test_homepage_locale_menu_preserves_same_native_scope(self):
        site=RMKSite(HOST,'',ROOT+'/job/x/123/','Acme')
        home=native()+'<a href="/?locale=en_US">English</a><a href="https://other.example.com/?locale=en_GB">Other</a>'
        with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,text=home if req.url.path=='/' else native()))) as client:
            _,c=SuccessFactorsAdapter(client).load_config(site,'cs_CZ',bounded_locale_recall=True)
        apply_scope_gate(c,synthetic_gate().scopes[0])
        self.assertEqual(c.locales,['cs_CZ','en_US'])
        self.assertTrue(c.bounded_recall)

    def test_homepage_other_tenant_or_brand_is_rejected(self):
        for home in (native(tenant='OTHER'),native(brand='Other')):
            with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,text=home if req.url.path=='/' else native()))) as client:
                with self.assertRaisesRegex(RMKError,'homepage'):
                    SuccessFactorsAdapter(client).load_config(RMKSite(HOST,'',ROOT+'/job/x/123/','Acme'),'cs_CZ',bounded_locale_recall=True)

    def test_english_locale_recovers_identity_and_deduplicates_before_details(self):
        def serve(req):
            locale=req.url.params.get('locale','cs_CZ')
            if req.method=='POST':return httpx.Response(200,json={'facets':{'map':{'country':[{'name':'CZ','count':2}]}}})
            return httpx.Response(200,text=table(['123','456'] if locale=='en_US' else ['123'],total=2 if locale=='en_US' else 1))
        c=replace(config(),locales=['cs_CZ','en_US'],bounded_recall=True)
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            jobs,modes=SuccessFactorsAdapter(client).discover_candidates(c)
        self.assertEqual([j['posting_id'] for j in jobs],['123','456'])
        self.assertEqual(len(modes),2)
        self.assertEqual(jobs[1]['discovery_locale'],'en_US')
        self.assertIsNone(czech_evidence(parse_detail(ROOT+'/job/x/456/',native('456',countries=('DE',),locations=('Berlin',)),config())))

    def test_native_czech_city_union_foreign_and_remote_excluded(self):
        f={'city':[{'name':v,'count':1} for v in ('Pardubice','Kutná Hora','KUTNA HORA','Berlin','Kutná Hora, Germany','EMEA','Remote')]}
        qs=geography_queries(f,['city'])
        self.assertEqual({q['value'] for q in qs},{'Pardubice','Kutná Hora','KUTNA HORA'})
        self.assertIsNone(czech_evidence({'locations':[{'text':'Kutná Hora','country':None}]}))

    def test_city_and_locale_overlap_do_not_repeat_candidate_identity(self):
        def serve(req):
            if req.method=='POST':return httpx.Response(200,json={'facets':{'map':{'city':[{'name':'Pardubice','count':1},{'name':'Kutná Hora','count':1}]}}})
            return httpx.Response(200,text=table(['123']))
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            a=SuccessFactorsAdapter(client);jobs,modes=a.discover_candidates(replace(config(),locales=['cs_CZ','en_US']))
        self.assertEqual(len(jobs),1);self.assertEqual(a.counters['listing_calls'],4)

    def test_alternate_no_facet_large_catalog_requires_review(self):
        def serve(req):
            if req.method=='POST':return httpx.Response(200,json={'facets':{'map':{}}})
            return httpx.Response(200,text=table(['123'],total=1) if req.url.params['locale']=='cs_CZ' else table(['456','789'],total=5000))
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            a=SuccessFactorsAdapter(client)
            with self.assertRaisesRegex(RMKError,'limited to 100'):
                a.discover_candidates(replace(config(),locales=['cs_CZ','en_US'],bounded_recall=True))
        self.assertEqual(a.counters['listing_calls'],2)

    def test_small_alternate_no_facet_catalog_is_traversed(self):
        def serve(req):
            if req.method=='POST':return httpx.Response(200,json={'facets':{'map':{}}})
            return httpx.Response(200,text=table([]) if req.url.params['locale']=='cs_CZ' else table(['456']))
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            jobs,_=SuccessFactorsAdapter(client).discover_candidates(replace(config(),locales=['cs_CZ','en_US'],bounded_recall=True))
        self.assertEqual([j['posting_id'] for j in jobs],['456'])

    def test_foundever_declining_count_requires_verified_empty_boundary(self):
        calls=[]
        def serve(req):
            offset=int(req.url.params.get('startrow','0'));calls.append(offset)
            return httpx.Response(200,text=table(['123','456'],total=4,size=2) if offset==0 else table(['789'] if offset==2 else [],total=3,offset=offset,size=2))
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            a=SuccessFactorsAdapter(client);jobs=list(a.traverse(config(),'cs_CZ',{'optionsFacetsDD_country':'CZ'}))
        self.assertEqual([j['posting_id'] for j in jobs],['123','456','789'])
        self.assertEqual(calls,[0,2,4]);self.assertEqual(a.counters['pagination_boundary_checks'],1)

    def test_changed_count_without_empty_boundary_is_not_complete(self):
        def serve(req):
            offset=int(req.url.params.get('startrow','0'))
            return httpx.Response(200,text=table(['123','456'],total=4,size=2) if offset==0 else table(['789'],total=3,offset=offset,size=2))
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            with self.assertRaisesRegex(RMKError,'boundary confirmation'):
                list(SuccessFactorsAdapter(client).traverse(config(),'cs_CZ',{}))

    def test_true_stall_still_fails(self):
        with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,text=table(['123','456'],total=4)))) as client:
            with self.assertRaisesRegex(RMKError,'repeated'):
                list(SuccessFactorsAdapter(client).traverse(config(),'cs_CZ',{}))

if __name__ == "__main__":
    unittest.main()
