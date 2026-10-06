# Job Profile V1 Phase 2A — persistence and independent evaluation

No production migration or processing was executed in this milestone. No LLM
integration is included. The FastAPI app and ingestion/merge Python modules are
unchanged. The operational interface is a local command, not a public API.

## Production schema audit

The 2026-10-06 audit used explicit read-only transactions against PostgreSQL 17.6.
`jobs.id` is UUID. `job_sources.job_id` cascades on canonical deletion, and
`(source_name, source_job_id)` is unique. `job_sources_job_id_idx` supports
bounded input loading. Duplicate candidate FKs cascade; merge history already
retains removed UUIDs without a live FK. Existing merge code locks canonical
jobs, moves sources, deletes the duplicate, then refreshes keeper activity.

Acquisition tables have RLS enabled with no policies. Worker DATABASE_URL uses
the `postgres` owner with BYPASSRLS. Supabase grants broad acquisition-table
privileges to anon/authenticated/service_role, so a new migration must revoke
inherited default grants rather than assuming RLS alone is sufficient. Existing
grants/policies are not changed by this milestone.

## Final migration

`worker/migrations/20261006_job_profiles.sql` replaces the review-only Phase 1
proposal as the candidate for explicit operator approval. It is transactional,
does not enroll or generate any profiles, and is never applied at app startup.
Run once; an existing conflicting schema causes failure rather than silent reuse.

`job_profile_versions` holds immutable JSONB profile/evidence, projected input
snapshot, semantic/description/source-selection/input hashes and queryable
schema/projector/parser/taxonomy versions. A per-job/input unique key prevents
duplicate versions. Database triggers reject UPDATE, DELETE and TRUNCATE, even
by the worker owner. Operational administrators can change DDL, so this is not
an adversarial archive guarantee. Evidence is duplicated alongside the profile
with a consistency constraint for simple downstream queries.

`job_profile_current` has one live canonical FK, composite version ownership,
target hashes, state, dirty revision, UUID lease token, expiry, attempts,
retry timing, last success/error and update timestamp. A work index supports
pending/stale/failed/expired-lease selection. Hash/JSON/state/lease constraints
reject malformed publication. No acquisition indexes are added.

Historical version `job_id` is explicitly an original historical identity,
not a live canonical pointer. On deletion the current row cascades; immutable
history survives. After a merge, moved sources invalidate the keeper. If only
the removed job was enrolled, the keeper inherits pending work, never its old
profile. Retired history stays attributable to the original UUID and may be
cross-referenced with job_merge_history. No current pointer can refer to another
job's version. No merge priority/behavior is changed and no merge is executed
in production. Queries for current product data must use job_profile_current.

## Security assumptions

Both new tables enable RLS and intentionally have no public policies. PUBLIC,
anon and authenticated receive no table access or function execution. Explicit
revocations also cover Supabase default grants. This intentionally grants no
browser read access to descriptions/evidence/review data yet.

The existing postgres worker owns the tables. service_role, if present, receives
SELECT/INSERT on versions and SELECT/INSERT/UPDATE on current; its verified
BYPASSRLS supplies worker access. A future dedicated non-bypass worker role
would require deliberately reviewed policies/grants; it cannot work merely by
inheriting these assumptions. SECURITY DEFINER source invalidation uses a fixed
search_path, qualified table names, and no externally supplied dynamic SQL.

Snapshots contain selected projected evidence, cleaned text and deterministic
text-layer outputs, not whole raw pages/session payloads. Descriptions may still
contain personal information; keep private and agree retention/export policies
before substantial deployment. last_error stores exception class/code only.

## Processing and invalidation

Only explicitly enrolled jobs are processed. Source INSERT/DELETE, identity,
attachment, activity and payload changes mark enrolled jobs stale; seen/verified
timestamp-only changes do not. Payload-only session/lifecycle changes can mark
dirty, but the cheap semantic hash pass then publishes a no-op, not a new
version or parser execution. A source transfer may inherit enrollment as above.

Each process call claims at most 25 jobs. `FOR UPDATE SKIP LOCKED` provides
exclusive claims; the claim commits before computation. Tokens, expiry and
dirty revisions fence crashed or superseded workers. Expired leases are
reclaimable; three attempts cap automatic retries. Transient failure retry
delays start at 15 seconds and double; exhausted work requires explicit retry.
Failures preserve the last successful version. No cron/service is installed.

Publication locks the canonical parent before current, validates lease/revision
and recomputes cheap input hashes before its atomic version insert/pointer
update. The source invalidation trigger serializes changes with publication.
Changed inputs never let old output replace a newer target. Database outages
leave expiring leases recoverable and report sanitized failure codes.

Native projection and description cleanup run to compute fingerprints. Unchanged
inputs/versions perform no generation. Metadata-only changes reuse the stored
text-layer cache. A selected-description, source identity, parser,
dictionary or cleaner change invalidates that cache; activity refreshes cached
evidence without reparsing. Title/native facts rebuild
conservatively. Source/schema/taxonomy version changes still invalidate the
profile. The selected source belongs to the cache key, so identical text from
different sources cannot borrow provenance. No heavy rules run in ingestion.

Version bumps must be detected by explicit bounded enqueue calls; no whole-
database scan runs automatically. This is intentional for the current staged
rollout. Polling drains only enrolled dirty/pending work.

## Bounded operational commands (after migration authorization)

Run inside the worker environment with its existing private DATABASE_URL, or
set JOB_PROFILE_DATABASE_URL privately. Never put credentials in command logs.

```bash
python -m app.job_profile.cli preview --job-id JOB_UUID
python -m app.job_profile.cli enqueue --job-id JOB_UUID
python -m app.job_profile.cli enqueue --job-id JOB_UUID --apply
python -m app.job_profile.cli process --limit 1 --apply
python -m app.job_profile.cli enqueue --job-id JOB_UUID --retry --apply
```

Repeat --job-id for a batch of at most 25 unique UUIDs. Enqueue without --apply
is read-only preview. Process requires --apply, has a 1–25 claim limit, and at
most 10 explicitly requested polls with --polls; it never enrolls all jobs.
Preview works before profile schema installation. Lease default is 120 seconds;
slow future semantic processing would need an explicit heartbeat design.

## Human evaluation

HumanReview JSONL uses `job-profile-human-v1.0`, job ID, input hash, snapshot,
reviewer ID/time and explicit per-field state. Independent human labels are in
a separate file from baseline predictions and provider suggestions. The 200-job
Phase 1 review set is reused; no new semantic labels are fabricated.

States: unlabeled, known, not_mentioned, unknown and conflict. known requires
a value; not_mentioned represents reviewed absence. Unknown/conflicted labels
are excluded from scoring until adjudicated. Reviewed labels need reviewer/time;
duplicate reviews must be adjudicated, not overwritten. A hash mismatch rejects
comparison with a different content/prediction version.

Scalar fields use reviewed normalized strings/enums. Technologies use normalized
name arrays; languages use language/requirement/explicit-CEFR objects;
experience uses numeric min/max; compensation uses separate typed offers.
Do not annualize, invent CEFR or combine salary offers while labeling. Reviewer
notes explain ambiguous decisions. Label templates start entirely unlabeled.

```bash
python -m app.job_profile.human_evaluation template \
  --review /tmp/career-os-job-profile-v1-20261005/evaluation/review.jsonl \
  --profiles /tmp/career-os-job-profile-v1-20261005/evaluation/profiles.jsonl \
  --snapshot 2026-10-05T13:16:08.050226Z --output /tmp/human-labels.jsonl
python -m app.job_profile.human_evaluation metrics \
  --labels /tmp/human-labels.jsonl \
  --profiles /tmp/career-os-job-profile-v1-20261005/evaluation/profiles.jsonl \
  --output /tmp/human-metrics.json
```

Metrics are micro precision/recall of exact normalized facts/items, decision
coverage, abstention and conflict rate per independently labeled field. Explicit
predicted absence is a decision, not abstention. Precision/recall are null when
their denominators are zero; unlabeled fields get no accuracy claim. This is
strict matching, not a semantic similarity or partial-credit metric. Dictionary
alias/interval mistakes must be reviewed rather than hidden with fuzzy scoring.

## Validation boundaries

Tests run against an isolated PostgreSQL 17 database, without production
credentials. A guarded acquisition fixture reproduces relevant audited columns,
FKs/indexes and Supabase default-grant behavior. It does not emulate every
Supabase extension/pooler feature or the existing normalization trigger.
Complete tests exercise the real merge function locally, source-trigger
invalidation, constraints, immutable versions, lease races/expiry, retries,
incremental cache use and actual RLS/service-role behavior.

Production schema application is a separate explicit approval step. After
approval, first verify catalog permissions/triggers, then authorize one UUID
enqueue/process and no-op rerun before any larger enrollment. Applying this
migration does not authorize processing or a recurring scheduler.
