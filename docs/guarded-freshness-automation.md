# Guarded recurring direct ATS freshness

This milestone supplies code and tests, not an installed schedule. It does not
deploy a worker, ingest production, change canonical activity, migrate schema,
rebuild dedupe or merge anything. Production validation is read-only.

## Execution paths reviewed before changes

The existing `scripts/run-direct-ats.sh` used sequential HTTP POSTs to the seven
worker `/ingest/ats/*` routes, with default requests except
`{"validated_scopes":true}` for RMK. It had no outer lock, overall deadline or
structured check of HTTP200 partial outcomes. Formatting JSON was not a success
check. The normal ingestion functions already have the following transaction
boundary: commit the running ingestion_run first; hold the source advisory
transaction lock while acquiring/writing/reconciling; commit inventory and run
result together; on a fatal exception rollback acquisition and record failure in
a separate transaction. A scope-level failure may coexist with committed healthy
scopes, but never proves absence in the failed scope.

Current source scopes remain: SmartRecruiters PUBLIC/CZ tenant; Greenhouse board;
Workable account/shortcode; Ashby public listed board; Lever instance/site;
Workday host/tenant/case-sensitive career site; RMK reviewed host/native tenant/
brand. Workday must have complete unfiltered fallback with no failed detail,
pathless listing or foreign pruning. RMK must have reviewed fallback in every
view, full advertised-locale coverage, unambiguous single-brand ownership and no
failed detail/pruning. Filtered, partial, unresolved and omitted scopes do not
authorize disappearance. Fantastic and Jooble remain non-authoritative feeds.

Docker Compose runs web and a FastAPI worker (`uvicorn app.main:app`), both with
restart unless-stopped. Worker credentials already come from Compose `env_file`;
the new shell scripts never source or print `.env`. Restart is not scheduling.
Fantastic and Jooble cron scripts retain their existing behavior/cadence,
including their existing dedupe flows. They are unchanged by this milestone.

## Entry points and explicit execution

Run from any directory using an absolute path. A default invocation prints a
plan inside the running worker and makes no DB/API request. It acquires the host
lock and saves local operational logs/artifacts:

```bash
bash /home/careeros/apps/career-os/scripts/run-direct-light.sh
bash /home/careeros/apps/career-os/scripts/run-direct-heavy.sh
```

After a separately approved deployment **and production execution authorization**:

```bash
bash scripts/run-direct-light.sh --apply
bash scripts/run-direct-heavy.sh --apply
```

Light order: SmartRecruiters, Greenhouse, Workable, Ashby, Lever. Heavy: Workday,
then SuccessFactors RMK. The legacy seven-source script delegates to the same
guarded runner with group `all`, also requiring `--apply`; use separate groups for
recurring scheduling. Dedupe is disabled and not required for reconciliation.

Bounded operator validation uses the existing request contracts, not new scraping:

```bash
bash scripts/run-direct-light.sh --apply --only-source ashby_direct \
  --request-json '{"board_name":"REVIEWED_BOARD"}' --source-deadline 300
bash scripts/run-direct-heavy.sh --apply --only-source workday_direct \
  --request-json '{"host":"TENANT.wd1.myworkdayjobs.com","tenant":"TENANT","site":"Careers"}'
bash scripts/run-direct-heavy.sh --apply --only-source successfactors_direct \
  --request-json '{"host":"REVIEWED_RMK_HOST","brand":"REVIEWED_BRAND"}'
```

These are examples, not authority to run them. RMK defaults always use
`validated_scopes=true`. A bounded host/brand must occur in the current repository
scope gate, uses the reviewed native/locale policy, and forbids locale overrides.
Unrestricted discovery, unreviewed hosts, `max_sites` RMK shortcuts and false
validated-scope fallback are rejected. A missing/invalid gate aborts execution.

## Lock ownership and recovery

Host entrypoints acquire a shared nonblocking `flock` on
`logs/freshness/runner.lock`, before locating the worker. This covers light,
heavy and legacy `all`, manual and scheduled calls, including planning and
atomic host baseline promotion. Contention prints `SKIPPED_LOCKED` JSON and
returns75 without invoking the worker. The file is never unlinked; a stale
file without a live kernel lock is harmless. The descriptor closes on exit.

The runner executes the existing Python ingestion functions **inside the worker**,
not a timed-out HTTP client. All light/heavy/all invocations use a shared Linux
`fcntl.flock` at `/tmp/career-os-freshness/runner.lock` in the worker. Lock ownership
is by the worker process/FD, not a PID-file age test. Two CLI runs cannot overlap,
even if a host shell or SSH connection disappears while Docker exec continues.
Manual and scheduled entry points share the same lock. An old file without a
live kernel lock never blocks a new run. Never unlink a locked file: another
inode would defeat exclusion. A worker crash closes the FD; no manual stale-lock
deletion is needed. Persistent owner time supports an unusually-long-busy alert.

The existing database advisory locks remain unchanged and serialize same-source
writers, including direct HTTP endpoint calls. HTTP callers do not acquire the
outer group lock, so use the guarded scripts for normal operator runs. Multiple
worker replicas on different hosts would need a cross-host orchestration lock;
this design targets the current single VPS/worker. Do not override lock paths.

## Deadlines and transaction safety

Defaults in `worker/app/config/freshness.json` are reviewable operational budgets,
not measured SLAs: light group1800s, heavy14400s, all16200s. Source budgets are
SR600s, GH600s, Workable300s, Ashby300s, Lever900s, Workday9000s, RMK5400s.
`--total-deadline SECONDS` overrides a group; `--source-deadline` requires one
selected source. Values must be finite and positive.

The runner passes the earlier source/group monotonic deadline through an internal
ContextVar. Guards check before every native HTTP request, scope acquisition,
observation, reconciliation and the final acquisition commit. Ordinary endpoint
calls without a runner context are unaffected. Deadline exceptions enter the
existing rollback/failure path. Even earlier successful scope writes/deactivations
in the same uncommitted source transaction rollback on a later deadline failure.
The initial running row and normal failed-run bookkeeping are retained.

Deadlines are **cooperative, not hard process-kill timers**. An in-flight HTTP/SQL
operation can overshoot by its native timeout; failure bookkeeping can also take
time. Never interrupt COMMIT. If a commit starts before the deadline and finishes
after it, retain its real result and report a timeout/PARTIAL, not a fictitious
rollback. If commit acknowledgment fails, stop remaining sources and require an
operator to inspect ingestion_runs/source state; do not retry automatically.
SIGKILL, Docker restart and client disconnect are not cancellation protocols.

Operational summaries use UTC start/end and application monotonic elapsed seconds.
Only the seven direct ingestion `finished_at` assignments change from `now()` to
`clock_timestamp()`; unrelated seen/verified/publication timestamps and aggregator
code are untouched. Historical transaction-start completion timestamps cannot be
repaired retrospectively. A completion timestamp is assigned just before commit;
monotonic summary duration includes commit and is the actual runtime measurement.

## Outcomes, anomalies and logs

Exit0 SUCCESS or explicit SUCCESS_WITH_KNOWN_GAPS; exit2 PARTIAL; exit1 FAILED;
exit75 SKIPPED_LOCKED. Reviewed gaps remain degraded coverage, never complete
inventories. See [Lever scope health](lever-scope-health.md) for review expiry,
probe cadence, alerts and activation acceptance. Invalid CLI
arguments may use the usual argparse usage exit2; distinguish via JSON/logs.
Source execution is sequential. An isolated rolled-back failure can allow other
sources; the group still fails/returns partial. Group deadline exhaustion records
remaining sources explicitly unattempted. Lock contention never calls ingestion.

Strict validation requires native run IDs, integer counters and lifecycle scope
diagnostics. It rejects partial-scope deactivation/inconsistent aggregates. HTTP
or function status success alone is insufficient: failed/incomplete/unreported
scopes, records or errors produce PARTIAL/FAILED. `scopes_completed` means usable
successful scopes; `scopes_complete` means authoritative absence-capable scopes.
Heavy filtered discovery normally returns PARTIAL for authoritative completeness,
even if every selected posting is acquired correctly. Do not loosen this to make
the scheduled exit code green. Monitoring should distinguish expected completeness
limitations from new API/config failures.

Flags include: no requested scopes; no authoritative complete scopes; formerly
nonempty source now zero; <20% of prior healthy returned records; duration over
expected budget; lock busy beyond14400s. These influence status/alerts. Result-drop
checks are **post-run operational warnings**, not replacement lifecycle logic or a
way to undo a valid native empty inventory. Healthy baselines are kept by source
and exact request, so bounded validation is not compared to a whole population.
Failures/collapses do not erase the last healthy baseline. RMK validated-config
changes and newly discovered native scopes still need operator review.
The first execution also seeds comparable successful observations from
ingestion_runs in an enforced read-only transaction, so absence of a new cache
does not hide a known-positive-to-zero change. This volume comparison is an alarm,
not proof of scope completeness; legacy metadata can omit past discovery limits.

Scripts write `logs/freshness/runs/<UTC>-<PID>-<group>/command.stdout` and
`command.log`. `artifacts/result.json` is the full validated run result;
`artifacts/plan.json` is a plan. They maintain an atomic
`logs/freshness/baseline-<group>.json`. JSON contains attempted source, timing, run ID,
scope completeness, fetched/created/updated/attached, deactivated/reactivated,
errors, anomalies and timeout. `canonical_closed` is null when legacy lifecycle
results do not provide transition counts; do not infer closures from job.updated_at.
An interrupted or invalid summary is a failure requiring DB/operator verification.
Logs are not pushed to an external monitoring service. Add rotation in a separately
approved operations task; the new files are not silently truncated.

On exit75: inspect owner age/active process; do not delete the lock or blindly
retry. On other nonzero outcomes: inspect per-source errors and scope completeness,
then persisted ingestion_runs and source evidence. Confirm execution ended before
any separately approved bounded retry. No automatic whole-source retries occur.
LIGHT read-only scope acquisition permits at most one classified transient retry;
see [retry boundaries, budgets and metrics](light-transient-resilience.md).

## Read-only freshness reporting

`bash scripts/freshness-report.sh` writes timestamped JSON
and Markdown to `logs/freshness/reports/<UTC>-<PID>/artifacts/`. It uses a single worker-side report
snapshot and copies its artifacts; no production table writes. The direct module
also supports:

```bash
python -m app.freshness_report --output-dir /tmp/freshness-review --native-hours 72
```

That Python command is **inside the worker**, not a host command. See
[the operational runtime boundary](freshness-operational-runtime.md) for host
dependencies, artifact recovery and offline integration tests.

Every DB session explicitly enables READ ONLY REPEATABLE READ before queries,
checks transaction_read_only and uses a local diagnostic statement timeout. The
report includes status/source counts, active evidence age24h/72h/7d/14d/30d, source
composition, separate seen/verified distributions, last successful runs, per-source
last complete unbounded run versus complete scope verification, lifecycle evidence,
derived state counts/subreasons and deterministic per-job classification. Native
and provider verification timestamps are not interchangeable. Inactive negative
verification never refreshes an active job's supporting evidence.

REPORTING ONLY states:

- VERIFIED_ACTIVE: recent positive native evidence, exact owned scope and a
  persisted successful complete scope proof. A filtered or bounded recovery is not
  presented as an authoritative verification. Light bounded scopes can retain
  independently proven complete scope evidence; heavy recovery slices are not
  promoted by this conservative reporting classifier.
- CONFIRMED_INACTIVE: stored inactive/expired canonical with all attached sources
  authoritative and proven absent from their successful complete scopes.
- NEEDS_VERIFICATION: everything else, with reasons including discovery_only,
  direct_evidence_stale, completeness_unproven,
  direct_withdrawn_but_aggregator_present, expired_timestamp_unconfirmed and
  no_recent_native_verification.

A fresh proven native source can override an older expiry signal in reporting.
Aggregator retrieval alone never produces VERIFIED_ACTIVE. No statuses are changed;
the October13 examples (27 direct-withdrawn/provider-held,8967 without active direct
support,52 elapsed expiry) remain flags. JSON separately records current closures
with proven absence and historical closure-event availability. Legacy absence
metadata is overwritten on reappearance; exact historical closure events cannot be
invented. Raw acquisition/Job Profile data is untouched.

## Proposed UTC schedule — do not install yet

Candidate times for **after separately authorized production validation**:

```cron
30 2 * * * /home/careeros/apps/career-os/scripts/run-direct-light.sh --apply >> /home/careeros/apps/career-os/logs/freshness-light-cron.log 2>&1
0 22 * * 0,2,4 /home/careeros/apps/career-os/scripts/run-direct-heavy.sh --apply >> /home/careeros/apps/career-os/logs/freshness-heavy-cron.log 2>&1
0 6 * * * /home/careeros/apps/career-os/scripts/freshness-report.sh >> /home/careeros/apps/career-os/logs/freshness-report-cron.log 2>&1
```

Sun/Tue/Thu22:00 targets Mon/Wed/Fri freshness. Heavy4h cooperative budget nominally
ends02:00, with a30min buffer before light02:30. Light30min budget nominally ends
03:00, before existing Fantastic04:15 and Jooble05:15. This is **not a proven safe
schedule**: heavy runs may exceed budgets while finishing an HTTP/SQL/commit. The
first current-inventory heavy run must be measured before final activation; if it
cannot consistently finish with buffer, move it earlier or split its approved work
in a separately reviewed orchestration change. A shared lock prevents direct-group
overlap but does not lock the existing aggregator scripts. Do not pretend cron
times guarantee isolation or forcibly kill an ambiguous transaction to fit them.

Fantastic and Jooble schedules remain unchanged. A later Fantastic reduction must
cover the entire largest scheduling gap plus safety margin/catch-up. Current24h
lookback is not suitable for multi-day cadence; existing supported7d may be a
reviewed starting point with billing/cap measurements. Jooble remains daily,
quota-budgeted pending novel-yield measurement. No direct per-source dedupe is
added; consolidation of the existing aggregator dedupe schedule is separate work.

## Validation gates

Commit/deploy separately after offline review. Then authorize one bounded light
scope to verify safe commit/rollback, logs, exit code and idempotence. Next authorize
a measured heavy run over explicitly approved scope/configuration, preserving
RMK gate and filtered/incomplete protections. Only then authorize scheduler
installation, verify root/private schedules, log permissions/rotation and runtime
buffer. This milestone does not grant any of those production actions.
