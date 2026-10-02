import html
import os
import re
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.canonical import update_canonical_job

from app.adapters.jooble import JoobleDirectAdapter


SOURCE_NAME = "jooble_direct"


def clean_html(value: str | None) -> str | None:
    if not value:
        return None

    text = re.sub(r"<[^>]+>", "", value)
    return html.unescape(text).strip()


def normalize_company_name(value: str) -> str:
    return " ".join(value.lower().split())


def get_or_create_company(
    cur: psycopg.Cursor,
    company_name: str | None,
):
    if not company_name:
        return None

    normalized = normalize_company_name(company_name)

    cur.execute(
        """
        select id
        from public.companies
        where normalized_name = %s
        limit 1
        """,
        (normalized,),
    )

    existing = cur.fetchone()

    if existing:
        return existing[0]

    cur.execute(
        """
        insert into public.companies (
            name,
            normalized_name
        )
        values (%s, %s)
        returning id
        """,
        (
            company_name.strip(),
            normalized,
        ),
    )

    return cur.fetchone()[0]


def ingest_jooble_search(
    keywords: str,
    location: str = "Czech Republic",
    page: int = 1,
    results_per_page: int = 100,
) -> dict[str, Any]:

    database_url = os.environ["DATABASE_URL"]

    adapter = JoobleDirectAdapter()

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
                        "keywords": keywords,
                        "location": location,
                        "page": page,
			"results_per_page": results_per_page,
                    }),
                ),
            )

            run_id = cur.fetchone()[0]
            conn.commit()

            created = 0
            updated = 0
            failed = 0

            try:
                result = adapter.search(
                    keywords=keywords,
                    location=location,
                    page=page,
		    results_per_page=results_per_page,
                )

                payload = result["data"]
                jobs = payload.get("jobs", [])

                for raw_job in jobs:
                    try:
                        source_job_id = str(raw_job["id"])

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

                        company_id = get_or_create_company(
                            cur,
                            raw_job.get("company"),
                        )

                        title = (
                            raw_job.get("title")
                            or "Unknown position"
                        ).strip()

                        description = clean_html(
                            raw_job.get("snippet")
                        )

                        source_url = raw_job.get("link")
                        salary_text = raw_job.get("salary")
                        location_text = raw_job.get("location")
                        employment_type = raw_job.get("type")

                        now = datetime.now(timezone.utc)

                        if existing_source:
                            source_id, job_id = existing_source

                            update_canonical_job(
                                cur, job_id, SOURCE_NAME,
                                {
                                    "company_id": company_id,
                                    "title": title,
                                    "description": description,
                                    "location_text": location_text,
                                    "employment_type": employment_type,
                                    "salary_text": salary_text,
                                    "canonical_url": source_url,
                                },
                                now,
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
                                    employment_type,
                                    salary_text,
                                    canonical_url,
                                    first_seen_at,
                                    last_seen_at,
                                    last_verified_at,
                                    status
                                )
                                values (
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    'active'
                                )
                                returning id
                                """,
                                (
                                    company_id,
                                    title,
                                    description,
                                    location_text,
                                    employment_type,
                                    salary_text,
                                    source_url,
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
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    true
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

                    except Exception:
                        failed += 1

                metadata = {
                    "keywords": keywords,
                    "location": location,
                    "page": page,
                    "total_count": payload.get("totalCount"),
                    "rate_limit": result.get("rate_limit"),
		    "results_per_page": results_per_page,
                }

                cur.execute(
                    """
                    update public.ingestion_runs
                    set
                        finished_at = now(),
                        status = 'success',
                        records_fetched = %s,
                        records_created = %s,
                        records_updated = %s,
                        records_failed = %s,
                        metadata = %s
                    where id = %s
                    """,
                    (
                        len(jobs),
                        created,
                        updated,
                        failed,
                        Jsonb(metadata),
                        run_id,
                    ),
                )

                conn.commit()

                return {
                    "status": "success",
                    "run_id": str(run_id),
                    "total_count": payload.get("totalCount"),
                    "fetched": len(jobs),
                    "created": created,
                    "updated": updated,
                    "failed": failed,
                    "rate_limit": result.get("rate_limit"),
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
