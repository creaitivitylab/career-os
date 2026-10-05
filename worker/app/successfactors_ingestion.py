"""RMK-only ingestion using the shared canonical/source/run architecture."""
import logging
import os
import time
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from app.adapters.successfactors import (
    RMKError, RMKSite, SuccessFactorsAdapter, classify_platform, czech_evidence,
    ineligible_reason, parse_rmk_url, public_url, source_job_id,
)
from app.canonical import update_canonical_job
from app.lifecycle import LifecycleRun, InventoryCompletion
from app.ingestion import get_or_create_company
from app.ingestion_status import ALL_TENANTS_FAILED, direct_run_status
from app.successfactors_scopes import apply_scope_gate, load_scope_gate


SOURCE_NAME = "successfactors_direct"
logger = logging.getLogger("uvicorn.error")
FANTASTIC_ROWS_SQL = """
    select js.job_id, coalesce(nullif(js.raw_payload ->> 'organization', ''), c.name),
           js.source_url, js.apply_url, js.raw_payload ->> 'url', js.raw_payload ->> 'job_url'
    from public.job_sources js
    join public.jobs j on j.id = js.job_id
    left join public.companies c on c.id = j.company_id
    where js.source_name = 'fantastic_jobs_apify'
      and lower(js.raw_payload ->> 'source') in ('successfactors', 'sap_successfactors', 'sap successfactors')
"""


def discover_sites(rows):
    sites, names, classifications = {}, {}, []
    for job_id, company, *urls in rows:
        unique_urls = list(dict.fromkeys(value for value in urls if value))
        native = {tuple((p[k] for k in ("host", "brand", "posting_id")))
                  for url in unique_urls if (p := parse_rmk_url(url))}
        # Conflicting source/apply identities cannot define a trustworthy site.
        if len(native) != 1:
            classification = "dream_jobs" if any("/jobs/detail/" in url for url in unique_urls) else "unknown"
            if any(classify_platform(url) == "migrated_sap" for url in unique_urls):
                classification = "migrated_sap"
            classifications.append(classification)
            continue
        host, brand, _ = next(iter(native))
        url = next(url for url in unique_urls if parse_rmk_url(url))
        if classify_platform(url) == "migrated_sap":
            classifications.append("migrated_sap")
            continue
        scope = host, brand
        sites.setdefault(scope, RMKSite(host, brand, url, company or host))
        names.setdefault(scope, set()).add(company or host)
        classifications.append("rmk_candidate")
    result = [RMKSite(s.host, s.brand, s.seed_url, next(iter(names[key])) if len(names[key]) == 1 else s.host)
              for key, s in sorted(sites.items())]
    return result, dict(Counter(classifications))


def select_sites(discovered, host=None, brand=None, max_sites=None):
    if brand is not None and host is None:
        raise ValueError("Specify host with brand")
    if host is not None:
        wanted = public_url("https://" + host + "/")
        host = urlsplit(wanted).hostname
        selected = [s for s in discovered if s.host == host and (brand is None or s.brand == brand.strip("/"))]
        if not selected:
            raise ValueError("Explicit RMK scope must exist in the Fantastic inventory")
        if len(selected) > 1 and brand is None:
            raise ValueError("Host has multiple discovered brands; specify brand")
    else:
        selected = list(discovered)
    if max_sites is not None:
        if max_sites < 1:
            raise ValueError("max_sites must be positive")
        selected = selected[:max_sites]
    return selected


def attachment_candidates(rows, config, posting_id):
    """All exact native-ID matches across every brand/slug on verified hosts."""
    matches = set()
    for job_id, _, *urls in rows:
        identities = {(p["host"], p["posting_id"]) for url in urls if (p := parse_rmk_url(url))}
        # Aliases verified against this native tenant may differ in hostname.
        if identities and all(host in config.hosts and pid == posting_id for host, pid in identities):
            matches.add(job_id)
    return matches


def find_existing_successfactors_job(rows, config, posting_id):
    source_job_id(config.tenant, posting_id)
    matches = attachment_candidates(rows, config, posting_id)
    return next(iter(matches)) if len(matches) == 1 else None


def published_at(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return parsedate_to_datetime(value)
        except (ValueError, TypeError):
            try:
                return datetime.strptime(value, "%a %b %d %H:%M:%S %Z %Y").replace(tzinfo=timezone.utc)
            except ValueError:
                return None


def persist_posting(cur, site, config, listing, detail, evidence, rows, now):
    identity = source_job_id(config.tenant, detail["posting_id"])
    fields = {"company_id": get_or_create_company(cur, site.company), "title": detail["title"],
              "description": detail["description"], "country_code": "CZ",
              "location_text": " | ".join(dict.fromkeys(m["location"].get("text") or m["location"].get("country")
                                                     for m in evidence["matches"])),
              "employment_type": detail.get("employment_type"), "canonical_url": detail["url"],
              "published_at": published_at(detail.get("posted_date"))}
    if isinstance(fields["employment_type"], list):
        fields["employment_type"] = " | ".join(v for v in fields["employment_type"] if isinstance(v, str)) or None
    workplace = (detail.get("workplace") or "").strip().lower()
    fields["remote_type"] = {"hybrid": "hybrid", "remote": "remote", "onsite": "onsite", "on-site": "onsite"}.get(workplace)
    raw = {"detail": detail, "listing": listing, "rmk_site": config.metadata(), "czech_eligibility": evidence}
    cur.execute("""select js.id, js.job_id from public.job_sources js
                   where js.source_name = %s and js.source_job_id = %s""", (SOURCE_NAME, identity))
    existing = cur.fetchone()
    new_job, ambiguous = False, False
    if existing:
        source_id, job_id = existing
    else:
        matches = attachment_candidates(rows, config, detail["posting_id"])
        ambiguous = len(matches) > 1
        job_id = next(iter(matches)) if len(matches) == 1 else None
        if job_id is None:
            cur.execute("""insert into public.jobs (company_id, title, description, location_text,
                country_code, remote_type, employment_type, skills, canonical_url, published_at,
                first_seen_at, last_seen_at, last_verified_at, status)
                values (%s, %s, %s, %s, 'CZ', %s, %s, %s, %s, %s, %s, %s, %s, 'active') returning id""",
                (fields["company_id"], fields["title"], fields["description"], fields["location_text"],
                 fields["remote_type"] or "unknown", fields["employment_type"], Jsonb([]), fields["canonical_url"],
                 fields["published_at"], now, now, now))
            job_id = cur.fetchone()[0]
            new_job = True
    if not new_job:
        update_canonical_job(cur, job_id, SOURCE_NAME, fields, now,
                             preserve_if_none=("employment_type", "remote_type", "published_at"))
    if existing:
        cur.execute("""update public.job_sources set source_url = %s, apply_url = %s, raw_payload = %s,
            last_seen_at = %s, last_verified_at = %s, is_active = true, updated_at = %s where id = %s""",
                    (detail["url"], None, Jsonb(raw), now, now, now, source_id))
        return "updated", False
    cur.execute("""insert into public.job_sources (job_id, source_name, source_job_id, source_url,
        apply_url, raw_payload, first_seen_at, last_seen_at, last_verified_at, is_active)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, true)""",
                (job_id, SOURCE_NAME, identity, detail["url"], None, Jsonb(raw), now, now, now))
    return "created" if new_job else "attached", ambiguous


COUNTERS = ("details_requested", "czech_jobs", "created", "attached", "updated", "ambiguous_attachment",
            "withdrawn", "missing_identity", "ineligible", "failed", "duplicate_native_ids")


def ingest_successfactors_jobs(host=None, brand=None, locale=None, max_sites=None, validated_scopes=False):
    from app.adapters.successfactors import COUNTERS as ADAPTER_COUNTERS, normalize_locale
    if locale is not None and not normalize_locale(locale):
        raise ValueError("Invalid RMK locale")
    if validated_scopes and any(value is not None for value in (host, brand, locale, max_sites)):
        raise ValueError("Validated-scope mode cannot accept scope or locale overrides")
    gate = load_scope_gate() if validated_scopes else None
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(FANTASTIC_ROWS_SQL)
            rows = cur.fetchall()
            discovered, classifications = discover_sites(rows)
            sites = gate.select(discovered) if gate else select_sites(discovered, host, brand, max_sites)
            # Explicit recovery of a reviewed scope uses the same safety and
            # locale policy as unattended execution. Unreviewed explicit sites
            # retain their established behavior; never expand automatic mode.
            reviewed = gate or (load_scope_gate() if host is not None and locale is None else None)
            rules = {(r.host, r.brand): r for r in reviewed.scopes} if reviewed else {}
            request = {"host": host, "brand": brand, "locale": locale, "max_sites": max_sites,
                       "validated_scopes": validated_scopes}
            if gate:
                request.update(gate.diagnostics(discovered))
            cur.execute("""insert into public.ingestion_runs (source_name, status, metadata)
                values (%s, 'running', %s) returning id""", (SOURCE_NAME, Jsonb(request)))
            run_id = cur.fetchone()[0]
            conn.commit()
            totals = dict.fromkeys((*ADAPTER_COUNTERS, *COUNTERS), 0)
            results, errors, processed = [], [], 0
            seen_native = set()
            try:
                lifecycle = LifecycleRun(cur, SOURCE_NAME, run_id)
                # RMK posting identities cross brands: single-scope ownership
                # cannot be assumed for tenants with multiple reviewed brands.
                tenant_scopes = Counter(r.native_tenant for r in load_scope_gate().scopes)
                for site in sites:
                    inventory = None
                    rule = rules.get((site.host, site.brand))
                    site_locale = (None if rule.locale_union else rule.preferred_locale) if rule else locale
                    started = time.monotonic()
                    counts = dict.fromkeys(COUNTERS, 0)
                    result = {"host": site.host, "brand": site.brand, "detail_errors": [], "ineligible_reasons": {}}
                    logger.info("RMK scope started run=%s host=%s brand=%s", run_id, site.host, site.brand)
                    with SuccessFactorsAdapter() as adapter:
                        try:
                            platform, config = adapter.load_config(site, site_locale, bounded_locale_recall=bool(rule))
                            result["platform"] = platform
                            if platform != "rmk":
                                raise RMKError("unsupported", "Public configuration is not RMK; no ingestion attempted")
                            if rule:
                                result["locale_policy"] = "validated_union" if rule.locale_union else "validated_bounded"
                                apply_scope_gate(config, rule)
                                result["unapproved_locales"] = sorted(set(config.advertised_locales) - set(config.locales))
                            result["config"] = config.metadata()
                            inventory = lifecycle.scope(site.host, config.tenant, config.brand)
                            candidates, modes = adapter.discover_candidates(config)
                            result["discovery"] = modes
                            # Compact audit of historical identities, not a
                            # substitute for current native listing discovery.
                            historical = set()
                            for row in rows:
                                for url in row[2:]:
                                    parsed = parse_rmk_url(url)
                                    if parsed and parsed["host"] in config.hosts:
                                        historical.add(parsed["posting_id"])
                            result["historical_discovered_ids"] = sorted(
                                historical & {j["posting_id"] for j in candidates})
                            result["historical_detail_outcomes"] = {}
                            usable = unavailable = 0
                            for listing in candidates:
                                counts["details_requested"] += 1
                                try:
                                    detail = adapter.get_detail(config, listing)
                                    identity = source_job_id(config.tenant, detail["posting_id"])
                                    inventory.observe(identity)
                                    if identity in seen_native:
                                        counts["duplicate_native_ids"] += 1
                                        usable += 1
                                        if listing["posting_id"] in historical:
                                            result["historical_detail_outcomes"][listing["posting_id"]] = {"status": "czech_duplicate"}
                                        continue
                                    evidence = czech_evidence(detail)
                                    if listing["posting_id"] in historical:
                                        result["historical_detail_outcomes"][listing["posting_id"]] = {
                                            "status": "czech" if evidence else "ineligible",
                                            "reason": None if evidence else ineligible_reason(detail)}
                                    if evidence:
                                        outcome, ambiguous = persist_posting(cur, site, config, listing, detail, evidence, rows,
                                                                            datetime.now(timezone.utc))
                                        counts[outcome] += 1
                                        counts["ambiguous_attachment"] += ambiguous
                                        counts["czech_jobs"] += 1
                                        seen_native.add(identity)
                                    else:
                                        counts["ineligible"] += 1
                                        reason = ineligible_reason(detail)
                                        result["ineligible_reasons"][reason] = result["ineligible_reasons"].get(reason, 0) + 1
                                    usable += 1
                                except RMKError as exc:
                                    if listing["posting_id"] in historical:
                                        result["historical_detail_outcomes"][listing["posting_id"]] = {
                                            "status": exc.kind, "reason": str(exc)}
                                    if exc.kind == "withdrawn":
                                        counts["withdrawn"] += 1
                                        unavailable += 1
                                    else:
                                        counts["failed"] += 1
                                        counts["missing_identity"] += exc.kind == "missing_identity"
                                    if len(result["detail_errors"]) < 10:
                                        result["detail_errors"].append({"posting_id": listing["posting_id"], "kind": exc.kind, "error": str(exc)})
                            if candidates and not usable and unavailable != len(candidates):
                                raise RMKError("unusable", "No current listing candidate produced usable native detail")
                            processed += 1
                            result.update(status="success", discovery_status="resolved")
                        except RMKError as exc:
                            status = "unresolved" if exc.kind == "unresolved" else "unsupported" if exc.kind == "unsupported" else "failed"
                            result.update(status=status, discovery_status=status, kind=exc.kind, error=str(exc))
                            if status == "failed":
                                counts["failed"] += 1
                            errors.append({"host": site.host, "brand": site.brand, "status": status, "kind": exc.kind, "error": str(exc)})
                        if inventory is not None:
                            complete = (result.get("status") == "success" and rule is not None
                                        and not counts["failed"] and not adapter.counters["fallback_skipped_foreign"]
                                        and all(m["mode"] == "fallback" for m in modes)
                                        and set(config.advertised_locales).issubset(config.locales)
                                        and tenant_scopes[config.tenant] == 1)
                            result["lifecycle"] = inventory.finish(InventoryCompletion(
                                result.get("status") == "success", complete,
                                "complete reviewed native inventory" if complete else
                                "filtered/partial/unreviewed inventory or shared brand/locale ownership"))
                        result.update(adapter.counters, **counts, duration_seconds=round(time.monotonic() - started, 3))
                    results.append(result)
                    logger.info("RMK scope completed run=%s host=%s brand=%s status=%s seconds=%s candidates=%s details=%s czech=%s failed=%s",
                                run_id, site.host, site.brand, result["status"], result["duration_seconds"],
                                result["candidate_records"], result["details_requested"], result["czech_jobs"], result["failed"])
                    for key in totals:
                        totals[key] += result[key]
                status = direct_run_status(len(sites), processed)
                metadata = {"lifecycle": lifecycle.summary(), **request, "fantastic_rows_classified": len(rows), "classification_counts": classifications,
                    "sites_discovered": len(discovered), "sites": len(sites), "successful_sites": processed,
                    "unresolved_sites": sum(r["status"] == "unresolved" for r in results),
                    "non_rmk_sites": sum(r["status"] == "unsupported" for r in results),
                    "failed_sites": sum(r["status"] == "failed" for r in results),
                    "requested_scope_complete": processed == len(sites) and not errors and not totals["failed"],
                    "coverage_complete": not validated_scopes and host is None and max_sites is None and processed == len(sites) and not errors and not totals["failed"],
                    **totals, "site_results": results, "site_errors": errors}
                cur.execute("""update public.ingestion_runs set finished_at = now(), status = %s,
                    error_message = %s, records_fetched = %s, records_created = %s,
                    records_updated = %s, records_failed = %s, metadata = %s where id = %s""",
                    (status, ALL_TENANTS_FAILED if status == "failed" else None, totals["listing_records"],
                     totals["created"], totals["attached"] + totals["updated"], totals["failed"], Jsonb(metadata), run_id))
                conn.commit()
                return {"status": status, "run_id": str(run_id), **metadata}
            except Exception as exc:
                conn.rollback()
                with conn.cursor() as error_cur:
                    error_cur.execute("""update public.ingestion_runs set finished_at = now(),
                        status = 'failed', error_message = %s where id = %s""", (type(exc).__name__, run_id))
                conn.commit()
                raise
