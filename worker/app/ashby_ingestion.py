import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import psycopg
from psycopg.types.json import Jsonb

from app.canonical import update_canonical_job
from app.ingestion_status import ALL_TENANTS_FAILED, direct_run_status

from app.adapters.ashby import AshbyAdapter
from app.ingestion import get_or_create_company


SOURCE_NAME = "ashby_direct"


UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}"
)


CZ_MARKERS = (
    "czech",
    "prague",
    "praha",
    "brno",
    "ostrava",
    "plzen",
    "pilsen",
    "olomouc",
    "liberec",
    "pardubice",
    "hradec kralove",
    "ceske budejovice",
    "usti nad labem",
    "zlin",
    "jihlava",
    "karlovy vary",
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


def extract_uuid(
    value: Any,
) -> str | None:

    match = UUID_RE.search(
        str(value or "")
    )

    if not match:
        return None

    return match.group(0).lower()


def extract_board(
    value: Any,
) -> str | None:

    try:
        path = urlparse(
            str(value or "")
        ).path.strip("/")

    except Exception:
        return None

    if not path:
        return None

    return path.split("/")[0]


def is_czech_country(
    value: Any,
) -> bool:

    return fold_text(value) in {
        "cz",
        "czechia",
        "czech republic",
    }


def has_czech_marker(
    value: Any,
) -> bool:

    text = fold_text(value)

    return any(
        marker in text
        for marker in CZ_MARKERS
    )


def is_czech_job(
    job: dict[str, Any],
) -> bool:

    address = (
        job.get("address")
        or {}
    )

    postal = (
        address.get("postalAddress")
        or {}
    )

    if is_czech_country(
        postal.get("addressCountry")
    ):
        return True

    if has_czech_marker(
        job.get("location")
    ):
        return True

    for secondary in (
        job.get("secondaryLocations")
        or []
    ):

        if not isinstance(
            secondary,
            dict,
        ):
            continue

        secondary_address = (
            secondary.get("address")
            or {}
        )

        if is_czech_country(
            secondary_address.get(
                "addressCountry"
            )
        ):
            return True

        if has_czech_marker(
            secondary.get("location")
        ):
            return True

    return False


def build_location_text(
    job: dict[str, Any],
) -> str:

    locations = []

    def add(
        value: str | None,
    ) -> None:

        if not value:
            return

        value = str(value).strip()

        if (
            value
            and value not in locations
        ):
            locations.append(value)

    address = (
        job.get("address")
        or {}
    )

    postal = (
        address.get("postalAddress")
        or {}
    )

    if is_czech_country(
        postal.get("addressCountry")
    ):
        values = [
            postal.get(
                "addressLocality"
            ),
            postal.get(
                "addressRegion"
            ),
            postal.get(
                "addressCountry"
            ),
        ]

        text = ", ".join(
            str(value).strip()
            for value in values
            if value
        )

        add(
            text
            or job.get("location")
        )

    elif has_czech_marker(
        job.get("location")
    ):
        add(
            job.get("location")
        )

    for secondary in (
        job.get("secondaryLocations")
        or []
    ):

        if not isinstance(
            secondary,
            dict,
        ):
            continue

        secondary_address = (
            secondary.get("address")
            or {}
        )

        if is_czech_country(
            secondary_address.get(
                "addressCountry"
            )
        ):
            values = [
                secondary_address.get(
                    "addressLocality"
                ),
                secondary_address.get(
                    "addressRegion"
                ),
                secondary_address.get(
                    "addressCountry"
                ),
            ]

            text = ", ".join(
                str(value).strip()
                for value in values
                if value
            )

            add(
                text
                or secondary.get(
                    "location"
                )
            )

        elif has_czech_marker(
            secondary.get("location")
        ):
            add(
                secondary.get("location")
            )

    if not locations:
        return "Czechia"

    return " | ".join(
        locations
    )


def normalize_remote_type(
    job: dict[str, Any],
) -> str:

    value = fold_text(
        job.get("workplaceType")
    )

    if value == "remote":
        return "remote"

    if value == "hybrid":
        return "hybrid"

    if value in {
        "onsite",
        "on site",
    }:
        return "onsite"

    if job.get("isRemote") is True:
        return "remote"

    return "unknown"


def salary_text(
    job: dict[str, Any],
) -> str | None:

    compensation = (
        job.get("compensation")
        or {}
    )

    value = (
        compensation.get(
            "compensationTierSummary"
        )
        or compensation.get(
            "scrapeableCompensationSalarySummary"
        )
    )

    if value:
        return str(value).strip()

    return None


def discover_boards(
    cur: psycopg.Cursor,
) -> list[tuple[str, str]]:

    cur.execute(
        """
        select
            js.raw_payload ->> 'organization'
                as company,
            js.source_url
        from public.job_sources js
        where js.source_name =
                'fantastic_jobs_apify'
          and js.raw_payload ->> 'source'
                = 'ashby'
        order by
            js.raw_payload ->> 'organization',
            js.source_url
        """
    )

    discovered = {}

    for company, source_url in (
        cur.fetchall()
    ):

        board = extract_board(
            source_url
        )

        if not board:
            continue

        key = board.casefold()

        if key not in discovered:
            discovered[key] = (
                company or board,
                board,
            )

    return sorted(
        discovered.values(),
        key=lambda item:
            item[1].casefold(),
    )


def find_existing_ashby_job(
    cur: psycopg.Cursor,
    job_uuid: str,
) -> Any | None:

    pattern = f"%{job_uuid}%"

    cur.execute(
        """
        select distinct
            js.job_id
        from public.job_sources js
        where js.source_name =
                'fantastic_jobs_apify'
          and js.raw_payload ->> 'source'
                = 'ashby'
          and js.source_url ilike %s
        """,
        (
            pattern,
        ),
    )

    matches = {
        row[0]
        for row in cur.fetchall()
    }

    if len(matches) != 1:
        return None

    return next(
        iter(matches)
    )


def ingest_ashby_jobs(
    board_name: str | None = None,
    max_boards: int | None = None,
) -> dict[str, Any]:

    database_url = os.environ[
        "DATABASE_URL"
    ]

    adapter = AshbyAdapter()

    with psycopg.connect(
        database_url
    ) as conn:

        with conn.cursor() as cur:

            discovered = (
                discover_boards(cur)
            )

            if board_name:

                boards = [
                    item
                    for item in discovered
                    if item[1].casefold()
                    == board_name.casefold()
                ]

                if not boards:
                    raise ValueError(
                        "Unknown Ashby board: "
                        f"{board_name}"
                    )

            else:
                boards = discovered

            if max_boards is not None:
                boards = boards[
                    :max_boards
                ]

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
                        "board_name":
                            board_name,
                        "board_count":
                            len(boards),
                    }),
                ),
            )

            run_id = (
                cur.fetchone()[0]
            )

            conn.commit()

            fetched = 0
            listed = 0
            czech_jobs = 0

            created = 0
            attached = 0
            updated = 0

            failed = 0
            successful_tenants = 0
            missing_uuid = 0

            board_errors = []

            try:

                for (
                    company_name,
                    board,
                ) in boards:

                    try:
                        jobs = (
                            adapter.list_jobs(
                                board
                            )
                        )

                    except Exception as exc:

                        board_errors.append({
                            "company":
                                company_name,
                            "board":
                                board,
                            "error":
                                str(exc),
                        })

                        continue

                    processed_before = created + attached + updated
                    fetched += len(
                        jobs
                    )

                    listed_jobs = [
                        job
                        for job in jobs
                        if job.get(
                            "isListed",
                            True,
                        )
                    ]

                    listed += len(
                        listed_jobs
                    )

                    selected = [
                        job
                        for job in listed_jobs
                        if is_czech_job(
                            job
                        )
                    ]

                    czech_jobs += len(
                        selected
                    )

                    for raw_job in selected:

                        job_uuid = (
                            extract_uuid(
                                raw_job.get(
                                    "jobUrl"
                                )
                            )
                        )

                        if not job_uuid:
                            missing_uuid += 1
                            failed += 1
                            continue

                        source_job_id = (
                            f"{board}:"
                            f"{job_uuid}"
                        )

                        title = str(
                            raw_job.get(
                                "title"
                            )
                            or
                            "Unknown position"
                        ).strip()

                        description = (
                            raw_job.get(
                                "descriptionPlain"
                            )
                        )

                        if description:
                            description = str(
                                description
                            ).strip()

                        location_text = (
                            build_location_text(
                                raw_job
                            )
                        )

                        remote_type = (
                            normalize_remote_type(
                                raw_job
                            )
                        )

                        source_url = (
                            raw_job.get(
                                "jobUrl"
                            )
                        )

                        apply_url = (
                            raw_job.get(
                                "applyUrl"
                            )
                        )

                        published_at = (
                            raw_job.get(
                                "publishedAt"
                            )
                        )

                        salary = (
                            salary_text(
                                raw_job
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

                            job_id = (
                                find_existing_ashby_job(
                                    cur,
                                    job_uuid,
                                )
                            )

                            if job_id:
                                attached_job = True

                            else:

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
                                        %s, %s, %s, %s,
                                        %s, 'active'
                                    )
                                    returning id
                                    """,
                                    (
                                        company_id,
                                        title,
                                        description,
                                        location_text,
                                        remote_type,
                                        salary,
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

                            update_canonical_job(
                                cur, job_id, SOURCE_NAME,
                                {
                                    "company_id": company_id,
                                    "title": title,
                                    "description": description,
                                    "location_text": location_text,
                                    "country_code": "CZ",
                                    "remote_type": remote_type,
                                    "salary_text": salary,
                                    "canonical_url": source_url,
                                    "published_at": published_at,
                                },
                                now,
                                preserve_if_none=('description', 'salary_text', 'canonical_url', 'published_at'),
                            )

                        raw_payload = dict(
                            raw_job
                        )

                        raw_payload[
                            "_board_name"
                        ] = board

                        raw_payload[
                            "_company_key"
                        ] = company_name

                        raw_payload[
                            "_job_uuid"
                        ] = job_uuid

                        raw_payload[
                            "_czech_location"
                        ] = location_text

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

                    if not selected or created + attached + updated > processed_before:
                        successful_tenants += 1
                    else:
                        board_errors.append(
                            {"board": board, "company": company_name, "error": "No Czech postings had usable identifiers"}
                        )

                run_status = direct_run_status(len(boards), successful_tenants)
                run_error = ALL_TENANTS_FAILED if run_status == "failed" else None

                cur.execute(
                    """
                    update public.ingestion_runs
                    set
                        finished_at = now(),
                        status = %s,
                        error_message = %s,
                        records_fetched = %s,
                        records_created = %s,
                        records_updated = %s,
                        records_failed = %s,
                        metadata = %s
                    where id = %s
                    """,
                    (
                        run_status,
                        run_error,
                        fetched,
                        created,
                        updated + attached,
                        failed,
                        Jsonb({
                            "board_name":
                                board_name,
                            "board_count":
                                len(boards),
                            "listed":
                                listed,
                            "czech_jobs":
                                czech_jobs,
                            "successful_tenants": successful_tenants,
                            "attached":
                                attached,
                            "missing_uuid":
                                missing_uuid,
                            "failed":
                                failed,
                            "board_errors":
                                board_errors,
                        }),
                        run_id,
                    ),
                )

                conn.commit()

                return {
                    "status":
                        run_status,

                    "run_id":
                        str(run_id),

                    "boards":
                        len(boards),

                    "fetched":
                        fetched,

                    "listed":
                        listed,

                    "czech_jobs":
                        czech_jobs,

                    "created":
                        created,

                    "attached":
                        attached,

                    "updated":
                        updated,

                    "missing_uuid":
                        missing_uuid,

                    "failed":
                        failed,

                    "board_errors":
                        board_errors,
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
