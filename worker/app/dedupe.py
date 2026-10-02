import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.source_priority import SOURCE_PRIORITY


# Keep the broad existing title gate: normalization absorbs punctuation/case,
# while trigram overlap allows suffixes and modest wording differences.
TITLE_BLOCK_MIN_SIMILARITY = 0.45
# A loose overlap guard for repeated role titles advertised in distinct towns.
# Unknown locations and explicitly remote jobs remain eligible.
LOCATION_BLOCK_MIN_SIMILARITY = 0.20

# Materialization is intentional: do not inline description work into the
# company join or evaluate expensive scores again in filters/projections.
# Source sets and normalization are computed once per canonical job.
DEDUPE_BLOCKING_SQL = """
    with source_sets as materialized (
        select job_id,
               array_agg(distinct source_name order by source_name) as source_names
        from public.job_sources
        group by job_id
        having bool_or(source_name in ({source_placeholders}))
    ),
    eligible_jobs as materialized (
        select j.id, j.title, j.normalized_title, j.description,
               j.normalized_location, j.location_text, j.remote_type,
               c.name as company, c.normalized_name, sources.source_names,
               public.normalize_job_text(coalesce(j.description, '')) as normalized_description
        from public.jobs j
        join public.companies c on c.id = j.company_id
        join source_sets sources on sources.job_id = j.id
        where nullif(btrim(c.normalized_name), '') is not null
    ),
    title_pairs as materialized (
        select a.id as job_a_id, b.id as job_b_id,
               similarity(coalesce(a.normalized_title, ''),
                          coalesce(b.normalized_title, '')) as title_similarity
        from eligible_jobs a
        join eligible_jobs b
          on a.normalized_name = b.normalized_name and a.id < b.id
    ),
    title_blocked_pairs as materialized (
        select pairs.*
        from title_pairs pairs
        where pairs.title_similarity >= {title_threshold}
          and not exists (
              select 1 from public.duplicate_candidates previous
              where previous.status <> 'pending'
                and ((previous.job_a_id = pairs.job_a_id and previous.job_b_id = pairs.job_b_id)
                  or (previous.job_a_id = pairs.job_b_id and previous.job_b_id = pairs.job_a_id))
          )
    ),
    located_pairs as materialized (
        select pairs.*,
               case
                   when coalesce(j.normalized_location, '') = ''
                     or coalesce(d.normalized_location, '') = '' then 0.50
                   when j.normalized_location = d.normalized_location then 1.00
                   when d.normalized_location like '%%' || j.normalized_location || '%%'
                     or j.normalized_location like '%%' || d.normalized_location || '%%' then 0.95
                   else greatest(similarity(j.normalized_location, d.normalized_location), 0)
               end as location_similarity,
               coalesce(j.remote_type = 'remote' or d.remote_type = 'remote', false) as remote_pair
        from title_blocked_pairs pairs
        join eligible_jobs j on j.id = pairs.job_a_id
        join eligible_jobs d on d.id = pairs.job_b_id
    ),
    blocked_pairs as materialized (
        select job_a_id, job_b_id, title_similarity, location_similarity
        from located_pairs
        where location_similarity >= {location_threshold} or remote_pair
    )
""".format(
    source_placeholders=", ".join("%s" for _ in SOURCE_PRIORITY),
    title_threshold=TITLE_BLOCK_MIN_SIMILARITY,
    location_threshold=LOCATION_BLOCK_MIN_SIMILARITY,
)


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
                DEDUPE_BLOCKING_SQL + """
                , description_pairs as materialized (
                    -- Repeated source boilerplate can occur on many jobs.
                    -- Scores depend only on the two normalized texts. Keep
                    -- UUID orientation: word_similarity is directional when
                    -- both texts have equal lengths.
                    select distinct
                        j.normalized_description as description_a,
                        d.normalized_description as description_b
                    from blocked_pairs pairs
                    join eligible_jobs j on j.id = pairs.job_a_id
                    join eligible_jobs d on d.id = pairs.job_b_id
                ),
                description_scores as materialized (
                    select p.*,
                        case
                            when p.description_a = '' or p.description_b = '' then 0.0
                            when p.description_a = p.description_b then 1.0
                            else greatest(
                                similarity(p.description_a, p.description_b),
                                word_similarity(
                                    case when length(p.description_a) <= length(p.description_b)
                                         then p.description_a else p.description_b end,
                                    case when length(p.description_a) <= length(p.description_b)
                                         then p.description_b else p.description_a end
                                )
                            )
                        end as description_similarity,
                        (
                            (length(p.description_a) >= 120
                             and position(p.description_a in p.description_b) > 0)
                            or
                            (length(p.description_b) >= 120
                             and position(p.description_b in p.description_a) > 0)
                        ) as description_contained
                    from description_pairs p
                ),
                scored as materialized (
                    select
                        j.id as job_a_id,
                        d.id as job_b_id,

                        j.company,

                        j.title as job_a_title,
                        d.title as job_b_title,

                        j.location_text as job_a_location,
                        d.location_text as job_b_location,

                        j.source_names as source_names_a,
                        d.source_names as source_names_b,

                        pairs.title_similarity,

                        pairs.location_similarity,

                        descriptions.description_similarity,
                        descriptions.description_contained

                    from blocked_pairs pairs
                    join eligible_jobs j on j.id = pairs.job_a_id
                    join eligible_jobs d on d.id = pairs.job_b_id
                    join description_scores descriptions
                      on descriptions.description_a = j.normalized_description
                     and descriptions.description_b = d.normalized_description
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

                        'job_a_title',
                            job_a_title,

                        'job_b_title',
                            job_b_title,

                        'job_a_location',
                            job_a_location,

                        'job_b_location',
                            job_b_location,

                        'job_a_sources', to_jsonb(source_names_a),
                        'job_b_sources', to_jsonb(source_names_b),

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
                """,
                tuple(SOURCE_PRIORITY),
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

                job_counts as (
                    select job_id, count(*) as matches
                    from (
                        select job_a_id as job_id from strong
                        union all
                        select job_b_id as job_id from strong
                    ) endpoints
                    group by job_id
                )

                select
                    s.id,
                    s.job_a_id,
                    s.job_b_id,
                    s.confidence,
                    s.reason

                from strong s

                join job_counts a
                    on a.job_id = s.job_a_id

                join job_counts b
                    on b.job_id = s.job_b_id

                where
                    a.matches = 1
                    and b.matches = 1

                order by s.confidence desc
                """
            )

            return cur.fetchall()
