# HEAVY scope health, coverage and record quality

A scope review acknowledges an operational coverage gap. It does not approve an
RMK host, tenant, brand or locale, change source identity, or confer lifecycle
authority. The validated gate and exact RMK ownership contract remain separate.
No backfill, migration or startup action is needed.

## Four independent decisions

| Axis | Values / meaning |
| --- | --- |
| Scope health | HEALTHY, TRANSIENT_FAILURE, KNOWN_UNAVAILABLE, NEEDS_REVIEW; same vocabulary as Lever |
| Acquisition | Current success/failure is recorded separately from reviewed scope state |
| Coverage | AUTHORITATIVE_COMPLETE, NON_AUTHORITATIVE_EXPECTED, INCOMPLETE, UNAVAILABLE, UNKNOWN |
| Lifecycle | Eligible only with successful complete native inventory and intact identity coverage; exact native guards still run inside ingestion transactions |
| Record quality | Identity-preserving failures, identity-unknown failures, required-field failures, unclassified legacy failures, systemic flags, lossless signature counts |

Lifecycle completeness still comes from Workday unfiltered terminal traversal or
validated RMK exact host/native tenant/search brand traversal. Locale is reviewed
coverage, not posting identity. The health evaluator never turns `complete` on. The four reviewed RMK quarantines
are explicitly vetoed before lifecycle effects inside ingestion, including after
expiry or unexpected acquisition recovery; only reviewed policy changes lift that
veto. Malformed policy is rejected before opening a source transaction.
Policy expiry or success of an availability request never approves coverage.

`config/heavy_scope_health.json` is an explicit review manifest derived from both
measured HEAVY runs, reviewed October 20, 2026, expiring November 19. Common review
metadata is expanded into every entry by the loader. Coverage signatures contain
semantic modes, locale sets, exact tenant/brand and pruning presence, not record
counts. Changed signatures, new gaps or expired reviews alert until reviewed.
Previously authoritative scopes have no blanket permission to become filtered.
The 19 discovered/unapproved and seven excluded RMK scopes stay separate and
non-authoritative; only the exact reviewed discovery backlog is non-alerting.
A new scope or changed exclusion reason alerts. Nothing promotes it into the gate.

## Outcomes and alerts

* SUCCESS: clean, complete scope results, no gaps or unexpected quality failures.
* SUCCESS_WITH_KNOWN_GAPS (exit 0): reviewed coverage/unavailability only, visible in
  structured output and freshness reporting. Warnings remain visible.
* PARTIAL (exit 2): unexpected failure/coverage change, expired review, unknown
  identity, unclassified legacy failure, or required-field/parser defect.
* FAILED (exit 1): systemic acquisition/orchestration failure or invalid/unsafe
  result contract. A wholly reviewed unavailable set can remain a known gap.
* SKIPPED_LOCKED remains exit 75. No lock, retry or deadline settings change.

Known gaps never suppress a different error or blocked-host transition. The
MANN+HUMMEL review matches the exact approved-host -> blocked-host pair. Historical
metadata omitted that pair, so its replay cannot silently accept the generic
`unsafe_url` message. New diagnostics persist only the host pair, without query
strings or credentials. Reappearance/changed behavior remains alert-worthy.

Runtime over the existing expected three-hour budget already alerts; the hard
four-hour group deadline remains. Volume-collapse comparison stays conservative.
Healthy HEAVY baseline promotion accounts for successful-but-filtered acquisitions
without double-counting them as skipped scopes. Unexpected quality, timeouts or
anomalies still prevent promotion; atomic host promotion is unchanged.

## Reviewed RMK exceptions

| Scope | Reviewed state / coverage | Policy |
| --- | --- | --- |
| Dachser | NEEDS_REVIEW / expected non-authoritative | Keep >100 alternate fallback guard; no locale approval |
| Gates | NEEDS_REVIEW / expected non-authoritative | Small primary does not approve large alternate catalog |
| Porsche Holding CZ | NEEDS_REVIEW / expected non-authoritative | Alternate locale uncertainty remains |
| Nord Anglia | HEALTHY acquisition / independently reviewed filtered coverage | Historical 403 remains in failure history; no permanent quarantine or bypass |
| MANN+HUMMEL | NEEDS_REVIEW / unavailable approved route | Exact blocked destination remains unapproved |
| Kaufland views | HEALTHY / expected non-authoritative | Shared native tenant/posting across brands permits observations, not formal verification/absence from another brand |

The current alternate-catalog exception occurs before `discover_candidates`
returns. This change does not claim primary positive rows were acquired on those
failed discoveries and does not enable partial catalog returns.

## Record quality and transaction boundary

Adapters may attach a proved native source identity to a required-field error
only after normal native tenant/posting/request/canonical/site checks. Ingestion
marks that identity seen, records the failure and never writes a malformed job.
No URL slug, requisition ID, pathless stub, or arbitrary host becomes an identity.

Optional enrichment with proved identity is warning-only. The model supports it;
current missing-title/description errors are required-field defects, not optional
warnings. They remain alert-worthy and non-authoritative. Required failures with
no usable records flag a systemic failure. There is no arbitrary percentage
threshold or tolerance suppressing recurring parser defects.

Any unknown identity downgrades lifecycle completeness. A native HTTP 404 is a
protocol absence; an HTTP-200 empty shell without proved identity is uncertainty.
The latter now explicitly prevents RMK completeness. Lossless per-signature counts
and identity-known/unknown totals are persisted; diagnostic examples stay bounded.
Record-quality counters describe observations, while seen identities are distinct.

Critical completeness/seen checks run before lifecycle effects in the source
transaction. Runner validation happens after source commits and cannot roll back
those commits. Reporting is read-only; historical native completeness flags are
retained as history, while new quality evaluation may mark them ineligible under
this stricter policy. It does not rewrite historical evidence.

## Offline evidence and remaining blockers

Both measured runs remain PARTIAL under the proposed policy. In the latest run:

* Workday's eight diagnostic observations are pathless, identity-unknown stubs (two each in
  Accenture, GHR, KION and NXP). They cannot be deduplicated into eight distinct native postings without identities;
  none supports a fabricated native identity.
* RMK's counter 43 comprises 42 failed records plus one MANN acquisition failure.
  Samples show 27 missing required title/description and three missing identities;
  aggregate counters prove one additional missing identity. Eleven exact error
  signatures were not retained. Samples cannot prove those missing diagnoses.
* Seven additional HTTP-200 empty shells were outside the failed counter,
  including one in Vodafone's previously complete inventory. That inventory
  would be downgraded under the new quality policy; Správa železnic remains
  eligible. This does not change exact RMK ownership matching.
* Required-field failures recur across both runs; they are not reviewed away.

The archived 304 Kaufland brand-view differences remain non-authoritative (300
existing observations, four new sources). No company, canonical assignment or
source-key logic changes. The validated ownership helper is untouched.

Before unattended HEAVY, resolve/classify these identity/parser issues using
archived evidence or separately authorized bounded diagnostics. Deploy only with
separate authorization, then perform one separately authorized measured run.
Accept SUCCESS_WITH_KNOWN_GAPS only with no unresolved unexpected quality/coverage
failure, safe lifecycle decisions, durable runtime artifacts, released locks and
green health. A PARTIAL due these remaining blockers is not scheduler acceptance.

Three times weekly is an operational candidate, not approved scheduling. A future
`0 20 * * 0,2,4` UTC window offers 6h30m before 02:30 LIGHT: four-hour hard budget
plus 30-minute cleanup leaves two hours. Both measurements (2h04m56s and 2h30m27s)
fit, but two observations do not prove future runtime. LIGHT/Fantastic/Jooble cron
and all LIGHT retry/Lever policy code remain unchanged. No HEAVY schedule exists
or is installed by this milestone.
