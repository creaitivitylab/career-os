# Lever scope triage and unattended LIGHT policy

This milestone is code and offline verification only. Deployment, one final
LIGHT validation and scheduler activation require separate authorizations.
No production ingestion, lifecycle reconciliation or data repair was performed.

## Findings from 2026-10-15 public diagnostics

Fliff is still served by Lever. The public
[Fliff board](https://jobs.lever.co/Fliff) advertises the capitalized site path.
`GET https://api.lever.co/v0/postings/fliff?mode=json&limit=100&skip=0`
returned HTTP 404; the same request using `Fliff` returned HTTP 200 and 21
postings, including a Prague posting. Current Fantastic-derived URLs also use
`Fliff`. The adapter discarded that case. This is a generic transport-path bug,
not evidence of withdrawal, an ATS migration or a replacement tenant.

Native path casing is now retained separately from the existing lowercase
instance/site identity. Existing `global:fliff:<posting-id>` source IDs and
attachment rules do not change. Competing case variants are quarantined for
review rather than guessed. No case enumeration or fallback tenant discovery
was added.

Jobgether uses the public [Lever Postings API](https://github.com/lever/postings-api),
whose documented pagination is numeric `skip` / `limit`, not the authenticated
v1 API's cursor contract. Bounded requests of 100 records progressed normally
until the `skip=2400` response overlapped one previously returned native ID.
There were no within-page duplicates. The pages were not identical repeats;
the unsafe condition was cross-page overlap despite advancing offsets.
This is consistent with upstream ordering/catalog drift. It does not prove
which posting, if any, was skipped. The prior production error did not record
an offset, so its exact failing page cannot be reconstructed retrospectively.

Removing overlap detection or deduplicating moving pages would not prove
completeness. The guard remains. New errors retain reason, offset, returned
rows and overlap count. Repeated pages, missing IDs, conflicting duplicates
and the 1,000-page limit also fail closed. Identical duplicate slots *within*
one page can be deduplicated, while offsets advance by raw returned length.
Cross-page duplicates remain a failure even if most of that page is new.

All 17 current discovery scopes had valid syntax and returned HTTP 200 to
one-record availability probes using their advertised casing. This is not
proof that all 17 inventories are complete. A legacy provider URL with
`integrators.cz` as a site segment is rejected by the existing slug validator;
it is not an additional configured scope and was not probed or enabled.

Diagnostic responses, request hashes, scope history and exact overlap IDs are
stored under `/tmp/career-os-lever-triage-20261015/`, outside Git.

## Scope health

| State | Meaning | Inventory / lifecycle behavior |
|---|---|---|
| HEALTHY | Last authoritative inventory completed successfully | Existing complete-scope reconciliation rules apply |
| TRANSIENT_FAILURE | Unexpected HTTP/transport/probe failure | Preserve unseen rows; alert |
| KNOWN_UNAVAILABLE | Explicitly reviewed unavailable native endpoint | Quarantine inventory; bounded periodic availability probe |
| NEEDS_REVIEW | Uncertain pagination, conflicting site evidence, reappearance or unusable records | No inferred completeness; quarantine reviewed scopes, alert on unexpected failures |

An unreviewed 404 is alert-worthy NEEDS_REVIEW. It is never automatically
converted into a permanent exclusion or proof that postings were withdrawn.
Fliff is not configured as KNOWN_UNAVAILABLE.

The repository-owned `worker/app/config/lever_scope_health.json` currently
contains only `global/jobgether`, reviewed NEEDS_REVIEW. Its reason and evidence
are explicit, recheck interval is 168 hours, and review expires on 2026-11-14.
No other scope is excluded. To remove or renew an exclusion, review new evidence
and deliberately change this file; do not patch production rows.

The registry validates source, exact scope, reviewed/expiry timestamps and
bounded recheck interval. Invalid configuration fails before a run is inserted.
Expired review stays quarantined and becomes alert-worthy; expiry does not
silently resume uncertain ingestion.

Probe cadence uses existing `ingestion_runs.metadata.scope_health`, considering
at most 100 recent recorded runs. No database schema or persistent scheduler
is added. If no eligible probe is recorded, the next run performs one public
request with `limit=1, skip=0`. Otherwise it skips until recheck is due. Missing
older history can cause an extra bounded probe, not an unsafe inventory.
Unexpected probe failures retry the probe on the next normal run rather than
being hidden until next week. There is no automatic whole-source retry.

A 200 probe for NEEDS_REVIEW does not establish stable pagination or clear the
review. A KNOWN_UNAVAILABLE scope returning 200 is surfaced as NEEDS_REVIEW,
alert-worthy, and remains quarantined pending deliberate review. A probe never
writes jobs/sources, refreshes last_verified, reactivates postings, or closes
anything. Even an empty 200 probe is not a complete inventory.

## Runner outcomes and monitoring

| Outcome | Exit | Operator action |
|---|---:|---|
| SUCCESS | 0 | All required inventories complete |
| SUCCESS_WITH_KNOWN_GAPS | 0 | Operationally healthy; explicitly degraded coverage remains visible |
| PARTIAL | 2 | Unexpected failure, incomplete inventory, expired review, reappearance or anomaly; inspect |
| FAILED | 1 | No required source succeeded, unsafe outcome/configuration, or execution failure; inspect |
| SKIPPED_LOCKED | 75 | Another runner owns the shared lock; repeated/over-budget contention alerts |

`freshness-run-v1` remains the envelope. This is an additive status extension:
the repository shell validator already uses the shared EXIT_CODES registry.
Consumers outside the repository that enumerate statuses must accept the new
value before activation. Checking exit 0 alone no longer means full coverage.

JSON includes `coverage_degraded`, `alert_worthy`, `known_gaps`, exact per-scope
health and raw completeness counters. Raw failed-scope counts include reviewed
quarantines; `scopes_known_gaps` and `scopes_unexpected_uncomplete` distinguish
them. Scope-health actions distinguish inventory, skip and availability probe.
The shell prints an explicit COVERAGE GAP notice for expected degradation and
an ALERT notice for unexpected failures. Keep the JSON and source log paths in
the existing `logs/freshness/` directory. No email/webhook/service was installed.

Only a currently valid matching review plus valid probe/cadence evidence and
zero observed/deactivated/reactivated rows can excuse a quarantined scope.
Another error cannot be hidden behind an expected gap. All-review-gap runs
are explicitly degraded with zero complete inventories, never full success.
Zero/collapse alarms compare only runs with the same known-gap scope set; a
coverage change is explicitly marked non-comparable and seeds a new comparable
volume baseline after healthy execution. These volume baselines never authorize
reconciliation.

The shared OS lock, source advisory locks, deadlines, source transaction
boundaries and lifecycle scope ownership checks remain. `lifecycle.py` is
unchanged. Dedupe remains disabled and aggregator scheduling is unchanged.

## Freshness reporting

`scripts/freshness-report.sh` remains read-only. JSON and Markdown now include
Lever scope state, latest outcome/reason, last successful complete inventory,
consecutive failed *inventory* count, review expiry and authoritative gaps.
Scheduled skips/probes do not pretend to be successful inventories or increment
the failed-inventory counter. Historical completeness timestamps remain visible.

A reviewed suspended active scope cannot supply VERIFIED_ACTIVE evidence in
the reporting classification. With no other verified native source, its jobs
are NEEDS_VERIFICATION. Canonical `jobs.status` and source `is_active` are not
changed. Previously proven complete absence remains historical evidence;
aggregator observations never become employer-native verification.

## Expected next run and daily cron acceptance

After a separately authorized deployment:

- Fliff uses the exact existing advertised `Fliff` API path and normal full
  traversal. It may reconcile only if that inventory genuinely completes.
- Jobgether is not traversed or reconciled. Its first due one-record 200 probe
  keeps the reviewed gap explicit; subsequent normal runs skip until due.
- If the other 16 Lever scopes and the other LIGHT sources complete, expected
  group outcome is SUCCESS_WITH_KNOWN_GAPS, exit 0, coverage_degraded=true and
  alert_worthy=false. New failures still produce PARTIAL/FAILED.

Before enabling daily LIGHT, perform one separately authorized final production
validation and require:

1. Fliff native-case traversal succeeds; source IDs remain stable.
2. Jobgether has zero job/source writes, observations or deactivations, with a
   visible reviewed gap and probe/skip timing.
3. No failed/incomplete scope deactivates unseen rows; all reconciliation uses
   complete successful exact native scopes.
4. Company resolver acceptance, acquisition integrity, locks, deadlines and
   cleanup still pass.
5. Structured status is SUCCESS or SUCCESS_WITH_KNOWN_GAPS, with no unexpected
   failures, expired reviews, reappearance alerts or anomalies.
6. The operator accepts the named coverage gap, retains run artifacts and
   monitors PARTIAL/FAILED, overdue reviews, missing runs and stuck locks.

A reviewed gap need not block the other scopes indefinitely. Cron remains
NO-GO before that deployment/validation and monitoring acceptance; conditional
GO afterward. No cron entry is installed by this milestone. Existing documented
UTC schedules remain proposals. No separate ATS migration or new adapter is
indicated for Fliff. Jobgether needs an upstream pagination/completeness review,
not a bypass of its progress guard.
