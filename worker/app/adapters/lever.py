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


def normalize_site(site: str) -> str:
    if not isinstance(site, str) or not SITE_RE.fullmatch(site.strip()):
        raise ValueError("Invalid Lever site")
    return site.strip().lower()


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

    def list_postings(self, site: str, instance: LeverInstance = "global") -> list[dict[str, Any]]:
        if instance not in API_HOSTS:
            raise ValueError("Invalid Lever instance")
        url = f"{API_HOSTS[instance]}/v0/postings/{normalize_site(site)}"
        jobs = []
        seen_pages = set()
        with httpx.Client(timeout=60.0, headers={
            "Accept": "application/json", "User-Agent": "CareerOS/0.1",
        }) as client:
            while True:
                response = client.get(url, params={
                    "mode": "json", "limit": self.PAGE_SIZE, "skip": len(jobs),
                })
                if response.status_code >= 400:
                    raise RuntimeError(f"Lever postings error {response.status_code}: {response.text[:500]}")
                try:
                    data = response.json()
                except ValueError as exc:
                    raise RuntimeError("Invalid Lever JSON response") from exc
                if not isinstance(data, list) or any(not isinstance(job, dict) for job in data):
                    raise RuntimeError("Unexpected Lever postings response")
                fingerprint = tuple(str(job.get("id")) for job in data)
                if data and fingerprint in seen_pages:
                    raise RuntimeError("Lever pagination did not advance")
                seen_pages.add(fingerprint)
                jobs.extend(data)
                if len(data) < self.PAGE_SIZE:
                    return jobs
