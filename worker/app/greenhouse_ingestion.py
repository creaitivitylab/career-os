import html
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import psycopg
from psycopg.types.json import Jsonb

from app.canonical import update_canonical_job
from app.ingestion_status import ALL_TENANTS_FAILED, direct_run_status

from app.adapters.greenhouse import GreenhouseAdapter
from app.ingestion import get_or_create_company


SOURCE_NAME = "greenhouse_direct"


def clean_html(value: str | None) -> str | None:
    if not value:
        return None

    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)

    return " ".join(text.split())


def fold_text(value: str | None) -> str:
    if not value:
        return ""

    normalized = unicodedata.normalize(
        "NFKD",
        value,
    )

    ascii_text = normalized.encode(
        "ascii",
        "ignore",
    ).decode("ascii")

    return ascii_text.lower()


def is_czech_job(
    raw_job: dict[str, Any],
) -> bool:

    location = raw_job.get("location") or {}

    location_name = location.get("name") or ""

    text = fold_text(
        str(location_name)
    )

    markers = (
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

    return any(
        marker in text
        for marker in markers
    )


def build_location_text(
    raw_job: dict[str, Any],
) -> str | None:

    location = raw_job.get("location") or {}

    value = location.get("name")

    if not value:
        return None

    return str(value).strip()


def normalize_remote_type(
    raw_job: dict[str, Any],
) -> str:

    location_text = fold_text(
        build_location_text(raw_job)
    )

    if "hybrid" in location_text:
        return "hybrid"

    if "remote" in location_text:
        return "remote"

    return "unknown"


def discover_boards(
    cur: psycopg.Cursor,
) -> list[str]:

    cur.execute(
        """
        select distinct
            js.raw_payload ->> 'source_slug'
        from public.job_sources js
        where js.source_name =
                'fantastic_jobs_apify'
          and js.raw_payload ->> 'source'
                = 'greenhouse'
          and nullif(
                js.raw_payload ->> 'source_slug',
                ''
              ) is not null
        order by 1
        """
    )

    return [
        row[0]
        for row in cur.fetchall()
        if row[0]
    ]


def greenhouse_url_matches(
    value: str | None,
    board_token: str,
    posting_id: str,
    board_is_known: bool = False,
) -> bool:
    """Require an exact posting token and board, not an ID substring."""
    if not value:
        return False
    try:
        url = urlsplit(value.strip())
        host = (url.hostname or "").casefold()
        parts = unquote(url.path).strip("/").split("/")
        query = parse_qs(url.query)
    except ValueError:
        return False

    if url.scheme not in {"https", "http"}:
        return False

    native_host = host in {
        "boards.greenhouse.io", "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io",
    }
    if native_host:
        if len(parts) == 3 and parts[1] == "jobs":
            return parts[0].casefold() == board_token.casefold() and parts[2] == posting_id
        if parts == ["embed", "job_app"]:
            return (
                {value.casefold() for value in query.get("for", [])}
                == {board_token.casefold()}
                and set(query.get("token", [])) == {posting_id}
            )
        return False

    # Employer-hosted gh_jid URLs require independent board evidence.
    return board_is_known and set(query.get("gh_jid", [])) == {posting_id}


def find_existing_greenhouse_job(
    cur: psycopg.Cursor,
    board_token: str,
    posting_id: str,
) -> Any | None:
    if not posting_id.isdigit():
        return None

    # Substrings only prefilter rows. Parsed URL/native ID checks below decide
    # identity, and all evidence is combined before checking ambiguity.
    cur.execute(
        """
        select js.job_id, js.source_name, js.source_url, js.raw_payload
        from public.job_sources js
        where (
            js.source_name = 'fantastic_jobs_apify'
            and js.raw_payload ->> 'source' = 'greenhouse'
            and js.raw_payload ->> 'source_slug' = %s
            and (
                js.raw_payload ->> 'id' = %s
                or js.raw_payload ->> 'job_id' = %s
            )
        ) or (
            js.source_url ilike %s
            and (js.source_url ilike '%%greenhouse%%'
                 or js.source_url ilike '%%gh_jid=%%')
        )
        """,
        (board_token, posting_id, posting_id, f"%{posting_id}%"),
    )
    matches = set()
    for job_id, source_name, source_url, payload in cur.fetchall():
        payload = payload or {}
        board_is_known = (
            source_name == "fantastic_jobs_apify"
            and payload.get("source") == "greenhouse"
            and payload.get("source_slug") == board_token
        )
        native_id_matches = board_is_known and posting_id in {
            str(payload.get("id", "")), str(payload.get("job_id", "")),
        }
        if native_id_matches or greenhouse_url_matches(
            source_url, board_token, posting_id, board_is_known
        ):
            matches.add(job_id)
    return next(iter(matches)) if len(matches) == 1 else None


def ingest_greenhouse_jobs(
    board_token: str | None = None,
    max_boards: int | None = None,
) -> dict[str, Any]:

    database_url = os.environ["DATABASE_URL"]

    adapter = GreenhouseAdapter()

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:

            if board_token:
                boards = [board_token]
            else:
                boards = discover_boards(cur)

            if max_boards is not None:
                boards = boards[:max_boards]

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
                        "board_token": board_token,
                        "board_count": len(boards),
                    }),
                ),
            )

            run_id = cur.fetchone()[0]
            conn.commit()

            fetched = 0
            czech_matched = 0
            details_loaded = 0

            created = 0
            attached = 0
            updated = 0
            failed = 0
            successful_tenants = 0

            board_errors = []

            try:

                for board in boards:

                    try:
                        jobs = adapter.list_jobs(
                            board
                        )

                    except Exception as exc:
                        board_errors.append({
                            "board": board,
                            "error": str(exc),
                        })
                        continue

                    details_before = details_loaded
                    fetched += len(jobs)

                    czech_jobs = [
                        job
                        for job in jobs
                        if is_czech_job(job)
                    ]

                    czech_matched += len(
                        czech_jobs
                    )

                    for summary in czech_jobs:

                        posting_id = summary.get("id")

                        if posting_id is None:
                            failed += 1
                            continue

                        posting_id = str(
                            posting_id
                        )

                        try:
                            raw_job = (
                                adapter.get_job(
                                    board,
                                    posting_id,
                                )
                            )

                            details_loaded += 1

                        except Exception:
                            failed += 1
                            continue

                        if not is_czech_job(
                            raw_job
                        ):
                            continue

                        source_job_id = (
                            f"{board}:"
                            f"{posting_id}"
                        )

                        company_name = (
                            raw_job.get(
                                "company_name"
                            )
                            or board
                        )

                        company_id = (
                            get_or_create_company(
                                cur,
                                company_name,
                            )
                        )

                        title = (
                            raw_job.get("title")
                            or "Unknown position"
                        ).strip()

                        description = clean_html(
                            raw_job.get("content")
                        )

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
                                "absolute_url"
                            )
                        )

                        apply_url = source_url

                        published_at = (
                            raw_job.get(
                                "first_published"
                            )
                            or raw_job.get(
                                "updated_at"
                            )
                        )

                        pay_ranges = (
                            raw_job.get(
                                "pay_input_ranges"
                            )
                        )

                        salary_text = (
                            json.dumps(
                                pay_ranges,
                                ensure_ascii=False,
                            )
                            if pay_ranges
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
                                find_existing_greenhouse_job(
                                    cur,
                                    board,
                                    posting_id,
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

                            update_canonical_job(
                                cur, job_id, SOURCE_NAME,
                                {
                                    "company_id": company_id,
                                    "title": title,
                                    "description": description,
                                    "location_text": location_text,
                                    "country_code": "CZ",
                                    "remote_type": remote_type,
                                    "salary_text": salary_text,
                                    "canonical_url": source_url,
                                    "published_at": published_at,
                                },
                                now,
                                preserve_if_none=('salary_text', 'canonical_url', 'published_at'),
                            )

                        raw_payload = dict(
                            raw_job
                        )

                        raw_payload[
                            "_board_token"
                        ] = board

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

                    if not czech_jobs or details_loaded > details_before:
                        successful_tenants += 1
                    else:
                        board_errors.append(
                            {"board": board, "error": "No Czech posting details could be loaded"}
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
                            "board_token":
                                board_token,
                            "board_count":
                                len(boards),
                            "czech_matched":
                                czech_matched,
                            "details_loaded":
                                details_loaded,
                            "successful_tenants": successful_tenants,
                            "attached":
                                attached,
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
                    "status": run_status,
                    "run_id": str(run_id),
                    "boards": len(boards),
                    "fetched": fetched,
                    "czech_matched":
                        czech_matched,
                    "details_loaded":
                        details_loaded,
                    "created": created,
                    "attached": attached,
                    "updated": updated,
                    "failed": failed,
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
