# LIGHT transient transport resilience

This is code/offline verification only. No production ingestion, deployment,
reconciliation, scheduling or data repair is part of this milestone.

## Incident and bounded diagnostics

The 2026-10-15 final LIGHT run preserved all 62 existing `global/tsmg` source
rows after a native HTTP read timeout. It correctly returned PARTIAL rather than
excusing an unexpected failure as a reviewed coverage gap.

Two public diagnostic requests (`limit=100`, `skip=0` and `skip=100`) subsequently
returned HTTP 200, approximately 2.3/2.4 seconds each, with 100 unique identities
per page and no cross-page overlap. This supports a transient failure diagnosis:
the scope is valid and initial pagination works. It does not prove an entire
inventory complete, rule out intermittent upstream problems or guarantee that a
retry succeeds. No full inventory, detail processing or database write was run.
Exact before/after diagnostics are under
`/tmp/career-os-light-transient-resilience-20261015/`.

## Retry policy and execution boundary

`freshness.json` defines `max_transient_retries: 1` and
`retry_backoff_seconds: 10`. Validation permits 0/1 retries and a finite 5–15s
backoff. There is no exponential backoff, whole-source retry or whole-run retry.

The guarded runner enables this policy for the five LIGHT source families,
including their LIGHT portion in the legacy guarded `all` group. HEAVY and
aggregators do not receive it. Ordinary ingestion HTTP endpoints retain their
existing single-attempt behavior: the additional attempt requires an explicit
guarded execution context and an operation budget. No endpoint starts new work
automatically.

All LIGHT ingesters use one shared `InventoryAcquisition` controller per native
source run. It invokes a **read-only acquisition callback** once, then at most
once more after a positively classified transient failure. A second `run()` for
the same scope is rejected, preventing another implicit retry allowance.

For Lever/SmartRecruiters pagination, callback-local page buffers, seen sets,
offsets and HTTP clients are freshly constructed. Workable/Ashby/Greenhouse
listing requests are freshly repeated too. SmartRecruiters and Greenhouse fetch
the required details into a per-attempt buffer before any company/job/source
writes. A transient detail failure under the guarded policy restarts the entire
read-only scope acquisition, including listing and detail requests. Failed
buffers are discarded. Non-transient detail failures retain existing partial
record/abstention behavior, and never authorize absence reconciliation.

Both transient attempts failing abandons that scope without acquisition writes.
Other healthy scopes can continue under existing source/result rules. Subsequent
database processing, identity resolution, validation, lifecycle completion and
COMMIT are never retried by this mechanism. Ambiguous commit handling is unchanged.

## Retryable and non-retryable errors

Retryable:

- `httpx.ReadTimeout` and `httpx.ConnectTimeout`.
- Positively identified connection reset/abort, OS timeout and temporary DNS
  `EAI_AGAIN` causes in transport exceptions. Generic error-message matching is
  deliberately absent.
- Typed native HTTP 429, 502, 503 and 504. All LIGHT listing/detail adapters
  retain the numeric status and optional Retry-After without copying response
  bodies into errors.

Not retryable: 400/401/403/404, unreviewed HTTP statuses (including 500), malformed
JSON/data, configuration/validation failures, pagination overlap/no-progress,
completeness failures, operation-budget exhaustion, unknown transport/TLS or
permanent DNS errors, local pool timeouts and arbitrary unknown exceptions.

Retry-After is honored conservatively. The configured backoff is used only if
it is at least the server's requested delay. Longer or unusable Retry-After
guidance skips the retry; it never causes an unbounded sleep or early retry.

## Budgets and lifecycle safety

Before sleeping, remaining source/group budget must exceed the backoff plus
`max(60 seconds, first attempt elapsed time)`. The reserve covers at least one
native request timeout or the previous traversal duration; it is not a guarantee
that a longer second inventory can finish. Budget is checked again after sleep.
Insufficient budget skips the additional attempt and reports the original error.
All existing per-request, observation, reconciliation and pre-COMMIT cooperative
deadline checks remain. There is no process kill or transaction interruption.

Lifecycle snapshots are acquired **after** final acquisition, never during a
failed attempt. The existing source advisory lock remains held throughout.
Only final-attempt rows can be observed or written. Existing exact ownership,
eligibility, identity validation and completeness checks decide whether the
single lifecycle `finish()` may reconcile. No union of partial attempts is
possible. Successful HTTP acquisition is explicitly distinct from authoritative
COMPLETE: malformed records can still leave the final scope INCOMPLETE.

`lifecycle.py`, canonical activity semantics, source identity/attachment rules,
locks, transaction boundaries, deadlines and the reviewed Lever registry are
unchanged. Jobgether remains NEEDS_REVIEW: its one-record availability probe is
outside this inventory-retry path, cannot authorize reconciliation and cannot
automatically re-enable inventory handling. Pagination guards are unchanged.

## Results and monitoring

Each normal ingestion result and persisted `ingestion_runs.metadata` includes
`scope_attempts` and `retry_metrics`. The runner retains these per source and
aggregates counters at group level. Context collection also retains attempted
acquisition diagnostics if a later fatal processing/transaction error propagates
to the runner, even though rolled-back run metadata cannot retain those writes.

Each scope record contains scope, attempt number/result/duration, error class,
typed HTTP status, retryable flag, retry attempted, actual backoff, skip reason,
retry result, final scope result, lifecycle outcome and total elapsed scope time
through normal completion. ACQUIRED is not a completeness claim. Fatal processing
before lifecycle completion reports ABORTED alongside FAILED source status;
it must never be interpreted as COMPLETE. Exception text/URLs are not
copied into retry records. Logs show the retry decision and acquisition recovery.

Counters:

- `transient_failures`: transient exceptions across both acquisition attempts.
- `transient_retries`: actual second callback invocations.
- `retry_successes`: retries that reach complete lifecycle inventory.
- `retry_failures`: failed second acquisition or subsequently incomplete scope.

A recovered complete inventory does not force PARTIAL, but its retry remains
visible. Exhausted/non-retryable/uncertain scopes still produce PARTIAL/FAILED
under existing contracts. A healthy group with only the reviewed Jobgether gap
can return SUCCESS_WITH_KNOWN_GAPS. Transient failures are never added to the
reviewed-gap registry or automatically hidden.

## Verification and activation

Offline tests cover the exact TSMG ReadTimeout/success and two-timeout cases,
first-attempt page isolation, strict Jobgether pagination, detail traversal
restart, HTTP classes/Retry-After, budget suppression, all five ingestion paths,
metadata, group counters and known-gap/partial outcome behavior. Test database
containers have no production credentials or external network access.

After review/checkpoint and separately authorized deployment, one separately
authorized `bash scripts/run-direct-light.sh --apply` validation is appropriate.
Do not retry that command automatically. Daily cron remains unapproved until
that run meets all existing lifecycle, resolver, Fliff, reviewed Jobgether,
unexpected-failure, runtime/lock/health and monitoring acceptance conditions.
If those pass, daily LIGHT is appropriate; schedule installation remains a
separate explicit operation. No schedule is installed by this code change.
