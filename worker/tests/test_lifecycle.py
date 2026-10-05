"""Offline lifecycle regression tests; no network or PostgreSQL connection."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from app.lifecycle import (
    canonical_activity, LifecycleRun, InventoryCompletion, scope_predicate,
)
from app.adapters.smartrecruiters import SmartRecruitersAdapter
from app.adapters.workable import WorkableAdapter
from app.adapters.ashby import AshbyAdapter
from app.adapters.greenhouse import GreenhouseAdapter
from app.adapters.lever import LeverAdapter


COMPLETE = InventoryCompletion(True, True, "complete native inventory")


class Store:
    """Stateful DB fake executing lifecycle effects, including scope predicates."""
    def __init__(self, rows=(), busy=False):
        self.sources = {r["identity"]: dict(r) for r in rows}
        self.jobs = {r["job"]: "active" for r in rows}
        self.rows, self.calls, self.busy = [], [], busy

    def matches(self, query, args):
        source, *scope = args
        def owns(row):
            if row["source"] != source:
                return False
            if not scope:
                return True  # Positive exact identity evidence, independent of brand.
            parts = row["identity"].split(":")
            if source == "workday_direct":
                return parts[:2] == scope[:2] and row.get("host") == scope[2]
            if source == "successfactors_direct":
                return parts[0] == scope[0] and row.get("host") == scope[1] and row.get("brand") == scope[2]
            return parts[:len(scope)] == scope
        return [r for r in self.sources.values() if owns(r)]

    def execute(self, query, params=()):
        query = " ".join(query.split())
        self.calls.append((query, params)); self.rows = []
        if query.startswith("select pg_try_advisory"):
            self.rows = [(not self.busy,)]
        elif query.startswith("select source_job_id"):
            rows = ([r for r in self.sources.values() if r["source"] == params[0] and r["identity"] in params[1]]
                    if "source_job_id = any" in query else self.matches(query, params))
            self.rows = [(r["identity"], r["job"], r["active"]) for r in rows]
        elif query.startswith("select id from public.jobs"):
            self.rows = [(i,) for i in sorted(params[0])]
        elif query.startswith("update public.job_sources"):
            present = "is_active = true," in query
            offset = 4 if present else 3
            for row in self.matches(query, params[offset:-1]):
                if row["identity"] in params[-1] and (present or row["active"] is True):
                    row["active"] = present
                    if present:
                        row["last_seen"] = params[0]
                    row["last_verified"] = params[0]
                    row["lifecycle"] = params[offset-1].obj
                    if not present:
                        self.rows.append((row["job"],))
        elif query.startswith("update public.jobs j set status"):
            for job in params[1]:
                value = canonical_activity((r["source"], r["active"]) for r in self.sources.values() if r["job"] == job)
                if value is not None:
                    self.jobs[job] = value
        else:
            raise AssertionError(query)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def row(identity="acme:1", source="ashby_direct", active=True, job=None, **extra):
    return dict(identity=identity, source=source, active=active, job=job or identity,
                first_seen="original", last_seen="original", last_verified="original", **extra)


class ActivityTests(unittest.TestCase):
    def test_active_source_keeps_canonical_active(self):
        self.assertEqual(canonical_activity([("ashby_direct", False), ("lever_direct", True)]), "active")

    def test_all_direct_sources_inactive_closes(self):
        self.assertEqual(canonical_activity([("ashby_direct", False), ("lever_direct", False)]), "inactive")

    def test_unknown_and_incomplete_feed_never_falsely_close(self):
        for source, active in (("unknown_source", False), ("fantastic_jobs_apify", False),
                               ("jooble_direct", False), ("workday_direct", None)):
            self.assertEqual(canonical_activity([("ashby_direct", False), (source, active)]), "active")

    def test_no_sources_preserves_unknown_state(self):
        self.assertIsNone(canonical_activity([]))

    def test_active_authoritative_source_not_overridden_by_expiry(self):
        # Activity derives from source state only; expiry is intentionally absent.
        self.assertEqual(canonical_activity([("workday_direct", True)]), "active")


class ReconciliationTests(unittest.TestCase):
    def reconcile(self, rows, seen=(), completion=COMPLETE):
        db = Store(rows); run = LifecycleRun(db, "ashby_direct", "run-1"); scope = run.scope("acme")
        for identity in seen:
            scope.observe(identity)
        result = scope.finish(completion)
        return db, result

    def test_present_stays_active_and_preserves_first_seen(self):
        db, result = self.reconcile([row()], ["acme:1"])
        self.assertTrue(db.sources["acme:1"]["active"])
        self.assertEqual(db.sources["acme:1"]["first_seen"], "original")
        self.assertIsInstance(db.sources["acme:1"]["last_seen"], datetime)
        self.assertEqual(result["deactivated"], 0)

    def test_missing_complete_deactivates_and_closes_direct_only(self):
        db, result = self.reconcile([row()])
        self.assertFalse(db.sources["acme:1"]["active"])
        self.assertEqual(db.jobs["acme:1"], "inactive")
        self.assertEqual(db.sources["acme:1"]["last_seen"], "original")
        self.assertIsInstance(db.sources["acme:1"]["last_verified"], datetime)
        self.assertEqual(result["deactivated"], 1)

    def test_empty_complete_catalog_reconciles(self):
        self.assertEqual(self.reconcile([row()])[1]["deactivated"], 1)

    def test_failed_partial_unresolved_and_pagination_uncertain_preserve(self):
        for completion in (InventoryCompletion(False, True, "failed"),
                           InventoryCompletion(True, False, "partial"),
                           InventoryCompletion(False, False, "unresolved"),
                           InventoryCompletion(True, False, "pagination uncertainty")):
            db, result = self.reconcile([row()], completion=completion)
            self.assertTrue(db.sources["acme:1"]["active"])
            self.assertEqual(db.sources["acme:1"]["last_verified"], "original")
            self.assertFalse(result["complete"])

    def test_reappearance_reactivates_same_source_and_canonical(self):
        db = Store([row(active=False)]); db.jobs["acme:1"] = "inactive"
        run = LifecycleRun(db, "ashby_direct", "run-2"); scope = run.scope("acme"); scope.observe("acme:1")
        result = scope.finish(COMPLETE)
        self.assertEqual(len(db.sources), 1)
        self.assertTrue(db.sources["acme:1"]["active"])
        self.assertEqual(db.jobs["acme:1"], "active")
        self.assertEqual(result["reactivated"], 1)

    def test_partial_success_can_reactivate_seen_but_never_close_missing(self):
        db, result = self.reconcile([row(active=False), row("acme:2")], ["acme:1"],
                                   InventoryCompletion(True, False, "detail failed"))
        self.assertTrue(all(r["active"] for r in db.sources.values()))
        self.assertEqual(result["reactivated"], 1)

    def test_other_scope_and_source_untouched(self):
        db, result = self.reconcile([row(), row("acme-extra:2"), row("other:3"),
                                    row("acme:4", source="greenhouse_direct")])
        self.assertEqual(result["deactivated"], 1)
        self.assertTrue(all(db.sources[i]["active"] for i in ("acme-extra:2", "other:3", "acme:4")))

    def test_inactive_before_run_never_verified_as_new_disappearance(self):
        db, result = self.reconcile([row(active=False)])
        self.assertEqual(result["deactivated"], 0)
        self.assertEqual(db.sources["acme:1"]["last_verified"], "original")

    def test_active_fantastic_or_jooble_keeps_canonical_open(self):
        for source in ("fantastic_jobs_apify", "jooble_direct"):
            db, result = self.reconcile([row(job="shared"), row("feed", source=source, job="shared")])
            self.assertEqual(db.jobs["shared"], "active")
            self.assertTrue(db.sources["feed"]["active"])
            self.assertEqual(db.sources["feed"]["last_seen"], "original")

    def test_fantastic_and_jooble_cannot_start_reconciliation(self):
        for source in ("fantastic_jobs_apify", "jooble_direct"):
            with self.assertRaises(ValueError):
                LifecycleRun(Store(), source, "run")

    def test_busy_run_fails_before_snapshot_or_write(self):
        db = Store([row()], busy=True)
        with self.assertRaisesRegex(RuntimeError, "already running"):
            LifecycleRun(db, "ashby_direct", "run")
        self.assertEqual(len(db.calls), 1)
        self.assertTrue(db.sources["acme:1"]["active"])

    def test_scope_cannot_finish_twice(self):
        scope = LifecycleRun(Store(), "ashby_direct", "run").scope("acme"); scope.finish(COMPLETE)
        with self.assertRaises(RuntimeError):
            scope.finish(COMPLETE)

    def test_observation_cannot_cross_native_tenant(self):
        scope = LifecycleRun(Store(), "ashby_direct", "run").scope("acme")
        with self.assertRaises(ValueError):
            scope.observe("other:123")

    def test_positive_shared_rmk_identity_reactivates_across_brand_without_absence(self):
        db = Store([row("tenant:123", "successfactors_direct", active=False,
                        host="jobs.example", brand="other")])
        db.jobs["tenant:123"] = "inactive"
        scope = LifecycleRun(db, "successfactors_direct", "run").scope("jobs.example", "tenant", "cz")
        scope.observe("tenant:123")
        scope.finish(InventoryCompletion(True, False, "shared brand ownership"))
        self.assertTrue(db.sources["tenant:123"]["active"])
        self.assertEqual(db.jobs["tenant:123"], "active")

    def test_only_preexisting_active_rows_can_disappear(self):
        db = Store([row()]); scope = LifecycleRun(db, "ashby_direct", "run").scope("acme")
        db.sources["acme:later"] = row("acme:later")
        scope.finish(COMPLETE)
        self.assertTrue(db.sources["acme:later"]["active"])

    def test_observed_duplicates_do_not_duplicate_source_identity(self):
        db, result = self.reconcile([row()], ["acme:1", "acme:1"])
        self.assertEqual(result["observed"], 1)
        self.assertEqual(len(db.sources), 1)

    def test_workday_scope_preserves_case_and_host(self):
        db = Store([row("acme:Careers:wid", "workday_direct", host="acme.wd1.myworkdayjobs.com"),
                    row("acme:careers:other", "workday_direct", host="acme.wd1.myworkdayjobs.com"),
                    row("acme:Careers:second", "workday_direct", host="acme.wd3.myworkdayjobs.com")])
        scope = LifecycleRun(db, "workday_direct", "run").scope("acme.wd1.myworkdayjobs.com", "acme", "Careers")
        result = scope.finish(COMPLETE)
        self.assertEqual(result["deactivated"], 1)
        self.assertTrue(db.sources["acme:careers:other"]["active"])
        self.assertTrue(db.sources["acme:Careers:second"]["active"])

    def test_unresolved_rmk_never_deactivates(self):
        db = Store([row("tenant:123", "successfactors_direct", host="jobs.example", brand="cz")])
        scope = LifecycleRun(db, "successfactors_direct", "run").scope("jobs.example", "tenant", "cz")
        scope.finish(InventoryCompletion(False, False, "unresolved"))
        self.assertTrue(db.sources["tenant:123"]["active"])


class InventoryShapeTests(unittest.TestCase):
    def test_lever_overlapping_pages_are_not_complete(self):
        pages = iter([[{"id": "1"}, {"id": "2"}], [{"id": "2"}, {"id": "3"}]])
        client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=next(pages))))
        with patch("httpx.Client", return_value=client), patch.object(LeverAdapter, "PAGE_SIZE", 2):
            with self.assertRaisesRegex(RuntimeError, "did not advance"):
                LeverAdapter().list_postings("acme")

    def test_greenhouse_contradictory_count_is_not_complete(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
            200, json={"jobs": [], "meta": {"total": 10}})))
        with patch("httpx.Client", return_value=client), self.assertRaisesRegex(RuntimeError, "contradicts"):
            GreenhouseAdapter().list_jobs("acme")

    def test_smartrecruiters_absence_scope_requires_native_czech_ownership(self):
        sql, args = scope_predicate("smartrecruiters_direct", ("acme",))
        self.assertIn("location'->>'country'", sql)
        self.assertIn("= 'cz'", sql)
        self.assertEqual(args, ("acme",))

    def test_missing_inventory_is_not_an_empty_complete_site(self):
        for adapter, method, args in ((GreenhouseAdapter(), "list_jobs", ("acme",)),
                                      (AshbyAdapter(), "list_jobs", ("acme",)),
                                      (WorkableAdapter(), "get_account", ("acme",))):
            client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})))
            with patch("httpx.Client", return_value=client), self.assertRaisesRegex(RuntimeError, "jobs response"):
                getattr(adapter, method)(*args)

    def test_smartrecruiters_repeated_or_contradictory_pages_fail(self):
        for responses in (({"content": [{"id": "1"}], "totalFound": 2},)*2,
                          ({"content": [{"id": "1"}], "totalFound": 2}, {"content": [], "totalFound": 2}),
                          ({"content": [], "totalFound": 1},)):
            stream = iter(responses)
            client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=next(stream))))
            with patch("httpx.Client", return_value=client), self.assertRaises(RuntimeError):
                SmartRecruitersAdapter().list_postings("acme")

    def test_smartrecruiters_complete_empty_or_nonempty_inventory(self):
        for jobs in ([], [{"id": "one"}]):
            client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"content": jobs, "totalFound": len(jobs)})))
            with patch("httpx.Client", return_value=client):
                self.assertEqual(SmartRecruitersAdapter().list_postings("acme"), jobs)
