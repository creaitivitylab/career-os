"""Workday ingestion using the existing canonical/source/run architecture."""
import html
import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.adapters.workday import (
    WorkdayAdapter, WorkdayAPIError, WorkdaySite, czech_evidence,
    czech_location, fold_text, normalize_external_path, parse_workday_url, source_job_id,
)
from app.canonical import update_canonical_job
from app.lifecycle import LifecycleRun, InventoryCompletion
from app.ingestion import get_or_create_company
from app.ingestion_status import ALL_TENANTS_FAILED, direct_run_status


SOURCE_NAME = "workday_direct"
FANTASTIC_WORKDAY_ROWS_SQL = """
    select js.job_id, coalesce(nullif(js.raw_payload ->> 'organization', ''), c.name),
           js.source_url, js.apply_url,
           js.raw_payload ->> 'url', js.raw_payload ->> 'job_url'
    from public.job_sources js
    join public.jobs j on j.id = js.job_id
    left join public.companies c on c.id = j.company_id
    where js.source_name = 'fantastic_jobs_apify'
      and (lower(js.raw_payload ->> 'source') = 'workday'
           or js.source_url ilike '%%myworkdayjobs.com/%%'
           or js.source_url ilike '%%myworkdaysite.com/%%'
           or js.apply_url ilike '%%myworkdayjobs.com/%%'
           or js.apply_url ilike '%%myworkdaysite.com/%%'
           or js.raw_payload ->> 'url' ilike '%%myworkdayjobs.com/%%'
           or js.raw_payload ->> 'url' ilike '%%myworkdaysite.com/%%')
"""


def fantastic_workday_rows(cur):
    cur.execute(FANTASTIC_WORKDAY_ROWS_SQL)
    return cur.fetchall()


def discover_sites(rows) -> list[WorkdaySite]:
    boards, companies = {}, {}
    for _, company, *urls in rows:
        parsed = [identity for value in urls if (identity := parse_workday_url(value))]
        scopes = {identity.board.scope for identity in parsed}
        if len(scopes) != 1:
            continue
        scope = next(iter(scopes))
        boards.setdefault(scope, parsed[0].board)
        companies.setdefault(scope, set())
        if company:
            companies[scope].add(company)
    return [replace(boards[scope], company=next(iter(names)) if len(names) == 1 else boards[scope].tenant)
            for scope, names in sorted(companies.items())]


def select_sites(discovered, host=None, tenant=None, site=None, max_sites=None, locale=None):
    explicit = (host, tenant, site)
    if any(value is not None for value in explicit) and not all(value is not None for value in explicit):
        raise ValueError("Specify host, tenant and site together")
    if host is not None:
        wanted = WorkdaySite(host, tenant, site, locale=locale or "en-US")
        selected = [board for board in discovered if board.scope == wanted.scope] or [replace(wanted, company=wanted.tenant)]
    else:
        selected = list(discovered)
    if locale is not None:
        selected = [replace(board, locale=locale) for board in selected]
    if max_sites is not None:
        if max_sites < 1:
            raise ValueError("max_sites must be positive")
        selected = selected[:max_sites]
    return selected


def build_attachment_index(rows):
    matches = {}
    for job_id, _, *urls in rows:
        identities = {parsed.identity for value in urls
                      if (parsed := parse_workday_url(value)) and parsed.external_path}
        # Conflicting posting paths on a Fantastic source row are unsafe evidence.
        if len(identities) == 1:
            matches.setdefault(next(iter(identities)), set()).add(job_id)
    return matches


def find_existing_workday_job(index, board, external_path):
    path = normalize_external_path(external_path)
    candidates = index.get((*board.scope, path), set()) if path else set()
    return next(iter(candidates)) if len(candidates) == 1 else None


def text_value(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def description_text(value):
    if not isinstance(value, str):
        return None
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split()) or None


def remote_type(value):
    text = fold_text(value) if isinstance(value, str) else ""
    if "hybrid" in text or "partially remote" in text:
        return "hybrid"
    if "remote" in text:
        return "remote"
    if text in ("on site", "onsite", "fully onsite", "fully on site", "in person"):
        return "onsite"
    return None


def published_at(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def location_text(info, evidence):
    values = [text_value(info.get("location"))]
    additional = info.get("additionalLocations")
    if isinstance(additional, list):
        values.extend(value for value in additional if czech_location(value))
    if evidence["kind"] != "primary_country":
        values = evidence["locations"]
    return " | ".join(dict.fromkeys(value for value in values if value)) or None


COUNTERS = (
    "listing_calls", "listing_records", "candidate_records", "facet_candidates", "fallback_skipped_foreign",
    "pagination_boundary_confirmed", "listing_missing_path",
    "facet_queries", "facet_listing_records",
    "details_requested", "czech_jobs", "primary_country_jobs", "additional_location_jobs",
    "created", "attached", "updated", "forbidden_403", "s22", "unavailable_404", "missing_identity",
    "malformed", "transient", "duplicate_native_ids", "failed",
)


def persist_posting(cur, board, listing, detail, evidence, attachment_index, now):
    info = detail["jobPostingInfo"]
    identity = source_job_id(board, info.get("id"))
    path = normalize_external_path(listing["externalPath"])
    source_url = text_value(info.get("externalUrl")) or board.public_base + path
    # Only an explicitly supplied apply URL is stored; do not manufacture one.
    apply_url = text_value(info.get("applyUrl"))
    if apply_url:
        parsed = parse_workday_url(apply_url)
        if parsed is None or parsed.identity != (*board.scope, path):
            raise WorkdayAPIError("malformed", "Workday apply URL does not match the posting")
    company_id = get_or_create_company(cur, board.company or board.tenant)
    fields = {"company_id": company_id, "title": info["title"].strip(),
        "description": description_text(info.get("jobDescription")),
        "location_text": location_text(info, evidence), "country_code": "CZ",
        "remote_type": remote_type(info.get("remoteType")), "employment_type": text_value(info.get("timeType")),
        "canonical_url": source_url, "published_at": published_at(info.get("startDate"))}
    raw = {"listing": listing, "detail": detail,
        "workday_site": {"host": board.host, "cluster": board.cluster, "tenant": board.tenant,
                         "site": board.site, "locale": board.locale, "family": board.family, "api_base": board.api_base},
        "czech_eligibility": evidence}
    cur.execute("""select js.id, js.job_id from public.job_sources js
        where js.source_name = %s and js.source_job_id = %s""", (SOURCE_NAME, identity))
    existing = cur.fetchone()
    new_job = False
    if existing:
        source_id, job_id = existing
    else:
        job_id = find_existing_workday_job(attachment_index, board, path)
        if job_id is None:
            cur.execute("""insert into public.jobs (company_id, title, description, location_text,
                country_code, remote_type, employment_type, skills, canonical_url, published_at,
                first_seen_at, last_seen_at, last_verified_at, status)
                values (%s, %s, %s, %s, 'CZ', %s, %s, %s, %s, %s, %s, %s, %s, 'active') returning id""",
                (company_id, fields["title"], fields["description"], fields["location_text"],
                 fields["remote_type"] or "unknown", fields["employment_type"], Jsonb([]), source_url,
                 fields["published_at"], now, now, now))
            job_id = cur.fetchone()[0]
            new_job = True
    if not new_job:
        update_canonical_job(cur, job_id, SOURCE_NAME, fields, now,
            preserve_if_none=("description", "location_text", "remote_type", "employment_type", "published_at"))
    if existing:
        cur.execute("""update public.job_sources set source_url = %s, apply_url = %s, raw_payload = %s,
            last_seen_at = %s, last_verified_at = %s, is_active = true, updated_at = %s where id = %s""",
            (source_url, apply_url, Jsonb(raw), now, now, now, source_id))
        return "updated"
    cur.execute("""insert into public.job_sources (job_id, source_name, source_job_id, source_url,
        apply_url, raw_payload, first_seen_at, last_seen_at, last_verified_at, is_active)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, true)""",
        (job_id, SOURCE_NAME, identity, source_url, apply_url, Jsonb(raw), now, now, now))
    return "created" if new_job else "attached"


def record_detail_error(counters, errors, path, exc):
    key = {"forbidden": "forbidden_403", "unavailable": "unavailable_404",
           "missing_identity": "missing_identity", "malformed": "malformed", "transient": "transient"}.get(exc.kind)
    if key:
        counters[key] += 1
    if exc.error_code == "S22":
        counters["s22"] += 1
    if exc.kind != "unavailable":
        counters["failed"] += 1
    if len(errors) < 10:
        errors.append({"path": path, "kind": exc.kind, "error": str(exc)})


def ingest_workday_jobs(host=None, tenant=None, site=None, max_sites=None, locale=None) -> dict[str, Any]:
    # Validate explicit scope before opening a database connection.
    select_sites([], host, tenant, site, max_sites, locale)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            rows = fantastic_workday_rows(cur)
            discovered = discover_sites(rows)
            sites = select_sites(discovered, host, tenant, site, max_sites, locale)
            attachments = build_attachment_index(rows)
            request = {"host": host, "tenant": tenant, "site": site, "max_sites": max_sites, "locale": locale}
            cur.execute("""insert into public.ingestion_runs (source_name, status, metadata)
                values (%s, 'running', %s) returning id""", (SOURCE_NAME, Jsonb(request)))
            run_id = cur.fetchone()[0]
            conn.commit()
            totals = dict.fromkeys(COUNTERS, 0)
            successful_sites = 0
            site_errors, site_results = [], []
            try:
                lifecycle = LifecycleRun(cur, SOURCE_NAME, run_id)
                for board in sites:
                    inventory = lifecycle.scope(board.host, board.tenant, board.site)
                    counters = dict.fromkeys(COUNTERS, 0)
                    errors, usable_details, unavailable, native_ids = [], 0, 0, set()
                    result = {"host": board.host, "tenant": board.tenant, "site": board.site}
                    with WorkdayAdapter() as adapter:
                        try:
                            discovery = adapter.discover_candidates(board)
                        except WorkdayAPIError as exc:
                            counters.update(adapter.counters)
                            counters["failed"] += counters["listing_missing_path"]
                            key = {"forbidden": "forbidden_403", "unavailable": "unavailable_404",
                                   "malformed": "malformed", "transient": "transient"}.get(exc.kind)
                            if key:
                                counters[key] += 1
                            if exc.error_code == "S22":
                                counters["s22"] += 1
                            counters["failed"] += 1
                            result.update(status="failed", error=str(exc), discovery_mode="failed")
                            site_errors.append({**result, "kind": exc.kind})
                        else:
                            counters.update(discovery.counters)
                            counters["failed"] += counters["listing_missing_path"]
                            result.update(discovery_mode="geography_facet" if discovery.geography else "fallback",
                                          geography=discovery.geography)
                            for listing in discovery.jobs:
                                path = listing["externalPath"]
                                counters["details_requested"] += 1
                                try:
                                    detail = adapter.get_detail(board, path)
                                    identity = source_job_id(board, detail["jobPostingInfo"].get("id"))
                                    inventory.observe(identity)
                                    if identity in native_ids:
                                        counters["duplicate_native_ids"] += 1
                                        continue
                                    native_ids.add(identity)
                                    evidence = czech_evidence(detail)
                                    if evidence is not None:
                                        outcome = persist_posting(cur, board, listing, detail, evidence, attachments,
                                                                  datetime.now(timezone.utc))
                                        counters[outcome] += 1
                                        counters["czech_jobs"] += 1
                                        counters[evidence["kind"] + "_jobs"] += 1
                                    usable_details += 1
                                except WorkdayAPIError as exc:
                                    record_detail_error(counters, errors, path, exc)
                                    unavailable += exc.kind == "unavailable"
                            # Empty/foreign-only catalogs and postings withdrawn with 404
                            # are successful checks. 403/S22 is never an expiry signal.
                            success = (not discovery.jobs and not counters["listing_missing_path"]) or usable_details > 0 or (
                                unavailable == len(discovery.jobs) and counters["failed"] == 0)
                            result["status"] = "success" if success else "failed"
                            if success:
                                successful_sites += 1
                            else:
                                site_errors.append({**result, "error": "No listing candidate produced usable detail data"})
                    complete = (result.get("status") == "success" and not counters["failed"]
                                and result.get("discovery_mode") == "fallback"
                                and not counters["fallback_skipped_foreign"]
                                and not counters["listing_missing_path"])
                    result["lifecycle"] = inventory.finish(InventoryCompletion(
                        result.get("status") == "success", complete,
                        "complete unfiltered native inventory" if complete else
                        "filtered geography, failed detail, or uncertain inventory"))
                    result.update(counters, detail_errors=errors)
                    site_results.append(result)
                    for key in COUNTERS:
                        totals[key] += counters[key]
                status = direct_run_status(len(sites), successful_sites)
                metadata = {"lifecycle": lifecycle.summary(), **request, "sites_discovered": len(discovered), "sites": len(sites),
                    "fantastic_inventory_rows": len(rows),
                    "successful_sites": successful_sites, **totals, "site_errors": site_errors, "site_results": site_results}
                cur.execute("""update public.ingestion_runs set finished_at = now(), status = %s,
                    error_message = %s, records_fetched = %s, records_created = %s,
                    records_updated = %s, records_failed = %s, metadata = %s where id = %s""",
                    (status, ALL_TENANTS_FAILED if status == "failed" else None, totals["listing_records"],
                     totals["created"], totals["updated"] + totals["attached"], totals["failed"], Jsonb(metadata), run_id))
                conn.commit()
                return {"status": status, "run_id": str(run_id), **metadata}
            except Exception as exc:
                conn.rollback()
                with conn.cursor() as error_cur:
                    error_cur.execute("""update public.ingestion_runs set finished_at = now(),
                        status = 'failed', error_message = %s where id = %s""", (str(exc), run_id))
                conn.commit()
                raise
