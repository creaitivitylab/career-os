# Career OS — Codex project instructions

## Project

Career OS is a Czech job-market and career platform.

Repository root:
`/home/careeros/apps/career-os`

Main components:

- `web/` — Next.js frontend
- `worker/` — FastAPI/Python ingestion and data-processing service
- `scripts/` — operational ingestion scripts
- PostgreSQL database hosted in Supabase
- Docker Compose is used for local production-like services on this VPS

Do not rebuild the project from scratch.
Inspect the existing implementation before changing anything.
Preserve all existing working behavior.

## Current data architecture

The main canonical entities are:

- `companies`
- `jobs`
- `job_sources`
- `ingestion_runs`
- `duplicate_candidates`
- `job_merge_history`

A canonical job may have multiple source records in `job_sources`.

Current important sources:

- `jooble_direct`
- `fantastic_jobs_apify`
- `smartrecruiters_direct`
- `greenhouse_direct`
- `workable_direct`
- `ashby_direct`
- `lever_direct`
- `workday_direct`
- `successfactors_direct`

Existing SmartRecruiters, Greenhouse, Workable, Ashby, Lever, Workday and SuccessFactors RMK adapters should be used as implementation patterns.

Relevant files include:

- `worker/app/adapters/`
- `worker/app/ingestion.py`
- `worker/app/smartrecruiters_ingestion.py`
- `worker/app/greenhouse_ingestion.py`
- `worker/app/workable_ingestion.py`
- `worker/app/ashby_ingestion.py`
- `worker/app/adapters/lever.py`
- `worker/app/lever_ingestion.py`
- `worker/app/adapters/workday.py`
- `worker/app/workday_ingestion.py`
- `worker/app/adapters/successfactors.py`
- `worker/app/successfactors_ingestion.py`
- `worker/app/successfactors_scopes.py`
- `worker/app/config/successfactors_scopes.json`
- `worker/app/dedupe.py`
- `worker/app/merge.py`
- `worker/app/main.py`

## Source priority

Current merge/source preference is conceptually:

1. direct ATS sources
2. `fantastic_jobs_apify`
3. `jooble_direct`

Direct ATS sources currently have priority 300.
Fantastic has priority 200.
Jooble has priority 100.

When adding another direct ATS, follow the existing source-priority pattern unless there is a concrete reason not to.

## Direct ATS ingestion rules

When implementing a new ATS adapter:

1. Inspect existing direct ATS implementations first.
2. Prefer the ATS's public/direct API over scraping whenever possible.
3. Discover tenants/boards from existing Fantastic records where practical.
4. Use a stable ATS-native posting identifier for `source_job_id`.
5. Filter to jobs genuinely applicable to Czechia.
6. Preserve the complete source response in `raw_payload`.
7. Store canonical source/apply URLs where available.
8. Update existing source records idempotently.
9. Never create duplicate source rows for repeated ingestion.
10. Prefer deterministic matching to existing canonical jobs.

Deterministic attachment examples include:

- identical ATS posting ID embedded in Fantastic URL
- exact stable source identifier
- another unambiguous source-specific mapping

Do NOT use fuzzy title matching as an automatic attachment mechanism unless the existing implementation explicitly defines a conservative, reviewed rule.

If deterministic attachment is unavailable, create a separate canonical job and allow the dedupe engine to identify a candidate later.

Workday discovers host/tenant/case-sensitive career-site scopes from Fantastic Workday URLs.
Its native posting ID is not interchangeable with `jobReqId`; use the native posting ID for source identity.
For Czech candidate discovery, OR all Czech country/city values within each geography facet, query different facet dimensions separately, and union normalized job paths before fetching details.
Country-labeled facets may omit city buckets or return no jobs, so retain all relevant dimensions.
Final eligibility still requires primary country CZ or a clearly Czech additional location.
Without a usable Czech geography facet, traverse listings conservatively and prune only explicit foreign single locations.
Use 20-record pages and actual returned lengths; ignore misleading subsequent totals and retain stalled-page/page-limit protection.
Accept an out-of-range page reset only after the advertised boundary is independently confirmed by the exact short tail; count pathless listing stubs without inventing URLs.

SuccessFactors ingestion supports Recruiting Marketing (RMK) only. Migrated SAP/SmartRecruiters and Dream.jobs records are excluded.
Discover native tenant/brand configuration from Fantastic-derived public sites; source identity is `{sso_company_id}:{rmk_posting_id}`. Internal requisition/locale IDs are separate and must never substitute for the posting ID.
Exact native identity must match exactly one Fantastic canonical job before attachment. Multiple canonical matches remain ambiguous even when a slug matches; create a separate canonical job and never auto-merge.

Unattended SuccessFactors execution must use `{"validated_scopes":true}` and the repository-owned validated scope gate. New scopes require deliberate validation/review; never enable unrestricted automatic discovery in the runner.
Keep native tenant, brand and public host checks. Preserve the preferred locale as the primary view; for non-English primary views, union at most one English locale publicly advertised by the same scope's posting layout or verified brand homepage. Do not enumerate all languages. Existing reviewed locale unions remain limited to their approved members.
Union all recognized Czech country/city/location facet values and deduplicate native posting identities across facets/locales before details. Kutná Hora is a recognized bare Czech geography value. English language, generic Remote/Europe/EMEA and URL slugs are not Czech eligibility: verify native detail country/address/primary/additional location evidence.
Without usable geography facets, retain conservative listing fallback. An automatically added alternate locale's unfiltered catalog must have a native count of at most 100; larger or uncounted catalogs require review and must report unresolved discovery rather than silently truncate.

Preserve both table and tile handlers, board page sizes, locale/filter state, repeated-page detection and maximum-page protection.
Foundever listing discovery was recovered and verified. A changing advertised count may terminate only when the native terminal range and current count agree with every accumulated unique identity and an independent next-page request confirms an empty boundary with the same count. Report malformed detail records separately; do not manufacture identities.
Erste remains unresolved and excluded. Keep all other excluded/unsupported scopes visible in gate diagnostics; do not bypass access controls or re-fetch historical URLs as current discovery.

## Dedupe and merge safety

Do NOT automatically merge duplicate candidates.

The dedupe system may generate candidates and safe candidates, but merging must remain a deliberate action unless the user explicitly requests otherwise.

Do not weaken existing dedupe thresholds without explicit instruction.

When adding a direct ATS source, add it to the generic direct-source sets in `worker/app/dedupe.py`.

When adding a direct ATS source, add the appropriate source priority in `worker/app/merge.py`.

Direct ATS jobs may legitimately overlap with:

- Fantastic
- Jooble
- another direct ATS

Preserve multiple source records on one canonical job when they genuinely represent the same posting.

## Production and cost safety

Do NOT run paid or limited external ingestion merely to test code unless explicitly requested.

In particular:

- Fantastic / Apify may incur monetary cost.
- Jooble API requests are limited.
- Do not trigger broad/full production ingestion automatically.

For a new ATS implementation:

1. implement code
2. run local/static/unit checks
3. test against a small or single tenant only if the user requested execution
4. report results
5. let the user decide when to run a full production ingestion

Do not make destructive database changes.
Do not execute destructive SQL or migrations without explicit approval.

Do not auto-merge duplicate candidates.

## Secrets

Never print, expose, commit, or modify secrets unnecessarily.

Do not commit:

- `.env`
- API keys
- database credentials
- tokens
- authentication material

Existing secrets must remain outside Git.

## Development style

Prefer extending existing architecture instead of introducing parallel systems.

Keep changes focused on the requested milestone.

Avoid unrelated refactors.

Use existing helper functions and conventions where appropriate.

Make ingestion idempotent.

Handle partial external-source failures gracefully and report per-tenant/per-board errors.

Keep code readable and explicit.

## Verification

Before declaring an implementation complete, run the relevant checks available in the repository.

For Python changes, at minimum consider:

- `python3 -m py_compile` for changed modules
- existing automated tests
- relevant imports
- Docker worker build if needed

For broader changes also inspect:

- `git diff`
- accidental secrets
- unintended unrelated modifications

Do not perform a production bulk ingestion merely as a verification step.

## Git

Do not push automatically unless explicitly requested.

Do not rewrite history.

Do not use destructive Git commands.

Do not discard user changes.

Before editing, inspect the working tree and account for existing modifications.

## Completion report

At the end of each implementation task, report concisely:

1. what was implemented
2. files changed
3. important design decisions
4. checks/tests executed and their results
5. anything not verified
6. any risks or unresolved questions
7. exact recommended next command(s), if applicable

Do not claim a production ingestion succeeded unless it was actually executed and verified.
