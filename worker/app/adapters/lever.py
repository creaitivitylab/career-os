from app.operation_budget import check_deadline
import re
from typing import Any, Literal, NamedTuple
from urllib.parse import unquote, urlsplit

import httpx


LeverInstance = Literal["global", "eu"]
JOB_HOSTS = {"jobs.lever.co": "global", "jobs.eu.lever.co": "eu"}
API_HOSTS = {"global": "https://api.lever.co", "eu": "https://api.eu.lever.co"}
SITE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,199}")
POSTING_ID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


class LeverIdentity(NamedTuple):
    instance: str
    site: str
    posting_id: str | None


class LeverHTTPError(RuntimeError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Lever postings error {status_code}")


class LeverPaginationError(RuntimeError):
    def __init__(self, reason: str, skip: int, rows: int, overlap: int = 0):
        self.diagnostics = {'reason': reason, 'skip': skip, 'returned_rows': rows,
                            'overlap_count': overlap}
        super().__init__(f"Lever pagination did not advance safely: {reason} at skip={skip}")


def normalize_site(site: str) -> str:
    if not isinstance(site, str) or not SITE_RE.fullmatch(site.strip()):
        raise ValueError("Invalid Lever site")
    return site.strip().lower()


def native_site_slug(url: Any) -> str | None:
    """Retain advertised API path case; the existing identity key stays lowercase."""
    if parse_lever_url(url) is None:
        return None
    return unquote(urlsplit(url.strip()).path).strip('/').split('/')[0]


def normalize_posting_id(value: Any) -> str | None:
    if isinstance(value, str) and POSTING_ID_RE.fullmatch(value.strip()):
        return value.strip().lower()
    return None


def parse_lever_url(value: Any) -> LeverIdentity | None:
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value.strip())
        if (url.scheme != "https" or url.hostname not in JOB_HOSTS
                or url.username is not None or url.password is not None
                or url.port not in (None, 443)):
            return None
        parts = unquote(url.path).strip("/").split("/")
        if len(parts) not in (1, 2, 3) or (len(parts) == 3 and parts[2] != "apply"):
            return None
        site = normalize_site(parts[0])
        posting_id = normalize_posting_id(parts[1]) if len(parts) >= 2 else None
        if len(parts) >= 2 and posting_id is None:
            return None
        return LeverIdentity(JOB_HOSTS[url.hostname], site, posting_id)
    except ValueError:
        return None


def source_job_id(instance: str, site: str, posting_id: str) -> str:
    native_id = normalize_posting_id(posting_id)
    if instance not in API_HOSTS or native_id is None:
        raise ValueError("Invalid Lever instance or posting ID")
    return f"{instance}:{normalize_site(site)}:{native_id}"


class LeverAdapter:
    PAGE_SIZE = 100

    def probe(self, site: str, instance: LeverInstance = "global") -> int:
        """Availability only: one tiny GET, never an inventory or verification."""
        if instance not in API_HOSTS:
            raise ValueError("Invalid Lever instance")
        normalize_site(site)
        check_deadline()
        with httpx.Client(timeout=20.0, headers={'Accept': 'application/json',
                                               'User-Agent': 'CareerOS/0.1'}) as client:
            response = client.get(f'{API_HOSTS[instance]}/v0/postings/{site.strip()}',
                                  params={'mode': 'json', 'limit': 1, 'skip': 0})
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise RuntimeError('Invalid Lever probe JSON') from exc
                if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                    raise RuntimeError('Invalid Lever probe response')
            return response.status_code

    def list_postings(self, site: str, instance: LeverInstance = "global") -> list[dict[str, Any]]:
        if instance not in API_HOSTS:
            raise ValueError("Invalid Lever instance")
        normalize_site(site)  # Validate the path without altering its native case.
        url = f"{API_HOSTS[instance]}/v0/postings/{site.strip()}"
        jobs = []
        seen_pages = set()
        seen_ids = set()
        offset = 0
        with httpx.Client(timeout=60.0, headers={
            "Accept": "application/json", "User-Agent": "CareerOS/0.1",
        }) as client:
            while True:
                if len(seen_pages) >= 1000:
                    raise RuntimeError("Lever pagination safety limit reached")
                check_deadline()
                response = client.get(url, params={
                    "mode": "json", "limit": self.PAGE_SIZE, "skip": offset,
                })
                if response.status_code >= 400:
                    raise LeverHTTPError(response.status_code)
                try:
                    data = response.json()
                except ValueError as exc:
                    raise RuntimeError("Invalid Lever JSON response") from exc
                if not isinstance(data, list) or any(not isinstance(job, dict) for job in data):
                    raise RuntimeError("Unexpected Lever postings response")
                if any(not isinstance(job.get('id'), str) or not job['id'].strip() for job in data):
                    raise LeverPaginationError('missing_identity', offset, len(data))
                fingerprint = tuple(job['id'].lower() for job in data)
                page_ids = set(fingerprint)
                if data and fingerprint in seen_pages:
                    raise LeverPaginationError('repeated_page', offset, len(data), len(page_ids))
                if seen_ids & page_ids:
                    # Even one overlap can indicate offset drift and skipped jobs.
                    raise LeverPaginationError('cross_page_overlap', offset, len(data), len(seen_ids & page_ids))
                unique = {}
                for key, job in zip(fingerprint, data):
                    if key in unique and unique[key] != job:
                        raise LeverPaginationError('conflicting_duplicate', offset, len(data))
                    unique[key] = job
                seen_ids.update(page_ids)
                seen_pages.add(fingerprint)
                jobs.extend(unique.values())
                # Native offsets count raw slots, including identical duplicates.
                offset += len(data)
                if len(data) < self.PAGE_SIZE:
                    return jobs
