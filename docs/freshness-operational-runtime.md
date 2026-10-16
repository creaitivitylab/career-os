# Freshness operational runtime

The host requires Bash, Linux `flock`, Docker with the Compose plugin, and basic
utilities (`cat`, `cp`, `date`, `dirname`, `mkdir`, `mv`, `tee`, `wc`). It does not
require Python, httpx, psycopg, Pydantic, FastAPI, jq, or a sourced `.env`.
Do not install worker packages on the VPS host to repair operational scripts.

## Boundary and commands

Previously, `_run-freshness.sh` imported `app.direct_freshness` on the host for
planning and again for validation/baseline updates **after source commits**.
Its new dependency chain through `app.transient_retry` to httpx exposed both
paths. Apply already executed ingestion in Docker, and reporting already ran
in Docker. Fixing only the plan import would leave a post-commit failure hazard.

Now `_freshness-worker.sh` resolves exactly one running Compose `worker` from
the repository directory, then targets that resolved container ID for both exec
and artifact copy. IDs are not hardcoded. `/app` is the execution directory and
PYTHONPATH; the worker's existing environment supplies credentials. There is no
host Python fallback, automatic worker start, rebuild, restart or deployment.
An unavailable, ambiguous, or inaccessible worker fails clearly with exit1.

The shell streams the repository's small `scripts/freshness-runtime.py` driver
with a length prefix and baseline JSON to Python **inside the worker**. This
operator driver imports all application code from the running image, never
host `worker/app`. It does not copy over deployed application files. This lets
host operational scripts use the existing deployed application interfaces
before a new image is deployed. Future changes to those interfaces still need
compatible deployment; no fallback guesses application behavior.

From any directory:

```bash
bash /home/careeros/apps/career-os/scripts/run-direct-light.sh
bash /home/careeros/apps/career-os/scripts/run-direct-heavy.sh
bash /home/careeros/apps/career-os/scripts/freshness-report.sh
```

The first two are plan-only: no database/API requests. The report enforces a
read-only repeatable-read transaction. Plans, reports and all validation execute
in the worker. Plan abbreviations such as `--app` are rejected, so they cannot
accidentally enable apply through argparse abbreviation.

Only with separate production authorization:

```bash
bash scripts/run-direct-light.sh --apply
bash scripts/run-direct-heavy.sh --apply
```

The driver imports the runner, outcome contract and baseline functions, parses
and validates the saved baseline **before calling ingestion**. Within one
worker Python invocation it executes the application entrypoint, preserves its
stdout, validates result schema/group/status/exit code, and generates the next
baseline. There is no return to host Python after ingestion. The shell owns
baseline and report output paths; do not supply `--baseline-stdin` or
`--output-dir`. Other application arguments retain their existing meaning.

## Locks and outcomes

All host light/heavy/all calls share `logs/freshness/runner.lock`. It is acquired
before Docker invocation and held through artifact recovery and atomic baseline
promotion. Host contention returns75 and `SKIPPED_LOCKED` without ingestion.
Keep the lock file: never unlink it as a stale-lock remedy.

The existing worker `/tmp/career-os-freshness/runner.lock` remains unchanged.
It protects a running ingestion even if SSH/the host script disappears. Existing
database advisory locks still protect source transactions and direct API calls.
Reports are read-only and do not take the orchestration lock. This remains a
single-host design; multiple hosts need separately reviewed distributed locking.

| Application outcome | Host exit |
| --- | --- |
| SUCCESS | 0 |
| SUCCESS_WITH_KNOWN_GAPS | 0, explicit degraded coverage |
| PARTIAL | 2 |
| FAILED | 1 |
| SKIPPED_LOCKED (host or worker) | 75 |

The shell captures Docker's position in `PIPESTATUS`, not tee's exit. Input or
log transport failure, missing worker, invalid result, Docker failure, copy
failure, or missing/mismatched validation marker returns1. No automatic command
retry occurs. Application argument usage errors have no valid run summary and
are operational failures. Inspect logs to distinguish them from PARTIAL.

## Logs, baselines and recovery

Runner host files:

```text
logs/freshness/runs/<UTC>-<PID>-<group>/command.stdout
logs/freshness/runs/<UTC>-<PID>-<group>/command.log
logs/freshness/runs/<UTC>-<PID>-<group>/artifacts/plan.json
logs/freshness/runs/<UTC>-<PID>-<group>/artifacts/result.json
logs/freshness/runs/<UTC>-<PID>-<group>/artifacts/baseline.json
logs/freshness/runs/<UTC>-<PID>-<group>/artifacts/validated.json
logs/freshness/baseline-<group>.json
```

Only the relevant plan/result files exist. The report uses
`logs/freshness/reports/<UTC>-<PID>/`, with command logs and
`artifacts/{summary.json,report.md,validated.json}`. The host destination is
printed on stderr. Stdout streams unmodified JSON through tee; stderr is saved
and replayed after the worker invocation. Existing cron stdout/stderr redirection
still works; source stderr is also retained in `command.log` during execution.

Worker artifacts are staged in a unique `/tmp/career-os-freshness-runtime-*`
directory and explicitly copied from the same container to host storage.
Container `/tmp` is **not** assumed shared or durable. Only after successful
copy and an exit-code-specific validation marker does the shell atomically
promote a generated baseline with cp + same-directory mv under the host lock.
SKIPPED_LOCKED and plans never replace the baseline. Prior caches remain intact
on failed validation/transfer. Host raw stdout/stderr survive a failed copy.
Staging directories remain for diagnosis until container recreation; neither
host logs nor diagnostic staging are silently deleted. Arrange retention in a
separately approved operations task.

If a command fails after an application may have committed, **do not blindly
retry**. Inspect host logs, the retained worker staging directory if available,
ingestion_runs and current source state. A stopped/replaced container can make
artifact recovery impossible; this is a visible operational failure, not proof
that ingestion rolled back. A lost shell may leave the worker running; inspect
processes and both locks before any separately authorized subsequent run.

## Correctness boundary

Disappearance completeness/ownership checks, retry isolation, cooperative
deadlines and final commit guards remain inside existing source transactions.
Only a final complete authoritative traversal may reconcile. A post-run result
validator or volume-anomaly report cannot roll back source commits. The driver
validates operational results and stores monitoring baselines; it does not
promise group-wide transactional atomicity or undo successful sources after a
later source fails. No lifecycle rules, retry classifications, 1-retry limit,
10s backoff, Fliff casing, Jobgether quarantine, Workday guards or RMK validated
scope rules change in this runtime milestone. No scheduling or dedupe is added.

## Offline bridge checks

```bash
python3 -S -m unittest discover -s tests -p 'test_freshness_runtime.py' -v
```

This **test command**, unlike operational scripts, uses stdlib host Python. It
replaces Docker with a fake worker, poisons host python/python3 commands, and
executes the real streamed driver against synthetic worker modules. It tests
success/known gaps/partial/failure/lock exits, transport failures, locks,
artifacts, baseline round trips, stderr, metrics, and no-host-dependency behavior.
It never invokes real Docker, database writes or external APIs. The normal full
worker suite separately verifies application retry and lifecycle guarantees.
