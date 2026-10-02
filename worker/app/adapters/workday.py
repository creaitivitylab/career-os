"""Public Workday candidate-experience API; no authenticated Workday access."""
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx


COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")
JOBS_HOST = re.compile(r"^([a-z0-9][a-z0-9-]*)\.(wd\d+)\.myworkdayjobs\.com$")
SITE_HOST = re.compile(r"^(wd\d+)\.myworkdaysite\.com$")
NATIVE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
LOCALE = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$")
PAGE_SIZE = 20
MAX_PAGES = 1000
CZECH_NAMES = {"cz", "cze", "czechia", "czech republic", "cesko", "ceska republika"}
CZECH_CITIES = (
    "prague", "praha", "brno", "ostrava", "plzen", "pilsen", "olomouc",
    "liberec", "pardubice", "hradec kralove", "ceske budejovice",
    "usti nad labem", "zlin", "jihlava", "karlovy vary",
)
# Only explicit country evidence defeats an otherwise ambiguous city name.
FOREIGN_COUNTRIES = (
    "united states", "usa", "us", "canada", "germany", "deutschland",
    "poland", "polska", "slovakia", "slovensko", "austria", "hungary",
    "united kingdom", "uk", "gb", "france", "spain", "romania", "india",
    "china", "japan", "australia", "netherlands", "switzerland", "italy",
    "serbia", "brazil", "mexico", "ireland", "portugal", "sweden", "finland",
)


def fold_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def contains_phrase(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "


def czech_location(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = fold_text(value)
    if any(contains_phrase(text, name) for name in CZECH_NAMES):
        return True
    if any(contains_phrase(text, name) for name in FOREIGN_COUNTRIES):
        return False
    return any(contains_phrase(text, city) for city in CZECH_CITIES)


def _segments(path: str) -> list[str]:
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if any(part in (".", "..") or any(ch in part for ch in "/\\")
           or any(ord(ch) < 32 for ch in part) for part in parts):
        raise ValueError("Unsafe Workday path")
    return parts


def normalize_external_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("/job/"):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            return None
        parts = _segments(parsed.path)
        if parts[-1:] == ["apply"]:
            parts.pop()
        # Workday also publishes /job/posting without a location segment.
        if len(parts) < 2 or parts[0] != "job":
            return None
        return "/" + "/".join(quote(part, safe="-._~") for part in parts)
    except ValueError:
        return None


@dataclass(frozen=True)
class WorkdaySite:
    host: str
    tenant: str
    site: str
    company: str = ""
    locale: str = "en-US"

    def __post_init__(self):
        host = self.host.lower()
        tenant = self.tenant.lower()
        match = JOBS_HOST.fullmatch(host)
        if not (match or SITE_HOST.fullmatch(host)):
            raise ValueError("Workday host must be a public myworkdayjobs/myworkdaysite hostname")
        if not COMPONENT.fullmatch(tenant) or not COMPONENT.fullmatch(self.site):
            raise ValueError("Invalid Workday tenant or career site")
        if match and match[1] != tenant:
            raise ValueError("Workday tenant does not match hostname")
        if not LOCALE.fullmatch(self.locale):
            raise ValueError("Invalid Workday locale")
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "tenant", tenant)

    @property
    def scope(self):
        return self.host, self.tenant, self.site

    @property
    def cluster(self):
        match = JOBS_HOST.fullmatch(self.host)
        return match[2] if match else SITE_HOST.fullmatch(self.host)[1]

    @property
    def family(self):
        return "myworkdayjobs" if JOBS_HOST.fullmatch(self.host) else "myworkdaysite"

    @property
    def api_base(self):
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"

    @property
    def public_base(self):
        prefix = self.site if self.family == "myworkdayjobs" else f"recruiting/{self.tenant}/{self.site}"
        return f"https://{self.host}/{prefix}"


@dataclass(frozen=True)
class WorkdayURL:
    board: WorkdaySite
    external_path: str | None

    @property
    def identity(self):
        return (*self.board.scope, self.external_path)


def parse_workday_url(value: Any) -> WorkdayURL | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
        host = (parsed.hostname or "").lower()
        jobs_host = JOBS_HOST.fullmatch(host)
        site_host = SITE_HOST.fullmatch(host)
        if not (jobs_host or site_host):
            return None
        parts = _segments(parsed.path)
        locale = "en-US"
        if len(parts) >= 2 and LOCALE.fullmatch(parts[0]) and (parts[1] == "recruiting" or parts[1] != "job"):
            locale = parts.pop(0)
        if jobs_host:
            tenant, site = jobs_host[1], parts.pop(0)
        else:
            if parts.pop(0) != "recruiting":
                return None
            tenant, site = parts.pop(0), parts.pop(0)
        board = WorkdaySite(host, tenant, site, locale=locale)
        path = normalize_external_path("/" + "/".join(quote(part, safe="-._~") for part in parts)) if parts else None
        if parts and path is None:
            return None
        return WorkdayURL(board, path)
    except (ValueError, IndexError):
        return None


def source_job_id(board: WorkdaySite, native_posting_id: Any) -> str:
    if not isinstance(native_posting_id, str) or not NATIVE_ID.fullmatch(native_posting_id):
        raise ValueError("Missing or invalid native Workday posting ID")
    # WID is native posting identity. Requisition IDs and URL tokens are not WIDs.
    return f"{board.tenant}:{board.site}:{native_posting_id.lower()}"


def czech_evidence(detail: dict) -> dict | None:
    info = detail.get("jobPostingInfo") or {}
    if not isinstance(info, dict):
        return None
    location = info.get("jobRequisitionLocation") or {}
    country = (location.get("country") or {}) if isinstance(location, dict) else {}
    primary = info.get("country") or {}
    code = country.get("alpha2Code") if isinstance(country, dict) else None
    if code is None and isinstance(primary, dict):
        code = primary.get("alpha2Code")
    if isinstance(code, str) and code.upper() == "CZ":
        return {"kind": "primary_country", "country_code": "CZ"}
    additional = info.get("additionalLocations")
    matches = [value for value in additional if czech_location(value)] if isinstance(additional, list) else []
    return {"kind": "additional_location", "locations": matches} if matches else None


def _facet_nodes(facets):
    for node in facets if isinstance(facets, list) else []:
        if isinstance(node, dict):
            yield node
            yield from _facet_nodes(node.get("values"))


def discover_czech_facets(facets: Any) -> list[dict]:
    """OR Czech values within a dimension; query dimensions separately."""
    choices = {}
    for facet in _facet_nodes(facets):
        parameter = facet.get("facetParameter")
        if not isinstance(parameter, str) or not parameter:
            continue
        label = fold_text(str(facet.get("descriptor") or ""))
        semantic = fold_text(parameter)
        if not any(word in f"{label} {semantic}" for word in ("country", "location", "geograph", "hierarchy")):
            continue
        raw_values = facet.get("values")
        values = [value for value in raw_values if isinstance(value, dict)
                  and isinstance(value.get("id"), str) and value["id"]
                  and isinstance(value.get("descriptor"), str)] if isinstance(raw_values, list) else []
        countries = [value for value in values if fold_text(value["descriptor"]) in CZECH_NAMES]
        # A country-named location bucket need not include the city's buckets.
        selected = [value for value in values if czech_location(value["descriptor"])]
        if selected:
            score = (bool(countries), "country" in f"{label} {semantic}",
                     sum(v.get("count", 0) for v in selected if isinstance(v.get("count"), int)), parameter)
            choice = choices.setdefault(parameter, {"values": {}, "score": score})
            choice["score"] = max(choice["score"], score)
            choice["values"].update({value["id"]: value for value in selected})
    return [{"parameter": parameter, "ids": list(choice["values"]),
             "descriptors": [value["descriptor"] for value in choice["values"].values()]}
            for parameter, choice in sorted(choices.items(), key=lambda item: item[1]["score"], reverse=True)]


def discover_czech_facet(facets: Any) -> dict | None:
    """Preferred dimension for existing callers; discovery unions all dimensions."""
    choices = discover_czech_facets(facets)
    return choices[0] if choices else None


def clearly_foreign_listing(record: dict) -> bool:
    """Fallback prunes only explicit foreign single locations, not unknowns."""
    value = record.get("locationsText")
    if not isinstance(value, str):
        return False
    text = fold_text(value)
    if czech_location(value) or re.search(r"\b\d+ locations\b", text) or any(
            contains_phrase(text, word) for word in ("remote", "multiple", "europe", "emea", "worldwide")):
        return False
    return any(contains_phrase(text, name) for name in FOREIGN_COUNTRIES)


class WorkdayAPIError(RuntimeError):
    def __init__(self, kind: str, message: str, http_status: int | None = None, error_code: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.error_code = error_code


@dataclass
class WorkdayDiscovery:
    jobs: list[dict]
    counters: dict
    geography: dict | None


class WorkdayAdapter:
    def __init__(self, client=None, max_pages: int = MAX_PAGES):
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.owns_client = client is None
        self.max_pages = max_pages
        self.counters = {"listing_calls": 0, "listing_records": 0, "candidate_records": 0,
                         "facet_candidates": 0, "fallback_skipped_foreign": 0,
                         "pagination_boundary_confirmed": 0, "listing_missing_path": 0,
                         "facet_queries": 0, "facet_listing_records": 0}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.owns_client:
            self.client.close()

    def _request(self, method: str, board: WorkdaySite, path: str, body=None):
        try:
            response = self.client.request(method, board.api_base + path, json=body,
                headers={"Accept": "application/json", "Accept-Language": board.locale,
                         "User-Agent": "CareerOS/1.0 (public careers API)"})
        except httpx.HTTPError as exc:
            raise WorkdayAPIError("transient", "Workday request failed or timed out") from exc
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code != 200:
            code = data.get("errorCode") if isinstance(data, dict) else None
            # Do not expose errorCaseId, session data, or response body in errors.
            kind = {403: "forbidden", 404: "unavailable"}.get(response.status_code, "transient")
            raise WorkdayAPIError(kind, f"Workday HTTP {response.status_code}" + (" / S22" if code == "S22" else ""),
                                  response.status_code, "S22" if code == "S22" else None)
        if not isinstance(data, dict):
            raise WorkdayAPIError("malformed", "Workday response is not a JSON object")
        return data

    def list_page(self, board: WorkdaySite, offset: int, applied_facets: dict):
        self.counters["listing_calls"] += 1
        data = self._request("POST", board, "/jobs", {
            "appliedFacets": applied_facets, "limit": PAGE_SIZE, "offset": offset, "searchText": ""})
        jobs = data.get("jobPostings")
        if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
            raise WorkdayAPIError("malformed", "Workday listing has invalid jobPostings")
        self.counters["listing_records"] += len(jobs)
        if applied_facets:
            self.counters["facet_listing_records"] += len(jobs)
        return data

    def _listing_jobs(self, board: WorkdaySite, applied: dict, first=None):
        # Each dimension has its own pagination state. Overlap between distinct
        # dimensions is legitimate and must not trigger stalled-page detection.
        seen, offset = set(), 0
        initial_total, last_paths, last_offset = None, [], 0
        pathless_pages = set()
        for page_number in range(self.max_pages):
            page = first if first is not None and page_number == 0 else self.list_page(board, offset, applied)
            records = page["jobPostings"]
            if not records:
                break
            # CXS can return a stub containing only bulletFields. It has no
            # fetchable posting identity; report it without inventing a URL.
            valid_records = [record for record in records if record.get("externalPath") is not None]
            self.counters["listing_missing_path"] += len(records) - len(valid_records)
            paths = [normalize_external_path(record["externalPath"]) for record in valid_records]
            if any(path is None for path in paths):
                raise WorkdayAPIError("malformed", "Workday listing has an invalid external job path")
            if page_number == 0:
                value = page.get("total")
                initial_total = value if type(value) is int and value >= len(records) else None
            if not paths:
                signature = json.dumps(records, sort_keys=True)
                if signature in pathless_pages:
                    raise WorkdayAPIError("stalled", "Workday pagination stalled: repeated pathless page")
                pathless_pages.add(signature)
            elif not any(path not in seen for path in paths):
                # Some CXS sites reset out-of-range offsets to page zero. Never
                # accept a repeat alone or trust a subsequent page's total.
                # Confirm the advertised boundary with an overlapping, short
                # tail page; an API ignoring offsets still fails this check.
                if initial_total == len(seen) == offset and len(last_paths) == PAGE_SIZE:
                    tail = self.list_page(board, last_offset + PAGE_SIZE // 2, applied)["jobPostings"]
                    tail_paths = [normalize_external_path(record.get("externalPath")) for record in tail]
                    if tail_paths == last_paths[PAGE_SIZE // 2:]:
                        self.counters["pagination_boundary_confirmed"] += 1
                        break
                raise WorkdayAPIError("stalled", "Workday pagination stalled: no new job paths")
            for record, path in zip(valid_records, paths):
                if path in seen:
                    continue
                seen.add(path)
                yield record, path
            last_paths, last_offset = paths, offset
            offset += len(records)
            # 'total' can be zero on nonempty subsequent pages. Ignore it.
            if len(records) < PAGE_SIZE:
                break
        else:
            raise WorkdayAPIError("page_limit", "Workday pagination exceeded its safety limit; site not truncated silently")

    def discover_candidates(self, board: WorkdaySite) -> WorkdayDiscovery:
        first = self.list_page(board, 0, {})
        queries = discover_czech_facets(first.get("facets"))
        geography = {**queries[0], "queries": queries} if queries else None
        jobs, seen = [], set()
        for query in queries or [None]:
            applied = {query["parameter"]: query["ids"]} if query else {}
            if query:
                self.counters["facet_queries"] += 1
            for record, path in self._listing_jobs(board, applied, first if query is None else None):
                if path in seen:
                    continue
                seen.add(path)
                if query is None and clearly_foreign_listing(record):
                    self.counters["fallback_skipped_foreign"] += 1
                    continue
                jobs.append(record)
        self.counters["candidate_records"] = len(jobs)
        self.counters["facet_candidates"] = len(jobs) if geography else 0
        return WorkdayDiscovery(jobs, dict(self.counters), geography)

    def get_detail(self, board: WorkdaySite, external_path: str):
        path = normalize_external_path(external_path)
        if path is None:
            raise WorkdayAPIError("malformed", "Invalid Workday detail path")
        data = self._request("GET", board, path)
        info = data.get("jobPostingInfo")
        if not isinstance(info, dict):
            raise WorkdayAPIError("malformed", "Workday detail lacks jobPostingInfo")
        try:
            source_job_id(board, info.get("id"))
        except ValueError as exc:
            raise WorkdayAPIError("missing_identity", str(exc)) from exc
        if not isinstance(info.get("title"), str) or not info["title"].strip():
            raise WorkdayAPIError("malformed", "Workday detail lacks a title")
        url = info.get("externalUrl")
        if url:
            parsed = parse_workday_url(url)
            if parsed is None or parsed.identity != (*board.scope, path):
                raise WorkdayAPIError("malformed", "Workday detail URL does not match the requested posting")
        if info.get("jobPostingSiteId") and info["jobPostingSiteId"] != board.site:
            raise WorkdayAPIError("malformed", "Workday detail career site does not match")
        return data
