"""Anonymous SAP Recruiting Marketing frontends, not authenticated RCM/OData."""
import html
import ipaddress
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from app.adapters.workday import CZECH_NAMES, czech_location, fold_text


LOCALE = re.compile(r"^[a-z]{2}_[A-Z]{2}$")
COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
RMK_PATH = re.compile(r"^(?P<brand>(?:/[^/]+)*)/job/[^/]+/(?P<id>\d{1,20})/?$")
MAX_PAGES = 1000
COUNTERS = ("configs_loaded", "facet_calls", "listing_calls", "listing_records",
            "candidate_records", "facet_candidates", "fallback_scopes", "fallback_skipped_foreign",
            "http_requests", "get_requests", "post_requests", "alias_resolutions", "config_fallbacks",
            "locale_home_loads", "pagination_boundary_checks")


class RMKError(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def public_url(value: Any, base: str | None = None) -> str:
    if not isinstance(value, str):
        raise ValueError("Missing public RMK URL")
    url = urljoin(base, html.unescape(value)) if base else value.strip()
    p = urlsplit(url)
    host = p.hostname or ""
    if (p.scheme != "https" or p.username or p.password or p.port not in (None, 443)
            or "." not in host or host.endswith((".local", ".internal", ".localhost"))
            or not re.fullmatch(r"[a-zA-Z0-9.-]+", host)):
        raise ValueError("RMK requires a public HTTPS career URL")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP-literal career hosts are not supported")
    if "\\" in p.path or re.search(r"(?:^|/)(?:\.|\.\.)(?:/|$)|%2f|%5c|%2e", p.path, re.I):
        raise ValueError("Unsafe RMK URL path")
    return urlunsplit(("https", host.lower(), p.path or "/", p.query, ""))


def parse_rmk_url(value: Any) -> dict | None:
    try:
        url = public_url(value)
    except (ValueError, TypeError):
        return None
    p = urlsplit(url)
    m = RMK_PATH.fullmatch(p.path)
    if not m:
        return None
    return {"host": p.hostname, "brand": m["brand"].strip("/"),
            "posting_id": m["id"], "url": url, "path": p.path}


def classify_platform(url: str, native_html: str = "") -> str:
    p = urlsplit(url)
    if p.hostname == "jobs.sap.com" or "jobs.smartrecruiters.com/" in native_html:
        return "migrated_sap"
    if ("api.dream.jobs" in native_html or
            ("__NEXT_DATA__" in native_html and "clientConfiguration-" in native_html)):
        return "dream_jobs"
    if re.search(r'["\']ssoCompanyId["\']\s*:', native_html) and "j2w.init" in native_html:
        return "rmk"
    return "unknown"


def source_job_id(tenant: Any, posting_id: Any) -> str:
    if not isinstance(tenant, str) or not COMPONENT.fullmatch(tenant):
        raise ValueError("Missing native RMK tenant")
    if not isinstance(posting_id, str) or not re.fullmatch(r"\d{1,20}", posting_id):
        raise ValueError("Missing native RMK posting ID")
    return f"{tenant}:{posting_id}"


class Node:
    def __init__(self, tag="document", attrs=None):
        self.tag, self.attrs, self.children = tag, attrs or {}, []
        self.parent = None

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        if self.tag in ("style", "script"):
            return ""
        return " ".join(c.text() if isinstance(c, Node) else c for c in self.children).strip()


class NativeHTML(HTMLParser):
    """Small native-data tree; script/style text never becomes job geography."""
    VOID = {"meta", "input", "img", "link", "br", "hr", "source", "wbr", "area", "base", "embed", "param"}

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, text):
        self.stack[-1].children.append(text)

    def nodes(self):
        return self.root.walk()

    def properties(self, node=None, attribute="itemprop"):
        values = {}
        for n in (node or self.root).walk():
            key = n.attrs.get(attribute)
            value = n.attrs.get("content") or " ".join(n.text().split())
            if key and value:
                values.setdefault(key, []).append(value)
        return values


def js_string(text, key):
    m = re.search(r"(?:[\"']" + re.escape(key) + r"[\"']|\b" + re.escape(key)
                  + r"\b)\s*:\s*([\"'])(.*?)\1", text, re.S)
    return html.unescape(m[2]) if m else None


def normalize_locale(value):
    value = value.replace("-", "_").strip() if isinstance(value, str) else ""
    return value if LOCALE.fullmatch(value) else None


@dataclass(frozen=True)
class RMKSite:
    host: str
    brand: str
    seed_url: str
    company: str

    @property
    def scope(self):
        return self.host, self.brand


@dataclass
class RMKConfig:
    tenant: str
    search_url: str
    brand: str
    locale: str
    locales: list[str]
    fields: list[str]
    hosts: set[str]
    csrf: str | None = field(default=None, repr=False)
    show_all_locales: bool = False
    locale_home_used: bool = False
    advertised_locales: list[str] = field(default_factory=list)
    bounded_recall: bool = False

    def metadata(self):
        return {"tenant": self.tenant, "search_url": self.search_url, "brand": self.brand,
                "locale": self.locale, "locales": self.locales, "fields": self.fields,
                "hosts": sorted(self.hosts), "csrf_available": bool(self.csrf),
                "locale_home_used": self.locale_home_used,
                "advertised_locales": self.advertised_locales,
                "bounded_recall": self.bounded_recall}


def configuration(url, text, requested_locale=None):
    if classify_platform(url, text) != "rmk":
        raise RMKError("unsupported", "Public career page is not RMK")
    tenant = js_string(text, "ssoCompanyId")
    if not tenant or not COMPONENT.fullmatch(tenant):
        raise RMKError("missing_identity", "RMK configuration lacks a native tenant")
    doc = NativeHTML(text)
    forms = [n for n in doc.nodes() if n.tag == "form" and n.attrs.get("name") == "keywordsearch"]
    if not forms or forms[0].attrs.get("method", "get").lower() != "get":
        raise RMKError("unresolved", "RMK has no advertised public GET search form")
    try:
        search = public_url(forms[0].attrs.get("action"), url)
    except ValueError as exc:
        raise RMKError("unresolved", "RMK search form has an unsafe target") from exc
    if not urlsplit(search).path.rstrip("/").endswith("/search"):
        raise RMKError("unresolved", "Unsupported public search form path")
    m = re.search(r"facets\s*:\s*(\[[^\]]*\])", text)
    try:
        fields = json.loads(m[1]) if m else []
    except ValueError:
        fields = []
    fields = [v for v in fields if isinstance(v, str) and COMPONENT.fullmatch(v)]
    if not fields:
        fields = list(dict.fromkeys(n.attrs["name"][len("optionsFacetsDD_"):]
                      for n in doc.nodes() if n.attrs.get("name", "").startswith("optionsFacetsDD_")))
    apply = re.search(r"j2w\.Apply\.init\s*\(\s*\{(.*?)\}\s*\)", text, re.S)
    locale = normalize_locale(js_string(apply[1], "locale")) if apply else None
    if not locale:
        m = re.search(r"/strings_([a-z]{2}_[A-Z]{2})\.js", text)
        locale = m[1] if m else next((normalize_locale(n.attrs.get("lang")) for n in doc.nodes()
                                    if n.tag == "html"), None)
    if not locale:
        raise RMKError("unresolved", "RMK public page exposes no usable locale")
    locales = {locale}
    for n in doc.nodes():
        if n.tag == "a" and n.attrs.get("href"):
            try:
                link = public_url(n.attrs["href"], url)
            except ValueError:
                continue
            if urlsplit(link).hostname != urlsplit(url).hostname:
                continue
            value = dict(parse_qsl(urlsplit(link).query)).get("locale")
            if (value := normalize_locale(value)):
                locales.add(value)
    if requested_locale and not normalize_locale(requested_locale):
        raise ValueError("Invalid RMK locale")
    return RMKConfig(tenant, search, urlsplit(search).path.rsplit("/search", 1)[0].strip("/"),
                     requested_locale or locale, [requested_locale] if requested_locale else sorted(locales),
                     fields, {urlsplit(url).hostname, urlsplit(search).hostname},
                     js_string(text, "X-CSRF-Token"), bool(re.search(r"showPicklistAllLocales\s*:\s*true", text)),
                     advertised_locales=sorted(locales))


def bounded_locale_views(preferred, advertised):
    """One primary view plus one public English view; never a language crawl."""
    if preferred.startswith("en_"):
        return [preferred]
    english = sorted({v for v in advertised if normalize_locale(v) and v.startswith("en_")},
                     key=lambda v: (v != "en_US", v != "en_GB", v))
    return [preferred] + english[:1]


def geography_queries(facets, fields):
    queries = []
    for key, values in facets.items():
        if key not in fields or not re.search(r"country|city|location|geograph|region|state", key, re.I):
            continue
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get("name"), str):
                continue
            # Bare city facets lack country suffixes. Extend the shared Czech
            # normalization for this native geography value only; final detail
            # eligibility is deliberately unchanged.
            if any(czech_location(v) or (isinstance(v, str) and fold_text(v) == "kutna hora")
                   for v in (value["name"], value.get("translated"))):
                queries.append({"field": key, "value": value["name"],
                                "label": value.get("translated") or value["name"],
                                "count": value.get("count") if type(value.get("count")) is int else None})
    # RMK's form selects a single value per field. Union separate requests,
    # never AND unrelated geography dimensions or invent multi-select syntax.
    return list({(q["field"], q["value"]): q for q in queries}.values())


def country_kind(value):
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    text = fold_text(value)
    if text in CZECH_NAMES:
        return "czech"
    # Ce/Czec and arbitrary two-letter strings are not reliable country codes.
    foreign_codes = {"us", "gb", "de", "pl", "sk", "at", "hu", "fr", "es", "ro", "in", "cn",
                     "jp", "au", "nl", "ch", "it", "rs", "br", "mx", "ie", "pt", "se", "fi", "ca"}
    foreign_names = {"germany", "poland", "slovakia", "austria", "hungary", "france", "spain", "romania",
                     "united states", "united kingdom", "sweden", "canada", "india", "china"}
    return "foreign" if text in foreign_codes | foreign_names else "unknown"


def czech_evidence(detail):
    matches = []
    for location in detail.get("locations", []):
        country = country_kind(location.get("country"))
        if country == "czech":
            matches.append({"kind": "native_country", "location": location})
        elif not location.get("country") and czech_location(location.get("text")):
            matches.append({"kind": "native_location", "location": location})
    return {"country_code": "CZ", "matches": matches} if matches else None


def ineligible_reason(detail):
    locations = detail.get("locations") or []
    if any(v.get("country") and country_kind(v["country"]) == "unknown" for v in locations):
        return "ambiguous_native_country"
    if locations and all(country_kind(v.get("country")) == "foreign" for v in locations):
        return "foreign_only"
    return "no_reliable_czech_native_location"


def clearly_foreign_listing(record):
    # A primary country does not exclude unreported secondary locations.
    # Only complete explicit address evidence can safely prune a fallback row.
    locations = record.get("locations") or []
    return bool(locations) and record.get("locations_complete", False) and all(
        country_kind(v.get("country")) == "foreign" for v in locations)


def parse_listing(text, base, hosts):
    doc = NativeHTML(text)
    jobs = {}
    for n in doc.nodes():
        if n.tag != "a" or not n.attrs.get("href"):
            continue
        try:
            url = public_url(n.attrs["href"], base)
        except ValueError:
            continue
        parsed = parse_rmk_url(url)
        if parsed and parsed["host"] in hosts:
            record = {"posting_id": parsed["posting_id"], "url": url, "title": " ".join(n.text().split()) or None}
            container = n.parent
            while container and container.tag not in ("tr", "article") and "JobPosting" not in container.attrs.get("itemtype", ""):
                container = container.parent
            if container:
                values = doc.properties(container)
                record["native_listing_properties"] = values
                locations = []
                for address in container.walk():
                    if "PostalAddress" in address.attrs.get("itemtype", ""):
                        geo = doc.properties(address)
                        locations.append({"country": next(iter(geo.get("addressCountry", [])), None),
                                          "text": " | ".join(geo.get("streetAddress") or geo.get("addressLocality") or [])})
                record["locations"] = locations
                # Plain row location labels may omit secondary locations.
                # Prune only an explicitly structured native JobPosting.
                record["locations_complete"] = bool(locations) and "JobPosting" in container.attrs.get("itemtype", "")
            jobs.setdefault(parsed["posting_id"], record)
    # Use visible native metadata, never counts inside unrelated scripts/CSS.
    total = size = range_start = range_end = None
    label = next((n for n in doc.nodes() if "paginationLabel" in n.attrs.get("class", "").split()), None)
    if label:
        bold = ["".join(re.findall(r"\d", n.text())) for n in label.walk() if n.tag == "b"]
        if len(bold) >= 2 and bold[-1]:
            total = int(bold[-1])
        ranges = re.findall(r"\d+", label.text())
        if len(ranges) >= 2:
            range_start, range_end = int(ranges[0]), int(ranges[1])
            size = range_end - range_start + 1
    def number(key):
        m = re.search(key + r"\s*:\s*(?:parseInt\(\s*)?[\"']?(\d+)", text)
        return int(m[1]) if m else None
    if "j2w.SearchResults.init" in text:
        style = "tile"
        size, total = number("jobRecordsPerPage"), number("jobRecordsFound")
        endpoint = js_string(text, "apiEndpoint")
        query = js_string(text, "searchQuery") or ""
    else:
        style, endpoint, query = "table", None, ""
    # Advertised offsets reveal the full page size even on a short tail.
    offsets = []
    for n in doc.nodes():
        if n.tag == "a" and n.attrs.get("href"):
            value = dict(parse_qsl(urlsplit(html.unescape(n.attrs["href"])).query)).get("startrow")
            if value and value.isdecimal() and int(value) > 0:
                offsets.append(int(value))
    if style == "table" and offsets:
        size = min(offsets)
    return {"jobs": list(jobs.values()), "total": total, "page_size": size,
            "handler": style, "endpoint": endpoint, "query": query,
            "range_start": range_start, "range_end": range_end}


def redact_session_data(text):
    text = re.sub(r"((?:X-CSRF-Token|ajaxSecKey)[\"']?\s*[:=]\s*[\"'])[^\"']+", r"\1[redacted]", text)
    text = re.sub(r"((?:_s\.crb|jsessionid)=)[^\s\"'&<>;]+", r"\1[redacted]", text, flags=re.I)
    return text


def parse_detail(url, text, config):
    doc = NativeHTML(text)
    props = doc.properties()
    fields = doc.properties(attribute="data-careersite-propertyid")
    schema = []
    for n in doc.nodes():
        if n.tag == "script" and n.attrs.get("type") == "application/ld+json":
            try:
                schema.append(json.loads("".join(c for c in n.children if isinstance(c, str))))
            except ValueError:
                pass
    def schema_nodes(value):
        if isinstance(value, list):
            for item in value:
                yield from schema_nodes(item)
        elif isinstance(value, dict):
            if value.get("@type") == "JobPosting":
                yield value
            yield from schema_nodes(value.get("@graph"))
    postings = list(schema_nodes(schema))
    m = re.search(r"\bjobID\s*:\s*(\d+)\b", text)
    title = next(iter(props.get("title") or fields.get("title") or []), None)
    description = next(iter(props.get("description") or fields.get("description") or []), None)
    if postings:
        title = title or postings[0].get("title")
        if not description and isinstance(postings[0].get("description"), str):
            description = " ".join(NativeHTML(postings[0]["description"]).root.text().split())
    if not m:
        kind = "withdrawn" if not title and not description else "missing_identity"
        raise RMKError(kind, "RMK detail lacks native posting identity" if kind != "withdrawn" else "RMK job is an empty/withdrawn shell")
    posting = m[1]
    parsed = parse_rmk_url(url)
    tenant = js_string(text, "ssoCompanyId")
    if not parsed or parsed["posting_id"] != posting or tenant != config.tenant:
        raise RMKError("missing_identity", "Native RMK detail identity differs from requested tenant/posting")
    if not isinstance(title, str) or not title.strip() or not isinstance(description, str) or not description.strip():
        raise RMKError("malformed", "RMK detail lacks native title/description")
    locations = []
    for n in doc.nodes():
        if "PostalAddress" in n.attrs.get("itemtype", ""):
            values = doc.properties(n)
            country = next(iter(values.get("addressCountry", [])), None)
            text_value = " | ".join(dict.fromkeys(v for k in ("streetAddress", "addressLocality", "addressRegion") for v in values.get(k, [])))
            locations.append({"country": country, "text": text_value, "native": values})
    if not locations:
        # Each native street/location node is a separate location, not one
        # concatenated string that could hide a foreign primary location.
        country_values = props.get("addressCountry") or fields.get("country") or []
        texts = list(dict.fromkeys(props.get("streetAddress") or fields.get("location") or []))
        for value in texts or ([""] if country_values else []):
            locations.append({"country": country_values[0] if len(country_values) == 1 and not locations else None, "text": value})
    for posting_data in postings:
        places = posting_data.get("jobLocation") or []
        for place in places if isinstance(places, list) else [places]:
            address = place.get("address") if isinstance(place, dict) else None
            if not isinstance(address, dict):
                continue
            country = address.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            value = " | ".join(str(address[k]) for k in ("streetAddress", "addressLocality", "addressRegion") if address.get(k))
            locations.append({"country": country, "text": value, "native": address})
    canonical = next((n.attrs.get("href") for n in doc.nodes() if n.tag == "link" and n.attrs.get("rel") == "canonical"), url)
    try:
        canonical = public_url(canonical, url)
    except ValueError as exc:
        raise RMKError("missing_identity", "Unsafe native RMK canonical URL") from exc
    identity = parse_rmk_url(canonical)
    if not identity or identity["host"] not in config.hosts or identity["posting_id"] != posting:
        raise RMKError("missing_identity", "RMK canonical URL differs from posting identity")
    req = js_string(text, "internalId")
    return {"posting_id": posting, "tenant": tenant, "internal_requisition_locale_id": req,
            "title": title, "description": description, "url": canonical, "locations": locations,
            "locale": js_string(text, "locale") or config.locale,
            "posted_date": next(iter(props.get("datePosted") or fields.get("datePosted") or []), None) or (postings[0].get("datePosted") if postings else None),
            "employment_type": next(iter(props.get("employmentType") or fields.get("employmentType") or []), None) or (postings[0].get("employmentType") if postings else None),
            "workplace": next(iter(fields.get("remote") or fields.get("workplace") or []), None),
            "native_properties": props, "native_fields": fields, "schema": schema,
            "native_html": redact_session_data(text)}


class SuccessFactorsAdapter:
    def __init__(self, client=None, max_pages=MAX_PAGES):
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.owns_client = client is None
        self.max_pages = max_pages
        self.counters = dict.fromkeys(COUNTERS, 0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.owns_client:
            self.client.close()

    def _request(self, method, url, hosts, **kwargs):
        url = public_url(url)
        for _ in range(4):
            if urlsplit(url).hostname not in hosts:
                raise RMKError("unsafe_url", "RMK request left its advertised public hosts")
            try:
                self.counters["http_requests"] += 1
                self.counters["get_requests" if method == "GET" else "post_requests"] += 1
                response = self.client.request(method, url, headers={"User-Agent": "CareerOS/1.0 (public RMK careers)",
                    **kwargs.pop("headers", {})}, **kwargs)
            except httpx.HTTPError as exc:
                raise RMKError("http", "Public RMK request failed or timed out") from exc
            if response.status_code in (301, 302, 303, 307, 308):
                if method != "GET":
                    raise RMKError("http", "Public RMK facet request redirected")
                try:
                    url = public_url(response.headers.get("location"), url)
                except ValueError as exc:
                    raise RMKError("unsafe_url", "Unsafe RMK redirect") from exc
                continue
            if response.status_code != 200:
                raise RMKError("withdrawn" if response.status_code == 404 else "http", f"Public RMK HTTP {response.status_code}")
            if re.search(r"g-recaptcha|hcaptcha|cf-chl-|verify you are human|not authorized to access", response.text, re.I):
                raise RMKError("access", "Public RMK response requires access/challenge; no bypass attempted")
            return str(response.url), response.text
        raise RMKError("http", "RMK public redirect limit exceeded")

    def load_config(self, site, locale=None, bounded_locale_recall=False):
        url, text = self._request("GET", site.seed_url, {site.host})
        platform = classify_platform(url, text)
        if platform != "rmk":
            return platform, None
        try:
            config = configuration(url, text, locale)
        except RMKError as exc:
            if exc.kind != "unresolved" or "search form" not in str(exc):
                raise
            # A normal public homepage can expose a search form omitted from
            # the detail layout. Same known host/brand only; never guess a
            # search endpoint or an alternative hostname.
            home = f"https://{site.host}/" + (site.brand + "/" if site.brand else "")
            if locale:
                home += "?" + urlencode({"locale": locale})
            home_url, home_text = self._request("GET", home, {site.host})
            config = configuration(home_url, home_text, locale)
            self.counters["config_fallbacks"] += 1
        if urlsplit(config.search_url).hostname != site.host:
            # The alternative host must be explicitly exposed by the public
            # form, and prove the same native tenant before receiving queries.
            _, alias_text = self._request("GET", config.search_url, config.hosts)
            alias = configuration(config.search_url, alias_text, locale)
            if alias.tenant != config.tenant:
                raise RMKError("unresolved", "Advertised alias has a different native tenant")
            alias.hosts.update(config.hosts)
            alias.locales = sorted(set(alias.locales + config.locales)) if not locale else [locale]
            config = alias
            self.counters["alias_resolutions"] += 1
        if locale is None and site.brand != config.brand:
            # A posting layout can expose a root search form and a narrower
            # language menu than its public brand homepage. Discover current
            # locale routes there; historical posting URLs are not listings.
            home = f"https://{site.host}/" + (site.brand + "/" if site.brand else "")
            home_url, home_text = self._request("GET", home, config.hosts)
            if classify_platform(home_url, home_text) != "rmk" or js_string(home_text, "ssoCompanyId") != config.tenant:
                raise RMKError("unresolved", "Public brand homepage cannot confirm the native RMK tenant")
            locales = set(config.locales)
            for node in NativeHTML(home_text).nodes():
                if node.tag != "a" or not node.attrs.get("href"):
                    continue
                try:
                    link = public_url(node.attrs["href"], home_url)
                except ValueError:
                    continue
                if urlsplit(link).hostname not in config.hosts:
                    continue
                value = normalize_locale(dict(parse_qsl(urlsplit(link).query)).get("locale"))
                if value:
                    locales.add(value)
            config.locales = sorted(locales)
            config.advertised_locales = sorted(locales)
            config.locale_home_used = True
            self.counters["locale_home_loads"] += 1
        if bounded_locale_recall and locale is not None and not locale.startswith("en_"):
            # Public posting layouts may omit language-menu entries. Read the
            # same brand homepage, confirm its native tenant and search scope,
            # and only use locale links actually exposed there.
            home = f"https://{site.host}/" + (site.brand + "/" if site.brand else "")
            home_url, home_text = self._request("GET", home, config.hosts)
            if classify_platform(home_url, home_text) != "rmk" or js_string(home_text, "ssoCompanyId") != config.tenant:
                raise RMKError("config_regression", "Public locale homepage cannot confirm the native RMK tenant")
            forms = [n for n in NativeHTML(home_text).nodes()
                     if n.tag == "form" and n.attrs.get("name") == "keywordsearch"]
            if forms and public_url(forms[0].attrs.get("action"), home_url).split("?", 1)[0].rstrip("/") != config.search_url.split("?", 1)[0].rstrip("/"):
                raise RMKError("config_regression", "Public locale homepage advertises a different search scope")
            advertised = set(config.advertised_locales)
            for node in NativeHTML(home_text).nodes():
                if node.tag != "a" or not node.attrs.get("href"):
                    continue
                try:
                    link = public_url(node.attrs["href"], home_url)
                except ValueError:
                    continue
                p = urlsplit(link)
                prefix = "/" + site.brand + "/" if site.brand else "/"
                if p.hostname not in config.hosts or not p.path.startswith(prefix):
                    continue
                value = normalize_locale(dict(parse_qsl(p.query)).get("locale"))
                if value:
                    advertised.add(value)
            config.advertised_locales = sorted(advertised)
            config.locale_home_used = True
            self.counters["locale_home_loads"] += 1
        self.counters["configs_loaded"] += 1
        return "rmk", config

    def get_facets(self, config, locale):
        if not config.fields:
            return {}
        self.counters["facet_calls"] += 1
        p = urlsplit(config.search_url)
        url = urlunsplit((p.scheme, p.netloc, "/services/jobs/options/facetValues/", urlencode({"locale": locale}), ""))
        body = {"page": 0, "keywords": "", "locationsearch": "", "sortby": "referencedate", "sortdir": "desc",
                "sortfield": "title", "recordsperpage": 25, "startrow": 0,
                "facetquery": {"facet": True, "mincount": 1, "limit": 5000, "fields": config.fields,
                               "sort": "index", "showPicklistAllLocales": config.show_all_locales}, "filterquery": {}}
        _, text = self._request("POST", url, config.hosts, json=body,
                                headers={"X-CSRF-Token": config.csrf} if config.csrf else {})
        try:
            data = json.loads(text)["facets"]["map"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RMKError("malformed", "RMK facet response is malformed") from exc
        if not isinstance(data, dict):
            raise RMKError("malformed", "RMK facet map is malformed")
        return data

    def list_page(self, config, locale, filters, offset=0, template=None):
        p = urlsplit(config.search_url)
        params = dict(parse_qsl(p.query))
        path = p.path
        if template and template["handler"] == "tile":
            endpoint = template["endpoint"]
            if not isinstance(endpoint, str) or endpoint != "tile-search-results":
                raise RMKError("unresolved", "Unsupported advertised RMK tile endpoint")
            path = p.path.rsplit("/search", 1)[0] + "/" + endpoint + "/"
            params.update(dict(parse_qsl(template["query"].lstrip("?"))))
        params.update({"q": "", "locationsearch": "", "sortColumn": "referencedate", "sortDirection": "desc",
                       **filters, "locale": locale, "startrow": offset})
        url = urlunsplit((p.scheme, p.netloc, path, urlencode(params), ""))
        self.counters["listing_calls"] += 1
        final, text = self._request("GET", url, config.hosts)
        if template is None or template["handler"] != "tile":
            if classify_platform(final, text) != "rmk" or js_string(text, "ssoCompanyId") != config.tenant:
                raise RMKError("unresolved", "Public search no longer exposes the expected RMK tenant")
        page = parse_listing(text, final, config.hosts)
        if template and template["handler"] == "tile":
            page.update({k: template[k] for k in ("handler", "page_size", "endpoint", "query")})
        self.counters["listing_records"] += len(page["jobs"])
        return page

    def traverse(self, config, locale, filters, first=None):
        seen, signatures, offset = set(), set(), 0
        template = None
        for number in range(self.max_pages):
            page = first if number == 0 and first is not None else self.list_page(config, locale, filters, offset, template)
            if number == 0:
                template = page
            jobs = page["jobs"]
            if not jobs:
                if page.get("total", 0):
                    raise RMKError("unresolved", "RMK advertised jobs but returned no public result paths")
                break
            signature = tuple(sorted(job["posting_id"] for job in jobs))
            if signature in signatures or not any(job["posting_id"] not in seen for job in jobs):
                raise RMKError("stalled", "RMK pagination repeated without new native identities")
            signatures.add(signature)
            for job in jobs:
                if job["posting_id"] not in seen:
                    seen.add(job["posting_id"])
                    yield job
            # Counted native catalogs can end on a full page. Require the
            # accumulated unique identities to agree with the initial count;
            # never use a subsequent page's count as an earlier boundary.
            if template.get("total") == len(seen):
                break
            size = template.get("page_size")
            if not size:
                # Absence of pagination metadata is safe only for a complete,
                # native-counted catalog. Otherwise do not silently truncate.
                if template.get("total") == len(seen):
                    break
                raise RMKError("unresolved", "RMK page lacks defensible pagination metadata")
            if len(jobs) < size:
                if template.get("total") is not None and len(seen) < template["total"]:
                    # A live catalog may shrink during traversal. Require a
                    # counted native terminal range agreeing with every unique
                    # identity, then independently confirm the empty next page.
                    # Never excuse duplicates, missing ranges or a real stall.
                    if not (page.get("total") == len(seen)
                            and page.get("range_start") == offset + 1
                            and page.get("range_end") == len(seen)):
                        raise RMKError("unresolved", "RMK short page contradicts advertised result count")
                    self.counters["pagination_boundary_checks"] += 1
                    probe = self.list_page(config, locale, filters, offset + size, template)
                    if (probe["jobs"] or probe.get("total") != len(seen)
                            or probe.get("range_start") != offset + size + 1):
                        raise RMKError("unresolved", "RMK changed terminal count lacks an empty boundary confirmation")
                break
            offset += size
        else:
            raise RMKError("page_limit", "RMK pagination exceeded its safety limit; catalog not truncated")

    def discover_candidates(self, config):
        candidates, modes = {}, []
        for locale in config.locales:
            facets = self.get_facets(config, locale)
            queries = geography_queries(facets, config.fields)
            count_before = len(candidates)
            handlers = set()
            if not queries:
                self.counters["fallback_scopes"] += 1
            for query in queries or [None]:
                filters = {"optionsFacetsDD_" + query["field"]: query["value"]} if query else {}
                first = self.list_page(config, locale, filters)
                if (config.bounded_recall and locale != config.locale and query is None
                        and (type(first.get("total")) is not int or first["total"] > 100)):
                    raise RMKError("unresolved", "Alternate locale needs reviewed full-catalog discovery; automatic fallback is limited to 100 postings")
                handlers.add(first["handler"])
                found = 0
                for job in self.traverse(config, locale, filters, first):
                    found += 1
                    if query is None and clearly_foreign_listing(job):
                        self.counters["fallback_skipped_foreign"] += 1
                        continue
                    candidates.setdefault(job["posting_id"], {**job, "discovery_locale": locale})
                if query and query.get("count", 0) and not found:
                    # Generic detection of the Erste-like contradiction. Do
                    # not fall back to re-fetching historical Fantastic URLs.
                    raise RMKError("unresolved", "Czech facet advertises postings but public search returned none")
            if queries:
                self.counters["facet_candidates"] += len(candidates) - count_before
            modes.append({"locale": locale, "mode": "facet" if queries else "fallback",
                          "queries": queries, "handlers": sorted(handlers)})
        self.counters["candidate_records"] = len(candidates)
        return list(candidates.values()), modes

    def get_detail(self, config, job):
        url, text = self._request("GET", job["url"], config.hosts)
        return parse_detail(url, text, config)
