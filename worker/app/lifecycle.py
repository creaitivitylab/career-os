"""Transaction-scoped, fail-closed reconciliation of complete native inventories."""
from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from app.source_priority import SOURCE_PRIORITY


DIRECT_SOURCES = tuple(name for name, priority in SOURCE_PRIORITY.items() if priority == 300)


def canonical_activity(sources):
    """Unknown lifecycle must not close a job; expiry never overrides live sources."""
    sources = list(sources)
    if not sources:
        return None
    return "active" if any(active is not False or name not in DIRECT_SOURCES
                           for name, active in sources) else "inactive"


def refresh_job_activity(cur, job_ids):
    ids = sorted(set(job_ids), key=str)
    if not ids:
        return
    # Follow canonical writer lock order: canonical first, then source writes.
    cur.execute("select id from public.jobs where id = any(%s) order by id for update", (ids,))
    cur.execute("""update public.jobs j set status = case
        when exists (select 1 from public.job_sources s where s.job_id = j.id
                     and (s.is_active is distinct from false or not (s.source_name = any(%s))))
        then 'active' else 'inactive' end,
        last_seen_at = greatest(j.last_seen_at, (select max(s.last_seen_at)
            from public.job_sources s where s.job_id = j.id)),
        last_verified_at = greatest(j.last_verified_at, (select max(s.last_verified_at)
            from public.job_sources s where s.job_id = j.id)),
        updated_at = greatest(j.updated_at, clock_timestamp())
        where j.id = any(%s)
          and exists (select 1 from public.job_sources s where s.job_id = j.id)
        """, (list(DIRECT_SOURCES), ids))


@dataclass(frozen=True)
class InventoryCompletion:
    successful: bool
    complete: bool
    reason: str

    @property
    def may_reconcile(self):
        return self.successful and self.complete


def scope_predicate(source, scope):
    """Exact native ownership; never LIKE/prefix substring matching."""
    if source not in DIRECT_SOURCES:
        raise ValueError("Absence reconciliation is restricted to direct ATS sources")
    sizes = {"lever_direct": 2, "workday_direct": 3, "successfactors_direct": 3}
    if len(scope) != sizes.get(source, 1) or any(not isinstance(v, str) for v in scope):
        raise ValueError("Invalid native inventory scope")
    if any(not v for v in scope[:2] if source == "successfactors_direct"):
        raise ValueError("Missing native host/tenant")
    if source == "workday_direct":
        host, tenant, site = scope
        if any(not value or ':' in value for value in scope):
            raise ValueError("Missing native Workday scope")
        return ("split_part(source_job_id, ':', 1) = %s and split_part(source_job_id, ':', 2) = %s "
                "and raw_payload->'workday_site'->>'host' = %s", (tenant, site, host))
    if source == "successfactors_direct":
        host, tenant, brand = scope
        if ':' in host or ':' in tenant:
            raise ValueError("Invalid native RMK scope")
        return ("split_part(source_job_id, ':', 1) = %s and raw_payload->'rmk_site'->>'host' = %s "
                "and raw_payload->'rmk_site'->>'brand' = %s", (tenant, host, brand))
    if any(not value or ':' in value for value in scope):
        raise ValueError("Invalid native inventory scope")
    if source == "smartrecruiters_direct":
        # The existing adapter's complete inventory is PUBLIC + CZ, not global.
        # Unproven/foreign legacy rows are outside that inventory boundary.
        return ("split_part(source_job_id, ':', 1) = %s "
                "and lower(raw_payload->'location'->>'country') = 'cz'", tuple(scope))
    return (" and ".join(f"split_part(source_job_id, ':', {i}) = %s" for i in range(1, len(scope) + 1)), tuple(scope))


class LifecycleRun:
    def __init__(self, cur, source, run_id):
        if source not in DIRECT_SOURCES:
            raise ValueError("Incomplete feed cannot reconcile absence")
        self.cur, self.source, self.run_id = cur, source, str(run_id)
        self.results = []
        # A single transaction lock serializes same-source runs, including RMK
        # identities shared across brands. Busy runs fail before fetching/writing.
        # No session locks, pooler assumptions, timestamp comparisons or migration.
        cur.execute("select pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("career-os:lifecycle:" + source,))
        row = cur.fetchone()
        if not row or row[0] is not True:
            raise RuntimeError("Another ingestion for this source is already running")

    def scope(self, *parts):
        return InventoryScope(self, parts)

    def summary(self):
        return {"scopes": self.results, "deactivated": sum(r["deactivated"] for r in self.results),
                "reactivated": sum(r["reactivated"] for r in self.results)}


class InventoryScope:
    def __init__(self, run, parts):
        self.run, self.parts = run, parts
        predicate, args = scope_predicate(run.source, parts)
        self.where, self.args = "source_name = %s and " + predicate, (run.source, *args)
        run.cur.execute("select source_job_id, job_id, is_active from public.job_sources where " + self.where,
                        self.args)
        self.before = {identity: (job, active) for identity, job, active in run.cur.fetchall()}
        self.seen, self.finished = set(), False

    def observe(self, identity):
        if not isinstance(identity, str) or not identity:
            raise ValueError("Missing native source identity")
        prefix = (self.parts[1:] if self.run.source == "workday_direct" else
                  (self.parts[1],) if self.run.source == "successfactors_direct" else self.parts)
        tokens = identity.split(":")
        if tuple(tokens[:len(prefix)]) != prefix or len(tokens) != len(prefix) + 1 or not tokens[-1]:
            raise ValueError("Native posting does not belong to this scope")
        self.seen.add(identity)

    def finish(self, completion):
        if self.finished:
            raise RuntimeError("Scope already completed")
        self.finished = True
        cur = self.run.cur
        cur.execute("select source_job_id, job_id, is_active from public.job_sources where " + self.where,
                    self.args)
        current = {identity: (job, active) for identity, job, active in cur.fetchall()}
        # A positively verified RMK identity can be shared by brands/aliases.
        # Positive evidence is safe across those views; absence ownership is not.
        cur.execute("select source_job_id, job_id, is_active from public.job_sources "
                    "where source_name = %s and source_job_id = any(%s)",
                    (self.run.source, sorted(self.seen)))
        seen_rows = {identity: (job, active) for identity, job, active in cur.fetchall()}
        missing = {identity for identity, (_, active) in self.before.items()
                   if active is True and identity not in self.seen} if completion.may_reconcile else set()
        observed = self.seen & seen_rows.keys()
        touched = {seen_rows[identity][0] for identity in observed}
        if completion.may_reconcile:
            touched.update(job for job, _ in current.values())
        if touched:
            cur.execute("select id from public.jobs where id = any(%s) order by id for update",
                        (sorted(touched, key=str),))
        now = datetime.now(timezone.utc)
        meta = {"run_id": self.run.run_id, "scope": list(self.parts),
                "inventory_complete": completion.may_reconcile, "reason": completion.reason}
        if observed:
            cur.execute("""update public.job_sources set is_active = true, last_seen_at = %s,
                last_verified_at = %s, updated_at = %s,
                raw_payload = jsonb_set(coalesce(raw_payload, '{}'::jsonb), '{_lifecycle}', %s)
                where source_name = %s and source_job_id = any(%s)""",
                (now, now, now, Jsonb({**meta, "state": "observed"}), self.run.source, sorted(observed)))
        deactivated = []
        if missing:
            cur.execute("""update public.job_sources set is_active = false, last_verified_at = %s,
                updated_at = %s,
                raw_payload = jsonb_set(coalesce(raw_payload, '{}'::jsonb), '{_lifecycle}', %s)
                where """ + self.where + " and is_active = true and source_job_id = any(%s) returning job_id",
                (now, now, Jsonb({**meta, "state": "absent"}), *self.args, sorted(missing)))
            deactivated = cur.fetchall()
        refresh_job_activity(cur, touched)
        result = {"scope": list(self.parts), "successful": completion.successful,
                  "complete": completion.may_reconcile,
                  "reason": completion.reason, "observed": len(observed), "deactivated": len(deactivated),
                  "reactivated": sum(self.before.get(i, (None, None))[1] is False for i in observed)}
        self.run.results.append(result)
        return result
