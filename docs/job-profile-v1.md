# Job Profile V1 — Phase 1

This is an offline native/deterministic foundation. It adds no ingestion hooks,
HTTP endpoint, persistence writer, queue, LLM call or production migration.

## Contract

`worker/app/job_profile/models.py` defines a Pydantic contract with forbidden
extra keys and typed domain values. Each fact has a state, value and evidence
references. States are `known`, `unknown`, `not_mentioned`, `conflict` and
`insufficient_content`. Known facts need a nonempty value and evidence. Unknown
is distinct from false; an empty array never establishes confirmed absence.

Groups are metadata, role, workplace, employment, compensation, requirements,
content, career_context and evidence. A generated JSON Schema can be used by
future persistence/API consumers. Job-family/specialization slots are unmapped;
Phase 1 does not introduce a role taxonomy.

Career level, title leadership markers, management track and people management
are independent. Manager/Lead/Head/Director/vedoucí markers do not establish
people management or years of experience.

Locations retain primary/additional/unspecified kinds and separate city, region,
country and original-label evidence. Employment separates schedule,
relationship and duration. Compensation contains whole offers, including
amount bounds, currency, period, gross/net, component, location applicability,
original text and explicitness. No cross-offer coalescing or annualization occurs.

## Evidence and source projection

All nine source projectors use the stored payload shapes documented by the
2026-10-05 read-only extraction audit. Input is a canonical job with attached
`job_sources` records; canonical country/workplace defaults are not native facts.
Older empty payloads remain unknown. Unknown sources emit no manufactured facts.

Evidence records include source name/native source identity, extraction method,
native field path or text span, input hash, observed time, explicitness,
validation state and confidence band. Text spans use cleaned-description
coordinates; title-rule spans carry a native title path. Compound native
descriptions reference their component container. Confidence bands are rule
evidence assessments, not calibrated probabilities.

Fantastic structured provider fields use `provider_structured`. Its selected
`ai_*` outputs use `provider_suggestion`, low confidence and unvalidated status;
they are retained in evidence, not promoted to known facts or used as labels.
Geocoder-derived fields are not treated as employer-native geography. Jooble
provider IDs are not ATS posting IDs. Workday posting IDs and RMK posting IDs
remain distinct from requisition and locale IDs.
Provider document-language suggestions are not applicant language requirements;
provider experience bands are not global career levels. Dedicated Jooble salary
strings may fill a missing offer, with unknown interval/origin explicitly retained.

Field-specific selection lives in `selection.py`, independently of canonical
ingestion priority. Native addresses/explicit employment fields precede native
label/body parsers and provider metadata. Title-marker evidence can precede
broad native seniority labels. Compensation is selected as whole offer sets;
competing offers remain in the evidence ledger. Equal-quality scalar conflicts
remain unresolved instead of being broken by source name. Inactive evidence
loses ties to active evidence. Location records preserve competing sources.

## Description and rules

Selection considers obvious truncation, short text, shells and completeness
before native/provider preference. A substantial Fantastic body can beat a
native snippet. Jooble text is always completeness-unverified. No descriptions
from different sources are concatenated. Sections from one native posting may
be assembled; this is not cross-source concatenation.

Cleanup uses the standard-library HTML parser, bounded entity decoding,
script/style/navigation/form suppression, normalized whitespace, preserved
headings/bullets/line breaks and extraction-relevant punctuation. Completeness
is a heuristic; 500 chars/60 words is not a proof of full content.

Deterministic rules include explicit English/Czech title markers; a small Czech
place alias list; native workplace and employment labels; boundary-safe tools;
explicit language/CEFR clauses; explicit experience years; conservative
currency+interval salary forms; and exact common English/Czech section headings.
Technology output is a mention, never a required skill. Fluent/advanced/
professional never imply a CEFR grade. Compensation parser rejects benefit,
bonus, revenue, voucher, budget and reward lines and retains unknown units.
Unmatched or ambiguous prose stays unknown. Content arrays contain native
section blocks, not semantically rewritten duties or atomic requirements.

## Hashing and asynchronous processing proposal

Version identifiers cover schema, projector, cleaner, parser, dictionary and
future taxonomy. Semantic metadata hashes use projected facts, not arbitrary
raw payloads. Description hashes use cleaned text. Source-input selection hashes
also track attached identities, activity and alternative description hashes.
Lifecycle/seen timestamps, run IDs, CSRF/session fields and irrelevant HTML
attributes do not trigger extraction. `reprocessing_layers` returns the native,
text, selection and/or profile layers that need rebuilding.

The pure baseline builder recomputes a profile when explicitly called; the
planner provides a cache contract, not a deployed scheduler/cache. Same hashes
and versions need no extraction. A metadata-only change avoids description
parsers; text/cleaner/dictionary/parser changes invalidate text-derived output.
Title-rule parser changes also require native/title rebuilding. Changes in
alternative source content/activity require selection, not necessarily another
semantic text pass.

Future processing should happen after ingestion commits. A small DB-backed
pending/stale scan with `FOR UPDATE SKIP LOCKED`, a bounded worker and expiring
leases fits this VPS. Claim and finalize in short transactions; process outside
ingestion transactions. Check target hashes before publishing to prevent stale
output overwriting a newer input. Failures retain the previous current profile.
No such production process is started by Phase 1.

## Persistence proposal — not applied

`job-profile-v1-schema-proposal.sql` is a review-only proposal outside migration
execution paths. A current processing row per canonical job points to an immutable
versioned JSONB profile. Relational hashes/state/version/timestamps support
incremental work; JSONB stores the evolving typed contract and evidence without
prematurely normalizing every nested requirement into tables.

Version history includes selected native-field and cleaned-text snapshots, so a
mutable job_sources row cannot destroy evidence reproducibility. Store the small
projection inputs, not full native pages or session/contact payloads. History
keeps the original canonical ID after a merge/delete; current rows cascade.
Canonical merges must mark the surviving job stale and retire the removed
current pointer in Phase 2. Source updates and model/schema/parser versions
need explicit invalidation. Define retention/privacy policy before storing
full-text snapshots in production.

The SQL requires review of RLS/grants/service access and workload before approval.
It intentionally grants no web-client access. No migrations or indexes were
executed. Hash lengths and composite version ownership are constrained, and
processing states are pending/processing/completed/failed/stale.

## Offline evaluation

Run against an archived audit snapshot, not production:

```bash
PYTHONPATH=worker python3 -m app.job_profile.evaluate \
  --sample /tmp/career-os-extraction-v1-20261005/sample.json \
  --output /tmp/career-os-job-profile-v1-20261005/evaluation
```

Use the worker container when host dependencies differ. The generator produces
profiles.jsonl, baseline.json, coverage.csv and 200-job review.csv/review.jsonl/
review.html artifacts. Review is active/recent relative to the input snapshot;
it is not a fresh production query. Seeded source quotas and diversity scoring
cover language, title family/level, location, workplace, employer catalog size
and compensation presence. Employer catalog size is not employee headcount.

Coverage is not accuracy. Winning routes are reported separately for employer
native, deterministic parser and provider structured evidence. A fifth
provider-structured category is necessary: Fantastic structured addresses must
not be relabelled employer-native. Suggestion-only fields remain unresolved;
conflicts and unknown/not-mentioned/insufficient states are also reported.

Manual labels remain blank. Predictions and provider suggestions are separately
labelled references and collapsed in the HTML review. Use independent human
labels to measure precision/recall, source-specific errors and abstentions before
production processing or a selective semantic extraction pilot.

## Remaining boundaries

No inferred salary, geographic eligibility, people management, complete job
taxonomy, required/preferred semantic skills, certification normalization or
full educational-alternative extraction is attempted. Dictionary aliases and
salary/language/experience rules are conservative and incomplete. Native
metadata labels can still be misleading. Description hashes measure the selected
cleaned document, not proof of employer intent or live-market eligibility.

All jobs can have unresolved semantic fields; that is not evidence that every
job needs an LLM. Improve deterministic/native coverage and label the evaluation
set before deciding which unresolved fields justify a semantic extraction call.
