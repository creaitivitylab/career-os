import html
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.adapters.workable import WorkableAdapter
from app.ingestion import get_or_create_company


SOURCE_NAME = "workable_direct"


WORKABLE_TENANTS = {
    "Mercier Consultancy Group":
        "mercier-consultancy-group",

    "Mindrift":
        "toloka-ai",

    "Stranger Soccer":
        "stranger-soccer-1",

    "SupportYourApp":
        "supportyourapp",

    "Dentons":
        "dentons-europe",

    "D-ploy":
        "d-ploy",

    "Gramian Consulting Group":
        "gramian",

    "Hedepy":
        "hedepy",

    "Innovatrics":
        "innovatrics",

    "Sweat Pants Agency":
        "sweat-pants-agency",

    "TheSoul Group":
        "thesoul-publishing-1",

    "TMGM":
        "tmgm",

    "Allucent":
        "allucent",

    "Biomapas":
        "biomapas",

    "Creditstar":
        "creditstar",

    "ELVTR":
        "elvtrcom",

    "EUROPEAN DYNAMICS":
        "european-dynamics",

    "FE fundinfo":
        "fe-fundinfo",

    "CallMiner":
        "callminer",

    "Fluentbe.com":
        "fluentbe",

    "Pixaera":
        "pixaera",

    "Snuggs":
        "snuggs",
}


def clean_html(
    value: str | None,
) -> str | None:

    if not value:
        return None

    text = re.sub(
        r"<[^>]+>",
        " ",
        value,
    )

    text = html.unescape(text)

    return " ".join(
        text.split()
    )


def fold_text(
    value: Any,
) -> str:

    if value is None:
        return ""

    value = unicodedata.normalize(
        "NFKD",
        str(value),
    )

    value = value.encode(
        "ascii",
        "ignore",
    ).decode("ascii")

    return value.strip().lower()


def exact_title_key(
    value: Any,
) -> str:

    if value is None:
        return ""

    value = unicodedata.normalize(
        "NFKC",
        str(value),
    )

    value = " ".join(
        value.split()
    )

    return value.casefold()


def is_czech_country(
    value: Any,
) -> bool:

    value = fold_text(value)

    return value in {
        "cz",
        "czechia",
        "czech republic",
    }


def is_czech_variant(
    job: dict[str, Any],
) -> bool:

    if (
        is_czech_country(
            job.get("country")
        )
        or is_czech_country(
            job.get("country_name")
        )
        or is_czech_country(
            job.get("country_code")
        )
    ):
        return True

    locations = (
        job.get("locations")
        or []
    )

    for location in locations:

        if not isinstance(
            location,
            dict,
        ):
            continue

        if (
            is_czech_country(
                location.get(
                    "country"
                )
            )
            or is_czech_country(
                location.get(
                    "country_name"
                )
            )
            or is_czech_country(
                location.get(
                    "country_code"
                )
            )
        ):
            return True

    return False


def group_jobs(
    jobs: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:

    grouped = defaultdict(list)

    for job in jobs:

        shortcode = job.get(
            "shortcode"
        )

        if not shortcode:
            continue

        grouped[
            str(shortcode)
        ].append(job)

    return dict(grouped)


def czech_groups(
    jobs: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:

    grouped = group_jobs(jobs)

    return {
        shortcode: variants
        for shortcode, variants
        in grouped.items()
        if any(
            is_czech_variant(
                variant
            )
            for variant in variants
        )
    }


def choose_primary_variant(
    variants: list[dict[str, Any]],
) -> dict[str, Any]:

    for variant in variants:
        if is_czech_variant(
            variant
        ):
            return variant

    return variants[0]


def build_location_text(
    variants: list[dict[str, Any]],
) -> str | None:

    locations = []

    def add_location(
        city: Any,
        state: Any,
        country: Any,
    ) -> None:

        if not is_czech_country(
            country
        ):
            return

        values = []

        for value in (
            city,
            state,
            country,
        ):
            if not value:
                continue

            value = str(
                value
            ).strip()

            if (
                value
                and value
                not in values
            ):
                values.append(
                    value
                )

        if not values:
            return

        text = ", ".join(
            values
        )

        if text not in locations:
            locations.append(
                text
            )

    for variant in variants:

        add_location(
            variant.get("city"),
            variant.get("state"),
            (
                variant.get("country")
                or variant.get(
                    "country_name"
                )
                or variant.get(
                    "country_code"
                )
            ),
        )

        for location in (
            variant.get("locations")
            or []
        ):

            if not isinstance(
                location,
                dict,
            ):
                continue

            add_location(
                location.get("city"),
                (
                    location.get("state")
                    or location.get(
                        "subregion"
                    )
                    or location.get(
                        "region"
                    )
                ),
                (
                    location.get(
                        "country"
                    )
                    or location.get(
                        "country_name"
                    )
                    or location.get(
                        "country_code"
                    )
                ),
            )

    if not locations:
        return "Czechia"

    return " | ".join(
        locations
    )


def remote_type(
    variants: list[dict[str, Any]],
) -> str:

    values = []

    for variant in variants:

        value = (
            variant.get(
                "workplace_type"
            )
            or variant.get(
                "workplace"
            )
        )

        if value:
            values.append(
                fold_text(value)
            )

    combined = " ".join(
        values
    )

    if "hybrid" in combined:
        return "hybrid"

    if "remote" in combined:
        return "remote"

    if (
        "on site" in combined
        or "onsite" in combined
        or "office" in combined
    ):
        return "onsite"

    return "unknown"


def find_unique_fantastic_match(
    cur: psycopg.Cursor,
    company_name: str,
    title: str,
) -> Any | None:

    cur.execute(
        """
        select distinct
            js.job_id,
            js.raw_payload ->> 'title'
                as title
        from public.job_sources js
        where js.source_name =
                'fantastic_jobs_apify'
          and js.raw_payload ->> 'source'
                = 'workable'
          and lower(
                trim(
                    js.raw_payload
                    ->> 'organization'
                )
              )
                = lower(trim(%s))
        """,
        (
            company_name,
        ),
    )

    wanted = exact_title_key(
        title
    )

    matches = {
        row[0]
        for row in cur.fetchall()
        if exact_title_key(
            row[1]
        ) == wanted
    }

    if len(matches) != 1:
        return None

    return next(
        iter(matches)
    )


def job_already_has_workable(
    cur: psycopg.Cursor,
    job_id: Any,
) -> bool:

    cur.execute(
        """
        select exists (
            select 1
            from public.job_sources
            where job_id = %s
              and source_name = %s
        )
        """,
        (
            job_id,
            SOURCE_NAME,
        ),
    )

    return bool(
        cur.fetchone()[0]
    )


def ingest_workable_jobs(
    tenant_slug: str | None = None,
    max_companies: int | None = None,
) -> dict[str, Any]:

    database_url = os.environ[
        "DATABASE_URL"
    ]

    adapter = WorkableAdapter()

    tenants = list(
        WORKABLE_TENANTS.items()
    )

    if tenant_slug:

        tenants = [
            (
                company,
                slug,
            )
            for company, slug
            in tenants
            if slug == tenant_slug
        ]

        if not tenants:
            raise ValueError(
                "Unknown Workable tenant: "
                f"{tenant_slug}"
            )

    if max_companies is not None:
        tenants = tenants[
            :max_companies
        ]

    with psycopg.connect(
        database_url
    ) as conn:

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
                        "tenant_slug":
                            tenant_slug,
                        "tenant_count":
                            len(tenants),
                    }),
                ),
            )

            run_id = (
                cur.fetchone()[0]
            )

            conn.commit()

            raw_rows = 0
            unique_jobs = 0
            czech_jobs = 0

            created = 0
            attached = 0
            updated = 0
            failed = 0

            ambiguous_exact_matches = 0
            tenant_errors = []

            try:

                for (
                    company_name,
                    slug,
                ) in tenants:

                    try:
                        account = (
                            adapter.get_account(
                                slug
                            )
                        )

                    except Exception as exc:

                        tenant_errors.append({
                            "company":
                                company_name,
                            "tenant_slug":
                                slug,
                            "error":
                                str(exc),
                        })

                        continue

                    jobs = (
                        account.get("jobs")
                        or []
                    )

                    raw_rows += len(
                        jobs
                    )

                    all_groups = (
                        group_jobs(jobs)
                    )

                    unique_jobs += len(
                        all_groups
                    )

                    groups = (
                        czech_groups(jobs)
                    )

                    czech_jobs += len(
                        groups
                    )

                    title_counts = Counter()

                    for variants in (
                        groups.values()
                    ):

                        primary = (
                            choose_primary_variant(
                                variants
                            )
                        )

                        title_counts[
                            exact_title_key(
                                primary.get(
                                    "title"
                                )
                            )
                        ] += 1

                    for (
                        shortcode,
                        variants,
                    ) in groups.items():

                        primary = (
                            choose_primary_variant(
                                variants
                            )
                        )

                        title = (
                            primary.get(
                                "title"
                            )
                            or
                            "Unknown position"
                        )

                        title = str(
                            title
                        ).strip()

                        if not title:
                            title = (
                                "Unknown position"
                            )

                        source_job_id = (
                            f"{slug}:"
                            f"{shortcode}"
                        )

                        source_url = (
                            primary.get(
                                "shortlink"
                            )
                            or primary.get(
                                "url"
                            )
                            or (
                                "https://"
                                "apply.workable.com/"
                                f"j/{shortcode}"
                            )
                        )

                        apply_url = (
                            primary.get(
                                "application_url"
                            )
                            or (
                                str(
                                    source_url
                                ).rstrip("/")
                                + "/apply"
                            )
                        )

                        description = (
                            clean_html(
                                primary.get(
                                    "description"
                                )
                                or primary.get(
                                    "description_text"
                                )
                            )
                        )

                        location_text = (
                            build_location_text(
                                variants
                            )
                        )

                        work_mode = (
                            remote_type(
                                variants
                            )
                        )

                        published_at = (
                            primary.get(
                                "published_on"
                            )
                            or primary.get(
                                "published_at"
                            )
                            or primary.get(
                                "created_at"
                            )
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
                            job_id = None

                            title_key = (
                                exact_title_key(
                                    title
                                )
                            )

                            if (
                                title_key
                                and
                                title_counts[
                                    title_key
                                ] == 1
                            ):

                                candidate = (
                                    find_unique_fantastic_match(
                                        cur,
                                        company_name,
                                        title,
                                    )
                                )

                                if candidate:

                                    if not (
                                        job_already_has_workable(
                                            cur,
                                            candidate,
                                        )
                                    ):
                                        job_id = (
                                            candidate
                                        )

                                        attached_job = (
                                            True
                                        )

                                    else:
                                        ambiguous_exact_matches += 1

                            if job_id is None:

                                company_id = (
                                    get_or_create_company(
                                        cur,
                                        company_name,
                                    )
                                )

                                cur.execute(
                                    """
                                    insert into public.jobs (
                                        company_id,
                                        title,
                                        description,
                                        location_text,
                                        country_code,
                                        remote_type,
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
                                        %s, %s, %s, %s,
                                        'active'
                                    )
                                    returning id
                                    """,
                                    (
                                        company_id,
                                        title,
                                        description,
                                        location_text,
                                        work_mode,
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

                        company_id = (
                            get_or_create_company(
                                cur,
                                company_name,
                            )
                        )

                        if not new_job:

                            cur.execute(
                                """
                                update public.jobs
                                set
                                    company_id = %s,
                                    title = %s,

                                    description =
                                        coalesce(
                                            %s,
                                            description
                                        ),

                                    location_text = %s,
                                    country_code = 'CZ',
                                    remote_type = %s,

                                    canonical_url =
                                        coalesce(
                                            %s,
                                            canonical_url
                                        ),

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
                                    work_mode,
                                    source_url,
                                    published_at,
                                    now,
                                    now,
                                    now,
                                    job_id,
                                ),
                            )

                        raw_payload = dict(
                            primary
                        )

                        raw_payload[
                            "_tenant_slug"
                        ] = slug

                        raw_payload[
                            "_account_name"
                        ] = account.get(
                            "name"
                        )

                        raw_payload[
                            "_company_key"
                        ] = company_name

                        raw_payload[
                            "_variant_count"
                        ] = len(
                            variants
                        )

                        raw_payload[
                            "_czech_location"
                        ] = location_text

                        raw_payload[
                            "_variants"
                        ] = variants

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
                                    Jsonb(
                                        raw_payload
                                    ),
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
                                    Jsonb(
                                        raw_payload
                                    ),
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
                        raw_rows,
                        created,
                        updated + attached,
                        Jsonb({
                            "tenant_slug":
                                tenant_slug,
                            "tenant_count":
                                len(tenants),
                            "unique_jobs":
                                unique_jobs,
                            "czech_jobs":
                                czech_jobs,
                            "attached":
                                attached,
                            "ambiguous_exact_matches":
                                ambiguous_exact_matches,
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
                    "status":
                        "success",

                    "run_id":
                        str(run_id),

                    "tenants":
                        len(tenants),

                    "raw_rows":
                        raw_rows,

                    "unique_jobs":
                        unique_jobs,

                    "czech_jobs":
                        czech_jobs,

                    "created":
                        created,

                    "attached":
                        attached,

                    "updated":
                        updated,

                    "ambiguous_exact_matches":
                        ambiguous_exact_matches,

                    "failed":
                        failed,

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
