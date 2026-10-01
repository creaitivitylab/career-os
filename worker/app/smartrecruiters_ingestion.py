import html
import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from app.adapters.smartrecruiters import (
    SmartRecruitersAdapter,
)
from app.ingestion import get_or_create_company


SOURCE_NAME = "smartrecruiters_direct"


def clean_html(value: str | None) -> str | None:
    if not value:
        return None

    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)

    return " ".join(text.split())


def normalize_url(value: str | None) -> str | None:
    if not value:
        return None

    value = value.strip()

    value = value.split("#", 1)[0]
    value = value.split("?", 1)[0]

    return value.rstrip("/").lower()


def discover_tenants(
    cur: psycopg.Cursor,
) -> list[str]:

    cur.execute(
        """
        select distinct
            js.source_url
        from public.job_sources js
        where js.source_name = 'fantastic_jobs_apify'
          and js.raw_payload ->> 'source'
                = 'smartrecruiters'
          and js.source_url is not null
          and js.source_url ilike
                'https://jobs.smartrecruiters.com/%'
        """
    )

    tenants: set[str] = set()

    for row in cur.fetchall():

        source_url = row[0]

        try:
            path = urlsplit(
                source_url
            ).path.strip("/")

            parts = path.split("/")

            if parts and parts[0]:
                tenants.add(parts[0])

        except Exception:
            continue

    return sorted(tenants)


def build_description(
    raw_job: dict[str, Any],
) -> str | None:

    job_ad = raw_job.get("jobAd") or {}

    sections = (
        job_ad.get("sections")
        if isinstance(job_ad, dict)
        else {}
    ) or job_ad

    parts = []

    for name in (
        "jobDescription",
        "qualifications",
        "additionalInformation",
    ):

        section = sections.get(name)

        if not section:
            continue

        if isinstance(section, dict):
            value = section.get("text")
        else:
            value = section

        cleaned = clean_html(
            str(value)
            if value
            else None
        )

        if cleaned:
            parts.append(cleaned)

    if not parts:
        return None

    return "\n\n".join(parts)


def build_location_text(
    raw_job: dict[str, Any],
) -> str | None:

    location = raw_job.get("location") or {}

    values = [
        location.get("city"),
        location.get("region"),
        location.get("country"),
    ]

    values = [
        str(value)
        for value in values
        if value
    ]

    if not values:
        return None

    return ", ".join(values)


def normalize_remote_type(
    raw_job: dict[str, Any],
) -> str:

    location = raw_job.get("location") or {}

    if location.get("remote") is True:
        return "remote"

    return "unknown"


def find_existing_job_by_url(
    cur: psycopg.Cursor,
    source_url: str | None,
) -> str | None:

    normalized = normalize_url(source_url)

    if not normalized:
        return None

    cur.execute(
        """
        select j.id
        from public.jobs j

        left join public.job_sources js
            on js.job_id = j.id

        where
            lower(
                rtrim(
                    split_part(
                        coalesce(
                            js.source_url,
                            ''
                        ),
                        '?',
                        1
                    ),
                    '/'
                )
            ) = %s

            or

            lower(
                rtrim(
                    split_part(
                        coalesce(
                            j.canonical_url,
                            ''
                        ),
                        '?',
                        1
                    ),
                    '/'
                )
            ) = %s

        limit 1
        """,
        (
            normalized,
            normalized,
        ),
    )

    row = cur.fetchone()

    return row[0] if row else None


def ingest_smartrecruiters_jobs(
    company_identifier: str | None = None,
    country: str = "cz",
    max_companies: int | None = None,
) -> dict[str, Any]:

    database_url = os.environ["DATABASE_URL"]

    adapter = SmartRecruitersAdapter()

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:

            if company_identifier:

                tenants = [
                    company_identifier
                ]

            else:

                tenants = discover_tenants(cur)

            if max_companies is not None:
                tenants = tenants[:max_companies]

            cur.execute(
                """
                insert into public.ingestion_runs (
                    source_name,
                    status,
                    metadata
                )
                values (
                    %s,
                    'running',
                    %s
                )
                returning id
                """,
                (
                    SOURCE_NAME,
                    Jsonb({
                        "country": country,
                        "company_identifier":
                            company_identifier,
                        "tenant_count":
                            len(tenants),
                    }),
                ),
            )

            run_id = cur.fetchone()[0]
            conn.commit()

            fetched = 0
            details_loaded = 0
            created = 0
            attached = 0
            updated = 0
            failed = 0

            tenant_errors = []

            try:

                for tenant in tenants:

                    try:

                        postings = (
                            adapter.list_postings(
                                tenant,
                                country=country,
                            )
                        )

                    except Exception as exc:

                        tenant_errors.append({
                            "tenant": tenant,
                            "error": str(exc),
                        })

                        continue

                    fetched += len(postings)

                    for posting in postings:

                        posting_id = posting.get("id")

                        if not posting_id:
                            failed += 1
                            continue

                        try:

                            raw_job = (
                                adapter.get_posting(
                                    tenant,
                                    str(posting_id),
                                )
                            )

                            details_loaded += 1

                        except Exception:
                            failed += 1
                            continue

                        source_job_id = (
                            f"{tenant}:"
                            f"{posting_id}"
                        )

                        company_data = (
                            raw_job.get("company")
                            or {}
                        )

                        company_name = (
                            company_data.get("name")
                            or tenant
                        )

                        company_id = (
                            get_or_create_company(
                                cur,
                                company_name,
                            )
                        )

                        title = (
                            raw_job.get("name")
                            or "Unknown position"
                        ).strip()

                        description = (
                            build_description(
                                raw_job
                            )
                        )

                        location_text = (
                            build_location_text(
                                raw_job
                            )
                        )

                        employment = (
                            raw_job.get(
                                "typeOfEmployment"
                            )
                            or {}
                        )

                        employment_type = (
                            employment.get("label")
                            if isinstance(
                                employment,
                                dict,
                            )
                            else None
                        )

                        remote_type = (
                            normalize_remote_type(
                                raw_job
                            )
                        )

                        source_url = (
                            raw_job.get(
                                "postingUrl"
                            )
                        )

                        apply_url = (
                            raw_job.get(
                                "applyUrl"
                            )
                            or source_url
                        )

                        published_at = (
                            raw_job.get(
                                "releasedDate"
                            )
                        )

                        compensation = (
                            raw_job.get(
                                "compensation"
                            )
                        )

                        salary_text = (
                            json.dumps(
                                compensation,
                                ensure_ascii=False,
                            )
                            if compensation
                            else None
                        )

                        now = datetime.now(
                            timezone.utc
                        )

                        cur.execute(
                            """
                            select
                                js.id,
                                js.job_id
                            from public.job_sources js
                            where js.source_name = %s
                              and js.source_job_id = %s
                            """,
                            (
                                SOURCE_NAME,
                                source_job_id,
                            ),
                        )

                        existing_source = (
                            cur.fetchone()
                        )

                        new_job = False
                        attached_job = False

                        if existing_source:

                            source_id = (
                                existing_source[0]
                            )

                            job_id = (
                                existing_source[1]
                            )

                        else:

                            source_id = None

                            job_id = (
                                find_existing_job_by_url(
                                    cur,
                                    source_url,
                                )
                            )

                            if job_id:
                                attached_job = True

                            else:

                                cur.execute(
                                    """
                                    insert into public.jobs (
                                        company_id,
                                        title,
                                        description,
                                        location_text,
                                        country_code,
                                        remote_type,
                                        employment_type,
                                        salary_text,
                                        skills,
                                        canonical_url,
                                        published_at,
                                        first_seen_at,
                                        last_seen_at,
                                        last_verified_at,
                                        status
                                    )
                                    values (
                                        %s, %s, %s, %s,
                                        'CZ', %s, %s, %s,
                                        %s, %s, %s,
                                        %s, %s, %s,
                                        'active'
                                    )
                                    returning id
                                    """,
                                    (
                                        company_id,
                                        title,
                                        description,
                                        location_text,
                                        remote_type,
                                        employment_type,
                                        salary_text,
                                        Jsonb([]),
                                        source_url,
                                        published_at,
                                        now,
                                        now,
                                        now,
                                    ),
                                )

                                job_id = (
                                    cur.fetchone()[0]
                                )

                                new_job = True

                        if not new_job:

                            cur.execute(
                                """
                                update public.jobs
                                set
                                    company_id = %s,
                                    title = %s,
                                    description = %s,
                                    location_text = %s,
                                    country_code = 'CZ',
                                    remote_type = %s,
                                    employment_type = %s,
                                    salary_text =
                                        coalesce(%s, salary_text),
                                    canonical_url =
                                        coalesce(%s, canonical_url),
                                    published_at =
                                        coalesce(
                                            %s,
                                            published_at
                                        ),
                                    last_seen_at = %s,
                                    last_verified_at = %s,
                                    status = 'active',
                                    updated_at = %s
                                where id = %s
                                """,
                                (
                                    company_id,
                                    title,
                                    description,
                                    location_text,
                                    remote_type,
                                    employment_type,
                                    salary_text,
                                    source_url,
                                    published_at,
                                    now,
                                    now,
                                    now,
                                    job_id,
                                ),
                            )

                        raw_payload = dict(raw_job)

                        raw_payload[
                            "_company_identifier"
                        ] = tenant

                        if existing_source:

                            cur.execute(
                                """
                                update public.job_sources
                                set
                                    source_url = %s,
                                    apply_url = %s,
                                    raw_payload = %s,
                                    last_seen_at = %s,
                                    last_verified_at = %s,
                                    is_active = true,
                                    updated_at = %s
                                where id = %s
                                """,
                                (
                                    source_url,
                                    apply_url,
                                    Jsonb(raw_payload),
                                    now,
                                    now,
                                    now,
                                    source_id,
                                ),
                            )

                            updated += 1

                        else:

                            cur.execute(
                                """
                                insert into public.job_sources (
                                    job_id,
                                    source_name,
                                    source_job_id,
                                    source_url,
                                    apply_url,
                                    raw_payload,
                                    first_seen_at,
                                    last_seen_at,
                                    last_verified_at,
                                    is_active
                                )
                                values (
                                    %s, %s, %s, %s,
                                    %s, %s, %s, %s,
                                    %s, true
                                )
                                """,
                                (
                                    job_id,
                                    SOURCE_NAME,
                                    source_job_id,
                                    source_url,
                                    apply_url,
                                    Jsonb(raw_payload),
                                    now,
                                    now,
                                    now,
                                ),
                            )

                            if new_job:
                                created += 1

                            elif attached_job:
                                attached += 1

                cur.execute(
                    """
                    update public.ingestion_runs
                    set
                        finished_at = now(),
                        status = 'success',
                        records_fetched = %s,
                        records_created = %s,
                        records_updated = %s,
                        metadata = %s
                    where id = %s
                    """,
                    (
                        fetched,
                        created,
                        updated + attached,
                        Jsonb({
                            "country": country,
                            "company_identifier":
                                company_identifier,
                            "tenant_count":
                                len(tenants),
                            "details_loaded":
                                details_loaded,
                            "attached":
                                attached,
                            "failed":
                                failed,
                            "tenant_errors":
                                tenant_errors,
                        }),
                        run_id,
                    ),
                )

                conn.commit()

                return {
                    "status": "success",
                    "run_id": str(run_id),
                    "tenants": len(tenants),
                    "fetched": fetched,
                    "details_loaded":
                        details_loaded,
                    "created": created,
                    "attached": attached,
                    "updated": updated,
                    "failed": failed,
                    "tenant_errors":
                        tenant_errors,
                }

            except Exception as exc:

                conn.rollback()

                with conn.cursor() as error_cur:

                    error_cur.execute(
                        """
                        update public.ingestion_runs
                        set
                            finished_at = now(),
                            status = 'failed',
                            error_message = %s
                        where id = %s
                        """,
                        (
                            str(exc),
                            run_id,
                        ),
                    )

                conn.commit()

                raise
