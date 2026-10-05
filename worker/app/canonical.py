from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from app.source_priority import source_priority


# Only ingestion-owned canonical fields may be passed to this helper.
CANONICAL_FIELDS = frozenset({
    "company_id", "title", "description", "location_text", "country_code",
    "remote_type", "employment_type", "salary_text", "skills",
    "canonical_url", "published_at", "expires_at",
})


def field_is_empty(field: str, value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        if not value.strip():
            return True
        if field == "remote_type" and value == "unknown":
            return True
    return field == "skills" and value == []


def canonical_field_updates(
    current: Mapping[str, Any],
    incoming: Mapping[str, Any],
    incoming_source: str,
    backing_sources: Iterable[str],
    preserve_if_none: Iterable[str] = (),
) -> dict[str, Any]:
    """Protect populated fields using the highest attached source priority.

    Without per-field provenance, protection is deliberately job-wide and
    includes inactive source rows, just as merge priority does. Equal/higher
    sources retain their existing update and coalesce semantics.
    """
    authoritative = source_priority([incoming_source]) >= source_priority(
        backing_sources
    )
    preserve = set(preserve_if_none)
    updates = {}
    for field, value in incoming.items():
        if authoritative:
            if value is not None or field not in preserve:
                updates[field] = value
        elif field_is_empty(field, current.get(field)):
            if not field_is_empty(field, value):
                # A fallback title is not useful enrichment.
                if field != "title" or value != "Unknown position":
                    updates[field] = value
    return updates


def update_canonical_job(
    cur,
    job_id: Any,
    incoming_source: str,
    fields: Mapping[str, Any],
    now: datetime,
    preserve_if_none: Iterable[str] = (),
) -> None:
    """Serialize canonical updates; source rows remain the caller's concern."""
    if not fields or set(fields) - CANONICAL_FIELDS:
        raise ValueError("Unsupported canonical update fields")

    columns = list(fields)
    cur.execute(
        sql.SQL("select {} from public.jobs where id = %s for update").format(
            sql.SQL(", ").join(map(sql.Identifier, columns))
        ),
        (job_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError("Canonical job no longer exists")
    current = dict(zip(columns, row))

    # Read sources after acquiring the job lock so a waiting lower-priority
    # writer sees sources committed by the preceding canonical writer.
    cur.execute(
        "select source_name from public.job_sources where job_id = %s",
        (job_id,),
    )
    updates = canonical_field_updates(
        current, fields, incoming_source,
        [row[0] for row in cur.fetchall()], preserve_if_none,
    )
    updates.update({
        "last_seen_at": now,
        "last_verified_at": now,
        "updated_at": now,
    })
    assignments = sql.SQL(", ").join(
        sql.SQL("{} = %s").format(sql.Identifier(field))
        for field in updates
    )
    values = [
        Jsonb(value) if field == "skills" and value is not None else value
        for field, value in updates.items()
    ]
    cur.execute(
        sql.SQL("update public.jobs set {} where id = %s").format(assignments),
        (*values, job_id),
    )
