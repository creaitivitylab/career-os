# Company resolution idempotency

## Identity authority and shared path

`public.normalize_company_text(name)` is the authority already used by the
`companies_normalize_before_write` trigger and by company-based job blocking.
It calls `normalize_job_text`: coalesce null to empty, unaccent, lowercase,
replace every run of non-ASCII-alphanumeric characters with one space, trim.
It then removes one trailing legal suffix from the existing reviewed list
(`s r o`, `sro`, `a s`, `as`, `spol s r o`, `se`, `ltd`, `limited`, `llc`,
`inc`, `gmbh`) and trims again. This fix does not broaden that contract.

The previous Python helper only lowercased/collapsed whitespace and queried
`normalized_name` with that different key. The insert trigger replaced the
Python key with the database key, so subsequent lookups missed deterministically.
For example `H&M Group` was queried as `h&m group` but stored as `h m group`;
`Gen Digital Inc.` was queried as `gen digital inc.` but stored as `gen digital`.
The old unordered `LIMIT 1` was also unsuitable for stable duplicate resolution.

All seven direct ATS integrations, Fantastic and Jooble import
`get_or_create_company` through `app.ingestion`. That existing import remains
available and now re-exports the single implementation in
`app.company_resolution`. No source-specific resolver or company INSERT remains.
Canonical source-priority behavior, lifecycle, attachment and raw payload retention
are unchanged. Source display names stay in their native/provider payloads.

## Corrected resolver

`find_existing_company(cursor, name)` is a SELECT-only preview path. It asks
PostgreSQL to normalize the input and chooses the matching row with the earliest
`created_at`, then lowest UUID. It never locks, inserts or overwrites a name.
An empty normalized identity abstains; it is not a shared anonymous company.

`get_or_create_company` first uses that same lookup. Existing rows need no creator
lock. A miss obtains the key from PostgreSQL, requires a writable READ COMMITTED
transaction with autocommit disabled, obtains a namespaced transaction advisory
lock for that key, and performs a second lookup with a fresh statement snapshot.
Only if it still misses does it insert. Both concurrent creators then resolve the
same committed row. The normalized insert value comes from PostgreSQL, and the
existing trigger remains authoritative. Hash collisions merely serialize creators;
row equality is still exact normalized-key equality.

No helper commit, rollback, retry, canonical update or duplicate cleanup occurs.
The caller retains its existing transaction boundary. Creation locks release on
commit/rollback; exceptions propagate. REPEATABLE READ/SERIALIZABLE creation is
rejected because a wait cannot refresh those snapshots. Autocommit creation is
rejected because a transaction advisory lock would release before the insert.
Existing/preview resolution is read-only and can work in those modes.

Cooperating worker creators are protected without a new schema constraint.
Direct SQL or an old deployed writer can still create duplicates. Multi-company
transactions acquiring new-key locks in opposite order may deadlock; PostgreSQL
aborts one transaction rather than permit unsafe duplicate creation. Existing
source rollback guards remain responsible for preserving inventory on failure.
Do not add a blind ingestion retry or suppress transaction errors.

## Production audit and repair boundary

Read-only October 13 incident audit found 160 inserts for 17 names, 125 existing
job-company changes, and zero changes between different database-normalized
employer identities. The affected keys already contained historical duplicates:
601 company rows in total, with 213 referencing jobs. Company metadata
(`domain`, website/careers URLs, ATS identifiers/type, country) has no conflicting
values within these clusters. Distinct job source payloads remain distinct jobs.

All 17 inputs resolve in production SELECT-only preview to their existing oldest
keeper, and both old/new name variants for all 125 jobs resolve to that keeper.
The 146 legitimate new jobs and 309 new source rows retain their native identities
and canonical attachments. A company repair must not roll back or merge them.

Exact evidence, keeper/duplicate IDs, job maps, preimages and SQL proposals live
under `/tmp/career-os-company-resolution-20261013/`, outside Git:

- Incident-only: repoint 151 jobs (125 existing + 26 new) and delete the 160
  incident company duplicates. Nine incident company rows are unreferenced.
- Optional broader repair: repoint 210 jobs and delete 584 duplicate rows,
  leaving 17 keepers across the affected clusters. This additionally touches
  historical duplicates and needs explicit broader authorization.

Only `jobs.company_id` has a company FK, with `ON DELETE SET NULL`. Repoint and
verify zero references before deleting, never rely on that cascade to clean up.
The job normalization trigger fires on company updates; proposals verify every
other job field (including dedupe keys) is unchanged. No historical JSON company
references were found in the audited profile versions/merge history.

Proposals require a maintenance window with all writers quiescent, deployed fixed
code, durable preimage/mapping backup, exact hash/reference revalidation, guarded
transaction and postchecks. Drift aborts; never adapt a stale plan automatically.
No production repair, migration, deployment or ingestion was performed here.

## Future uniqueness proposal — no migration in this milestone

Production has a UUID primary key and a partial unique index on `lower(domain)`;
neither enforces normalized-name identity. There is no normalized-name index.
The audit found 1,342 duplicate normalized-key clusters with 23,850 excess rows
across the database, so an incident-only repair cannot enable global uniqueness.

After separately reviewing/cleaning all duplicate keys and possible same-name
employer collisions, a partial unique index on nonempty `normalized_name` would
protect against noncooperating writers and permit an exact conflict-aware insert.
Keep the database trigger/function as authority. A concurrent index build avoids
a long write lock but still scans the table, waits on transactions, can fail on
new conflicts and can leave an invalid index requiring deliberate cleanup.
Roll back an approved index rollout by dropping that index, not by restoring
duplicates. A data repair rollback needs the archived original rows/job links,
and another quiescent window; it is separate from index rollback.

No migration or index is added by this code change. The detailed future proposal
is `/tmp/career-os-company-resolution-20261013/uniqueness-proposal.md`.

## Verification and next gates

Tests use the exact audited PostgreSQL functions in a dedicated loopback test
database, including all 17 incident names, variants, real simultaneous misses,
rollback, display preservation, deterministic duplicate choice and all nine
source import paths. Tests explicitly show the limitation for uncooperative SQL.
The database-backed tests require `COMPANY_TEST_DATABASE_URL` naming
`career_os_company_test` on localhost; they refuse another database before DDL.

Commit and deploy only after separate authorization. Keep ingestion blocked until
the reviewed repair is separately authorized and verified. Then authorize one
LIGHT validation, followed by a measured HEAVY validation. Scheduling remains a
later operational action, not part of this fix.
