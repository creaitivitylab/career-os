import html
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.adapters.lever import (
    API_HOSTS, LeverAdapter, LeverIdentity, normalize_posting_id,
    normalize_site, parse_lever_url, source_job_id,
)
from app.canonical import update_canonical_job
from app.ingestion import get_or_create_company
from app.ingestion_status import ALL_TENANTS_FAILED, direct_run_status


SOURCE_NAME = "lever_direct"
CZ_MARKERS = (
    "czech", "czechia", "cesko", "ceska republika", "prague", "praha", "brno",
    "ostrava", "plzen", "pilsen", "olomouc", "liberec", "pardubice",
    "hradec kralove", "ceske budejovice", "usti nad labem", "zlin", "jihlava",
    "karlovy vary",
)


@dataclass(frozen=True)
class LeverSite:
    instance: str
    site: str
    company: str


# Match the observed upstream marker ('lever.co') as well as native URL hosts.
# URL parsing below is authoritative; custom URLs/slugs never imply an instance.
FANTASTIC_LEVER_ROWS_SQL = """
    select js.job_id, coalesce(nullif(js.raw_payload ->> 'organization', ''), c.name),
           js.source_url, js.apply_url,
           js.raw_payload ->> 'url', js.raw_payload ->> 'job_url'
    from public.job_sources js
    join public.jobs j on j.id = js.job_id
    left join public.companies c on c.id = j.company_id
    where js.source_name = 'fantastic_jobs_apify'
      and (lower(js.raw_payload ->> 'source') in ('lever', 'lever.co')
           or js.source_url ilike '%%lever.co/%%'
           or js.apply_url ilike '%%lever.co/%%'
           or js.raw_payload ->> 'url' ilike '%%lever.co/%%'
           or js.raw_payload ->> 'job_url' ilike '%%lever.co/%%')
"""


def fantastic_lever_rows(cur, posting_id: str | None = None):
    query = FANTASTIC_LEVER_ROWS_SQL
    params = ()
    if posting_id is not None:
        query += """ and (js.source_url ilike %s or js.apply_url ilike %s
                         or js.raw_payload ->> 'url' ilike %s
                         or js.raw_payload ->> 'job_url' ilike %s)"""
        params = (f"%{posting_id}%",) * 4
    cur.execute(query, params)
    return cur.fetchall()


def discover_sites(cur) -> list[LeverSite]:
    companies = {}
    for _, company, *urls in fantastic_lever_rows(cur):
        scopes = {(identity.instance, identity.site) for value in urls
                  if (identity := parse_lever_url(value)) is not None}
        if len(scopes) == 1:
            scope = next(iter(scopes))
            companies.setdefault(scope, set())
            if company:
                companies[scope].add(company)
    return [LeverSite(instance, site, next(iter(names)) if len(names) == 1 else site)
            for (instance, site), names in sorted(companies.items())]


def select_sites(discovered: list[LeverSite], site: str | None,
                 instance: str | None, max_sites: int | None) -> list[LeverSite]:
    if instance is not None and instance not in API_HOSTS:
        raise ValueError("Invalid Lever instance")
    if site is not None:
        site = normalize_site(site)
        selected = [item for item in discovered if item.site == site
                    and (instance is None or item.instance == instance)]
        if len(selected) > 1:
            raise ValueError("Lever site exists in multiple instances; specify global or eu")
        if not selected:
            if instance is None:
                raise ValueError("Specify global or eu for an undiscovered Lever site")
            selected = [LeverSite(instance, site, site)]
    else:
        selected = [item for item in discovered if instance is None or item.instance == instance]
    if max_sites is not None:
        if max_sites < 1:
            raise ValueError("max_sites must be positive")
        selected = selected[:max_sites]
    return selected


def find_existing_lever_job(cur, instance: str, site: str, posting_id: str) -> Any | None:
    wanted_id = normalize_posting_id(posting_id)
    if wanted_id is None or instance not in API_HOSTS:
        return None
    wanted = LeverIdentity(instance, normalize_site(site), wanted_id)
    matches = set()
    for job_id, _, *urls in fantastic_lever_rows(cur, wanted_id):
        identities = {identity for value in urls
                      if (identity := parse_lever_url(value)) is not None
                      and identity.posting_id is not None}
        # Conflicting native identities on a source row are unsafe evidence.
        if identities == {wanted}:
            matches.add(job_id)
    return next(iter(matches)) if len(matches) == 1 else None


def fold_text(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()


def has_czech_location(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = fold_text(value)
    return any(re.search(r"(?<![a-z])" + re.escape(marker) + r"(?![a-z])", text)
               for marker in CZ_MARKERS) or bool(re.search(r"\bcz\b", text))


def posting_locations(job: dict[str, Any]) -> list[str]:
    categories = job.get("categories")
    if not isinstance(categories, dict):
        return []
    values = [categories.get("location")]
    if isinstance(categories.get("allLocations"), list):
        values.extend(categories["allLocations"])
    return list(dict.fromkeys(value.strip() for value in values
                              if isinstance(value, str) and value.strip()))


def is_czech_job(job: dict[str, Any]) -> bool:
    country = job.get("country")
    if isinstance(country, str) and country.strip().upper() == "CZ":
        return True
    # Remote, Europe, EMEA, or worldwide alone are not Czech eligibility.
    if isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country.strip()):
        # country describes the primary location. An explicit foreign country
        # defeats an ambiguous primary city (e.g. Prague in the US), but a
        # genuinely separate Czech secondary location still qualifies.
        categories = job.get("categories") or {}
        if not isinstance(categories, dict):
            return False
        primary = categories.get("location")
        return any(has_czech_location(value) for value in posting_locations(job) if value != primary)
    return any(has_czech_location(value) for value in posting_locations(job))


def build_location_text(job: dict[str, Any]) -> str | None:
    locations = posting_locations(job)
    czech = [value for value in locations if has_czech_location(value)]
    if czech:
        return " | ".join(czech)
    if str(job.get("country") or "").strip().upper() == "CZ" and locations:
        return " | ".join(locations)
    return None


def clean_html(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def text_value(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def build_description(job: dict[str, Any]) -> str | None:
    # descriptionPlain already includes openingPlain; do not prepend it twice.
    main = (text_value(job.get("descriptionPlain")) or clean_html(job.get("description"))
            or text_value(job.get("openingPlain")) or clean_html(job.get("opening")))
    parts = [main] if main else []
    lists = job.get("lists")
    for section in lists if isinstance(lists, list) else []:
        if isinstance(section, dict):
            content = clean_html(section.get("content"))
            if content:
                heading = text_value(section.get("text"))
                parts.append(f"{heading}\n{content}" if heading else content)
    additional = text_value(job.get("additionalPlain")) or clean_html(job.get("additional"))
    if additional:
        parts.append(additional)
    return "\n\n".join(parts) or None


def normalize_remote_type(job: dict[str, Any]) -> str:
    value = str(job.get("workplaceType") or "").strip().lower()
    return {"on-site": "onsite", "onsite": "onsite", "remote": "remote", "hybrid": "hybrid"}.get(value, "unknown")


def salary_text(job: dict[str, Any]) -> str | None:
    parts = []
    if job.get("salaryRange"):
        parts.append(json.dumps(job["salaryRange"], ensure_ascii=False))
    description = text_value(job.get("salaryDescriptionPlain")) or clean_html(job.get("salaryDescription"))
    if description:
        parts.append(description)
    return "\n".join(parts) or None


def ingest_lever_jobs(site: str | None = None, instance: str | None = None,
                      max_sites: int | None = None) -> dict[str, Any]:
    adapter = LeverAdapter()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            sites = select_sites(discover_sites(cur), site, instance, max_sites)
            cur.execute("""insert into public.ingestion_runs (source_name, status, metadata)
                values (%s, 'running', %s) returning id""",
                (SOURCE_NAME, Jsonb({"site": site, "instance": instance, "site_count": len(sites)})))
            run_id = cur.fetchone()[0]
            conn.commit()
            fetched = czech_jobs = created = attached = updated = failed = successful_sites = 0
            site_errors = []
            site_results = []
            try:
                for tenant in sites:
                    try:
                        jobs = adapter.list_postings(tenant.site, instance=tenant.instance)
                    except Exception as exc:
                        site_errors.append({"instance": tenant.instance, "site": tenant.site, "error": str(exc)})
                        continue
                    fetched += len(jobs)
                    selected = [job for job in jobs if is_czech_job(job)]
                    czech_jobs += len(selected)
                    before = (created, attached, updated, failed)
                    for raw_job in selected:
                        posting_id = normalize_posting_id(raw_job.get("id"))
                        title = text_value(raw_job.get("text"))
                        if posting_id is None or title is None:
                            failed += 1
                            continue
                        # Provided native URLs must agree with the API scope/ID.
                        wanted = LeverIdentity(tenant.instance, tenant.site, posting_id)
                        source_url = text_value(raw_job.get("hostedUrl"))
                        apply_url = text_value(raw_job.get("applyUrl"))
                        if any(parse_lever_url(url) != wanted for url in (source_url, apply_url) if url):
                            failed += 1
                            continue
                        identity = source_job_id(tenant.instance, tenant.site, posting_id)
                        categories = raw_job.get("categories") or {}
                        employment = text_value(categories.get("commitment")) if isinstance(categories, dict) else None
                        company_id = get_or_create_company(cur, tenant.company)
                        description = build_description(raw_job)
                        location = build_location_text(raw_job)
                        remote = normalize_remote_type(raw_job)
                        salary = salary_text(raw_job)
                        now = datetime.now(timezone.utc)
                        cur.execute("""select js.id, js.job_id from public.job_sources js
                            where js.source_name = %s and js.source_job_id = %s""", (SOURCE_NAME, identity))
                        existing = cur.fetchone()
                        new_job = False
                        if existing:
                            source_id, job_id = existing
                        else:
                            job_id = find_existing_lever_job(cur, tenant.instance, tenant.site, posting_id)
                            if job_id is None:
                                cur.execute("""insert into public.jobs (
                                    company_id, title, description, location_text, country_code,
                                    remote_type, employment_type, salary_text, skills, canonical_url,
                                    first_seen_at, last_seen_at, last_verified_at, status)
                                    values (%s, %s, %s, %s, 'CZ', %s, %s, %s, %s, %s, %s, %s, %s, 'active')
                                    returning id""", (company_id, title, description, location, remote,
                                    employment, salary, Jsonb([]), source_url, now, now, now))
                                job_id = cur.fetchone()[0]
                                new_job = True
                        if not new_job:
                            update_canonical_job(cur, job_id, SOURCE_NAME, {
                                "company_id": company_id, "title": title, "description": description,
                                "location_text": location, "country_code": "CZ", "remote_type": remote,
                                "employment_type": employment, "salary_text": salary, "canonical_url": source_url,
                            }, now, preserve_if_none=("description", "location_text", "employment_type", "salary_text", "canonical_url"))
                        if existing:
                            cur.execute("""update public.job_sources set source_url = %s, apply_url = %s,
                                raw_payload = %s, last_seen_at = %s, last_verified_at = %s,
                                is_active = true, updated_at = %s where id = %s""",
                                (source_url, apply_url, Jsonb(raw_job), now, now, now, source_id))
                            updated += 1
                        else:
                            cur.execute("""insert into public.job_sources (
                                job_id, source_name, source_job_id, source_url, apply_url, raw_payload,
                                first_seen_at, last_seen_at, last_verified_at, is_active)
                                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, true)""",
                                (job_id, SOURCE_NAME, identity, source_url, apply_url, Jsonb(raw_job), now, now, now))
                            if new_job:
                                created += 1
                            else:
                                attached += 1
                    delta = [value - previous for value, previous in zip((created, attached, updated, failed), before)]
                    if not selected or sum(delta[:3]) > 0:
                        successful_sites += 1
                    else:
                        site_errors.append({"instance": tenant.instance, "site": tenant.site,
                                            "error": "No Czech postings could be processed"})
                    site_results.append({"instance": tenant.instance, "site": tenant.site,
                        "fetched": len(jobs), "czech_jobs": len(selected),
                        **dict(zip(("created", "attached", "updated", "failed"), delta))})
                status = direct_run_status(len(sites), successful_sites)
                metadata = {"site": site, "instance": instance, "site_count": len(sites),
                    "czech_jobs": czech_jobs, "attached": attached, "successful_sites": successful_sites,
                    "site_errors": site_errors, "site_results": site_results}
                cur.execute("""update public.ingestion_runs set finished_at = now(), status = %s,
                    error_message = %s, records_fetched = %s, records_created = %s,
                    records_updated = %s, records_failed = %s, metadata = %s where id = %s""",
                    (status, ALL_TENANTS_FAILED if status == "failed" else None, fetched, created,
                     updated + attached, failed, Jsonb(metadata), run_id))
                conn.commit()
                return {"status": status, "run_id": str(run_id), "sites": len(sites),
                    "fetched": fetched, "czech_jobs": czech_jobs, "created": created,
                    "attached": attached, "updated": updated, "failed": failed,
                    "successful_sites": successful_sites, "site_errors": site_errors, "site_results": site_results}
            except Exception as exc:
                conn.rollback()
                with conn.cursor() as error_cur:
                    error_cur.execute("""update public.ingestion_runs set finished_at = now(),
                        status = 'failed', error_message = %s where id = %s""", (str(exc), run_id))
                conn.commit()
                raise
