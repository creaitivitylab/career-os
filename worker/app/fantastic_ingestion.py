import html
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.adapters.fantastic_jobs import FantasticJobsApifyAdapter
from app.ingestion import get_or_create_company


SOURCE_NAME = "fantastic_jobs_apify"


def clean_html(value: str | None) -> str | None:
    if not value:
        return None

    text = re.sub(r"<[^>]+>", "", value)
    return html.unescape(text).strip()


def build_location_text(raw_job: dict[str, Any]) -> str | None:
    locations = raw_job.get("locations_derived")

    if locations:
        parts = []

        for location in locations:
            if isinstance(location, dict):
                values = [
                    location.get("city"),
                    location.get("admin"),
                    location.get("country"),
                ]
                text = ", ".join(
                    str(value)
                    for value in values
                    if value
                )

                if text:
                    parts.append(text)

            elif location:
                parts.append(str(location))

        if parts:
            return " | ".join(parts)

    alt = raw_job.get("locations_alt")

    if alt:
        return str(alt)

    cities = raw_job.get("cities_derived") or []
    countries = raw_job.get("countries_derived") or []

    values = []

    if cities:
        values.extend(str(x) for x in cities)

    if countries:
        values.extend(str(x) for x in countries)

    return ", ".join(values) if values else None


def normalize_employment_type(
    raw_job: dict[str, Any]
) -> str | None:

    value = (
        raw_job.get("ai_employment_type")
        or raw_job.get("employment_type")
    )

    if isinstance(value, list):
        return ", ".join(str(x) for x in value)

    if value:
        return str(value)

    return None


def normalize_remote_type(
    raw_job: dict[str, Any]
) -> str:

    if raw_job.get("location_type") == "TELECOMMUTE":
        return "remote"

    arrangement = raw_job.get("ai_work_arrangement")

    if arrangement:
        text = str(arrangement).lower()

        if "hybrid" in text:
            return "hybrid"

        if "remote" in text:
            return "remote"

        if "onsite" in text or "on-site" in text:
            return "onsite"

    return "unknown"


def ingest_fantastic_jobs(
    time_range: str = "24h",
    location: str = "Czechia",
    limit: int = 10,
) -> dict[str, Any]:

    database_url = os.environ["DATABASE_URL"]
    adapter = FantasticJobsApifyAdapter()

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:

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
                        "time_range": time_range,
                        "location": location,
                        "limit": limit,
                    }),
                ),
            )

            run_id = cur.fetchone()[0]
            conn.commit()

            created = 0
            updated = 0

            try:
                jobs = adapter.fetch(
                    time_range=time_range,
                    location=location,
                    limit=limit,
                )

                now = datetime.now(timezone.utc)

                for raw_job in jobs:

                    source_job_id = str(
                        raw_job.get("id")
                        or raw_job.get("url")
                    )

                    if not source_job_id:
                        continue

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

                    existing_source = cur.fetchone()

                    company_name = raw_job.get("organization")

                    company_id = get_or_create_company(
                        cur,
                        company_name,
                    )

                    title = (
                        raw_job.get("title")
                        or "Unknown position"
                    ).strip()

                    description = (
                        raw_job.get("description_text")
                        or clean_html(
                            raw_job.get("description_html")
                        )
                    )

                    location_text = build_location_text(
                        raw_job
                    )

                    employment_type = (
                        normalize_employment_type(
                            raw_job
                        )
                    )

                    remote_type = normalize_remote_type(
                        raw_job
                    )

                    skills = (
                        raw_job.get("ai_key_skills")
                        or []
                    )

                    source_url = raw_job.get("url")

                    salary_raw = raw_job.get("salary")

                    salary_text = (
                        json.dumps(
                            salary_raw,
                            ensure_ascii=False,
                        )
                        if salary_raw
                        else None
                    )

                    published_at = raw_job.get(
                        "date_posted"
                    )

                    expires_at = raw_job.get(
                        "date_valid_through"
                    )

                    if existing_source:
                        source_id, job_id = (
                            existing_source
                        )

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
                                salary_text = %s,
                                skills = %s,
                                canonical_url = %s,
                                published_at = %s,
                                expires_at = %s,
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
                                Jsonb(skills),
                                source_url,
                                published_at,
                                expires_at,
                                now,
                                now,
                                now,
                                job_id,
                            ),
                        )

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
                                source_url,
                                Jsonb(raw_job),
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
                                expires_at,
                                first_seen_at,
                                last_seen_at,
                                last_verified_at,
                                status
                            )
                            values (
                                %s, %s, %s, %s,
                                'CZ', %s, %s, %s,
                                %s, %s, %s, %s,
                                %s, %s, %s, 'active'
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
                                Jsonb(skills),
                                source_url,
                                published_at,
                                expires_at,
                                now,
                                now,
                                now,
                            ),
                        )

                        job_id = cur.fetchone()[0]

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
                                %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, true
                            )
                            """,
                            (
                                job_id,
                                SOURCE_NAME,
                                source_job_id,
                                source_url,
                                source_url,
                                Jsonb(raw_job),
                                now,
                                now,
                                now,
                            ),
                        )

                        created += 1

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
                        len(jobs),
                        created,
                        updated,
                        Jsonb({
                            "time_range": time_range,
                            "location": location,
                            "limit": limit,
                        }),
                        run_id,
                    ),
                )

                conn.commit()

                return {
                    "status": "success",
                    "run_id": str(run_id),
                    "fetched": len(jobs),
                    "created": created,
                    "updated": updated,
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
