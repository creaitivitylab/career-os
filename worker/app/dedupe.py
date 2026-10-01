import os
from typing import Any

import psycopg
from psycopg.rows import dict_row


def rebuild_duplicate_candidates() -> dict[str, Any]:
    database_url = os.environ["DATABASE_URL"]

    with psycopg.connect(
        database_url,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                delete from public.duplicate_candidates
                where status = 'pending'
                """
            )

            cur.execute(
                """
                with jooble as (
                    select
                        j.id,
                        j.title,
                        j.normalized_title,
                        j.description,
                        j.normalized_location,
                        j.location_text,
                        c.name as company,
                        c.normalized_name
                    from public.jobs j
                    join public.companies c
                        on c.id = j.company_id
                    where exists (
                        select 1
                        from public.job_sources js
                        where js.job_id = j.id
                          and js.source_name = 'jooble_direct'
                    )
                    and not exists (
                        select 1
                        from public.job_sources other
                        where other.job_id = j.id
                          and other.source_name in (
                              'fantastic_jobs_apify',
                              'smartrecruiters_direct'
                          )
                    )
                ),

                direct_jobs as (
                    select
                        j.id,
                        j.title,
                        j.normalized_title,
                        j.description,
                        j.normalized_location,
                        j.location_text,
                        c.name as company,
                        c.normalized_name,

                        array(
                            select distinct js.source_name
                            from public.job_sources js
                            where js.job_id = j.id
                              and js.source_name in (
                                  'fantastic_jobs_apify',
                                  'smartrecruiters_direct'
                              )
                            order by js.source_name
                        ) as source_names

                    from public.jobs j
                    join public.companies c
                        on c.id = j.company_id

                    where exists (
                        select 1
                        from public.job_sources js
                        where js.job_id = j.id
                          and js.source_name in (
                              'fantastic_jobs_apify',
                              'smartrecruiters_direct'
                          )
                    )

                    and not exists (
                        select 1
                        from public.job_sources other
                        where other.job_id = j.id
                          and other.source_name = 'jooble_direct'
                    )
                ),

                scored as (
                    select
                        j.id as job_a_id,
                        d.id as job_b_id,

                        j.company,

                        j.title as jooble_title,
                        d.title as direct_title,

                        j.location_text as jooble_location,
                        d.location_text as direct_location,

                        d.source_names,

                        similarity(
                            coalesce(
                                j.normalized_title,
                                ''
                            ),
                            coalesce(
                                d.normalized_title,
                                ''
                            )
                        ) as title_similarity,

                        case
                            when
                                coalesce(
                                    j.normalized_location,
                                    ''
                                ) = ''
                                or
                                coalesce(
                                    d.normalized_location,
                                    ''
                                ) = ''
                            then 0.50

                            when
                                j.normalized_location =
                                d.normalized_location
                            then 1.00

                            when
                                d.normalized_location like
                                    '%' ||
                                    j.normalized_location ||
                                    '%'
                                or
                                j.normalized_location like
                                    '%' ||
                                    d.normalized_location ||
                                    '%'
                            then 0.95

                            else greatest(
                                similarity(
                                    j.normalized_location,
                                    d.normalized_location
                                ),
                                0
                            )
                        end as location_similarity,

                        greatest(
                            similarity(
                                public.normalize_job_text(
                                    coalesce(
                                        j.description,
                                        ''
                                    )
                                ),
                                public.normalize_job_text(
                                    coalesce(
                                        d.description,
                                        ''
                                    )
                                )
                            ),

                            word_similarity(
                                public.normalize_job_text(
                                    coalesce(
                                        j.description,
                                        ''
                                    )
                                ),
                                public.normalize_job_text(
                                    coalesce(
                                        d.description,
                                        ''
                                    )
                                )
                            )
                        ) as description_similarity,

                        case
                            when
                                length(
                                    public.normalize_job_text(
                                        coalesce(
                                            j.description,
                                            ''
                                        )
                                    )
                                ) >= 120

                                and position(
                                    public.normalize_job_text(
                                        coalesce(
                                            j.description,
                                            ''
                                        )
                                    )
                                    in
                                    public.normalize_job_text(
                                        coalesce(
                                            d.description,
                                            ''
                                        )
                                    )
                                ) > 0

                            then true
                            else false
                        end as description_contained

                    from jooble j

                    join direct_jobs d
                        on d.normalized_name =
                           j.normalized_name
                       and d.id <> j.id
                ),

                final_scores as (
                    select
                        *,

                        (
                            title_similarity * 0.62
                            +
                            location_similarity * 0.18
                            +
                            description_similarity * 0.20
                        ) as confidence

                    from scored
                )

                insert into public.duplicate_candidates (
                    job_a_id,
                    job_b_id,
                    company_similarity,
                    title_similarity,
                    location_similarity,
                    description_similarity,
                    confidence,
                    reason
                )

                select
                    job_a_id,
                    job_b_id,

                    1.00,
                    title_similarity,
                    location_similarity,
                    description_similarity,
                    confidence,

                    jsonb_build_object(
                        'company',
                            company,

                        'jooble_title',
                            jooble_title,

                        'direct_title',
                            direct_title,

                        'jooble_location',
                            jooble_location,

                        'direct_location',
                            direct_location,

                        'direct_sources',
                            to_jsonb(source_names),

                        'description_contained',
                            description_contained
                    )

                from final_scores

                where
                    (
                        title_similarity >= 0.45
                        or
                        description_similarity >= 0.55
                    )

                    and confidence >= 0.50

                on conflict (job_a_id, job_b_id)
                do update set
                    company_similarity =
                        excluded.company_similarity,

                    title_similarity =
                        excluded.title_similarity,

                    location_similarity =
                        excluded.location_similarity,

                    description_similarity =
                        excluded.description_similarity,

                    confidence =
                        excluded.confidence,

                    reason =
                        excluded.reason

                returning id
                """
            )

            rows = cur.fetchall()

            conn.commit()

            return {
                "status": "success",
                "candidates_upserted": len(rows),
            }


def get_safe_auto_merge_candidates() -> list[dict[str, Any]]:
    database_url = os.environ["DATABASE_URL"]

    with psycopg.connect(
        database_url,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                with strong as (
                    select *
                    from public.duplicate_candidates
                    where status = 'pending'

                      and title_similarity >= 0.98

                      and location_similarity >= 0.90

                      and confidence >= 0.95

                      and coalesce(
                            (reason ->> 'description_contained')::boolean,
                            false
                          ) = true
                ),

                a_counts as (
                    select
                        job_a_id,
                        count(*) as matches
                    from strong
                    group by job_a_id
                ),

                b_counts as (
                    select
                        job_b_id,
                        count(*) as matches
                    from strong
                    group by job_b_id
                )

                select
                    s.id,
                    s.job_a_id,
                    s.job_b_id,
                    s.confidence,
                    s.reason

                from strong s

                join a_counts a
                    on a.job_a_id = s.job_a_id

                join b_counts b
                    on b.job_b_id = s.job_b_id

                where
                    a.matches = 1
                    and b.matches = 1

                order by s.confidence desc
                """
            )

            return cur.fetchall()
