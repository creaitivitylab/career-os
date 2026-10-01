import os
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


SOURCE_PRIORITY = {
    "smartrecruiters_direct": 300,
    "fantastic_jobs_apify": 200,
    "jooble_direct": 100,
}


def source_priority(
    sources: list[str],
) -> int:
    return max(
        (
            SOURCE_PRIORITY.get(source, 0)
            for source in sources
        ),
        default=0,
    )


def merge_duplicate_candidate(
    candidate_id: str,
    dry_run: bool = True,
) -> dict[str, Any]:

    database_url = os.environ["DATABASE_URL"]

    with psycopg.connect(
        database_url,
        row_factory=dict_row,
    ) as conn:

        with conn.cursor() as cur:

            # Lock candidate.
            cur.execute(
                """
                select
                    id,
                    job_a_id,
                    job_b_id,
                    confidence,
                    reason
                from public.duplicate_candidates
                where id = %s
                for update
                """,
                (candidate_id,),
            )

            candidate = cur.fetchone()

            if not candidate:
                raise ValueError(
                    f"Duplicate candidate {candidate_id} not found"
                )

            job_a_id = candidate["job_a_id"]
            job_b_id = candidate["job_b_id"]

            # Lock both jobs.
            cur.execute(
                """
                select *
                from public.jobs
                where id in (%s, %s)
                for update
                """,
                (
                    job_a_id,
                    job_b_id,
                ),
            )

            jobs = {
                row["id"]: row
                for row in cur.fetchall()
            }

            if len(jobs) != 2:
                raise ValueError(
                    "One or both candidate jobs no longer exist"
                )

            # Find sources attached to each job.
            cur.execute(
                """
                select
                    job_id,
                    array_agg(source_name order by source_name)
                        as sources
                from public.job_sources
                where job_id in (%s, %s)
                group by job_id
                """,
                (
                    job_a_id,
                    job_b_id,
                ),
            )

            source_rows = cur.fetchall()

            sources: dict[Any, list[str]] = {
                row["job_id"]: row["sources"]
                for row in source_rows
            }

            sources_a = sources.get(job_a_id, [])
            sources_b = sources.get(job_b_id, [])

            # Prefer the highest-quality direct source.
            a_priority = source_priority(sources_a)
            b_priority = source_priority(sources_b)

            if a_priority > b_priority:
                keeper_id = job_a_id
                removed_id = job_b_id

            elif b_priority > a_priority:
                keeper_id = job_b_id
                removed_id = job_a_id

            else:
                raise ValueError(
                    "Cannot automatically choose canonical job: "
                    "source priority is ambiguous"
                )

            keeper = jobs[keeper_id]
            removed = jobs[removed_id]

            preview = {
                "candidate_id": candidate_id,
                "confidence": float(
                    candidate["confidence"]
                ),
                "dry_run": dry_run,

                "keeper": {
                    "id": str(keeper_id),
                    "title": keeper["title"],
                    "sources": sources.get(
                        keeper_id,
                        [],
                    ),
                },

                "removed": {
                    "id": str(removed_id),
                    "title": removed["title"],
                    "sources": sources.get(
                        removed_id,
                        [],
                    ),
                },
            }

            if dry_run:
                conn.rollback()
                return preview

            # Save full pre-merge snapshot.
            cur.execute(
                """
                insert into public.job_merge_history (
                    kept_job_id,
                    removed_job_id,
                    duplicate_candidate_id,
                    confidence,
                    reason,
                    snapshot
                )
                select
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    jsonb_build_object(
                        'keeper',
                        to_jsonb(k),
                        'removed',
                        to_jsonb(r)
                    )
                from public.jobs k
                cross join public.jobs r
                where k.id = %s
                  and r.id = %s
                """,
                (
                    keeper_id,
                    removed_id,
                    candidate_id,
                    candidate["confidence"],
                    Jsonb(
                        candidate["reason"] or {}
                    ),
                    keeper_id,
                    removed_id,
                ),
            )

            # Enrich canonical job.
            # Preferred source remains authoritative,
            # but missing fields can be supplied by
            # the secondary source.
            cur.execute(
                """
                update public.jobs k
                set
                    company_id =
                        coalesce(
                            k.company_id,
                            r.company_id
                        ),

                    title =
                        coalesce(
                            nullif(k.title, ''),
                            r.title
                        ),

                    description =
                        coalesce(
                            nullif(k.description, ''),
                            r.description
                        ),

                    location_text =
                        coalesce(
                            nullif(k.location_text, ''),
                            r.location_text
                        ),

                    city =
                        coalesce(
                            nullif(k.city, ''),
                            r.city
                        ),

                    country_code =
                        coalesce(
                            nullif(k.country_code, ''),
                            r.country_code
                        ),

                    remote_type =
                        case
                            when k.remote_type is null
                                 or k.remote_type = 'unknown'
                            then r.remote_type
                            else k.remote_type
                        end,

                    employment_type =
                        coalesce(
                            nullif(k.employment_type, ''),
                            r.employment_type
                        ),

                    seniority =
                        coalesce(
                            nullif(k.seniority, ''),
                            r.seniority
                        ),

                    salary_text =
                        coalesce(
                            nullif(k.salary_text, ''),
                            nullif(r.salary_text, '')
                        ),

                    salary_min =
                        coalesce(
                            k.salary_min,
                            r.salary_min
                        ),

                    salary_max =
                        coalesce(
                            k.salary_max,
                            r.salary_max
                        ),

                    salary_currency =
                        coalesce(
                            nullif(k.salary_currency, ''),
                            r.salary_currency
                        ),

                    salary_period =
                        coalesce(
                            nullif(k.salary_period, ''),
                            r.salary_period
                        ),

                    skills =
                        case
                            when k.skills is null
                                 or k.skills = '[]'::jsonb
                            then r.skills
                            else k.skills
                        end,

                    canonical_url =
                        coalesce(
                            nullif(k.canonical_url, ''),
                            r.canonical_url
                        ),

                    published_at =
                        coalesce(
                            k.published_at,
                            r.published_at
                        ),

                    expires_at =
                        coalesce(
                            k.expires_at,
                            r.expires_at
                        ),

                    first_seen_at =
                        least(
                            k.first_seen_at,
                            r.first_seen_at
                        ),

                    last_seen_at =
                        greatest(
                            k.last_seen_at,
                            r.last_seen_at
                        ),

                    last_verified_at =
                        greatest(
                            k.last_verified_at,
                            r.last_verified_at
                        ),

                    status =
                        case
                            when k.status = 'active'
                                 or r.status = 'active'
                            then 'active'
                            else k.status
                        end,

                    updated_at = now()

                from public.jobs r

                where k.id = %s
                  and r.id = %s
                """,
                (
                    keeper_id,
                    removed_id,
                ),
            )

            # Move all source references to keeper.
            cur.execute(
                """
                update public.job_sources
                set
                    job_id = %s,
                    updated_at = now()
                where job_id = %s
                """,
                (
                    keeper_id,
                    removed_id,
                ),
            )

            # Removing the duplicate job also removes
            # obsolete duplicate_candidates via FK cascade.
            cur.execute(
                """
                delete from public.jobs
                where id = %s
                """,
                (removed_id,),
            )

            conn.commit()

            preview["merged"] = True

            return preview
