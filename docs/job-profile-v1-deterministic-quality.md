# Job Profile V1.2 — deterministic quality and evaluation freeze

This milestone changes only the offline/native projection subsystem and independent
human-review utilities. No deployment, migration, enrollment, generation or profile
publication is performed in production. No external model or geocoder is used.

## Contract and evidence

The additive JSON contract remains readable alongside V1.0/V1.1 immutable profiles.
Missing new properties default to unknown/null. Acquisition data is unchanged.

- `metadata.opportunity_type`: evidence-backed vacancy, talent_pool,
  internship_program, event or hackathon; abstention is an unknown Fact.
- `metadata.is_normal_vacancy`: derived from the opportunity signal, sharing its
  original evidence. It is product metadata, never an acquisition activity rule.
- `compensation.monetary_benefits`: literal monetary benefit statements with spans;
  these are separate from `offers`, not normalized base-pay amounts.
- `CompensationOffer.original_location_labels`: original labels from a recognized
  native offer-specific location. Unresolved range titles remain `original_text`.
- `LanguageRequirement.cefr_comparator`: at_least, exact or unknown (legacy records
  may have null). Keep `cefr_or_higher` for compatibility. Bare B2 is not an exact
  equality claim; professional/fluent/advanced never manufacture a CEFR level.

## Narrow rules

Explicit talent-pool/community/open-application titles qualify. The observed Czech
CV invitation additionally needs text stating that no matching vacancy exists.
A year-labelled hackathon and explicit recruiting-event/program titles qualify;
organizer/manager/engineer role titles abstain rather than becoming the event.
Ordinary internships, generic join-our-team and unmarked evergreen ads remain
unknown without affirmative evidence. A limited explicit recruiting-clause rule
can signal vacancy. This is not a classifier for every stored job.

Pension/contribution and reviewed allowance contexts are excluded from textual
salary offers. Benefit statements retain original wording and evidence, with
bounded context for native descriptions collapsed to one long line. A mixed line
containing salary and benefits still abstains conservatively; V1.2 does not broaden
salary recognition. Missing amounts are never zero; no salary estimation or
period conversion is introduced.

Greenhouse native `pay_input_ranges` own their range titles; the inspected HTML
renders each country title with its corresponding range. Exact recognized country
or reviewed city titles supply applicability for that range only. Arbitrary titles
and geographic substrings remain unresolved. Never copy canonical geography or
currency-implied geography into an offer. Preserve separate currencies/ranges and
unknown periods. General ISO country data is reused without modifying geography.

CEFR bounds include B2+, B2 or above, B2 level or above, minimum B2, at least B2 and
C1 or higher. Qualifiers are bound within each language's clause; explicit wording
and spans remain. Requirements/preference and non-CEFR proficiency are unchanged.

## Versions and incremental behavior

- schema: `job-profile-v1.2`
- native projector: `native-v1.2`
- deterministic parser: `deterministic-v1.1`
- unchanged: geography-v1.1, description-v1.0, technology-v1.0, unmapped-v1.0

Schema additions require no database migration: existing persistence stores a
versioned JSONB contract. Projector changes invalidate native projection; parser
changes invalidate title/text rules. Unresolved native offer-label changes are also part of the semantic metadata hash.
Metadata-only input changes reuse cached
monetary-benefit and opportunity-description evidence. Same hashes/versions remain
a no-op. Versions are discovered by a future explicitly authorized bounded enqueue;
this task does not mark production rows stale. Historical versions are untouched.

## Independent human evaluation

`job-profile-human-v1.1` extends the existing human-label format, retaining v1.0
readability. Fields cover opportunity/normal vacancy, role/family/level/people
management, workplace/city/country, schedule/relationship/duration, separate base
and other compensation/benefits, technologies mentioned versus required,
languages with explicit CEFR/comparator, experience ranges and notes.

States: unlabeled, known, not_mentioned, unknown, ambiguous and conflict. Unknown
or ambiguous gold is not scored. Known requires a value and completed reviews need
reviewer identity/time. Country labels use ISO alpha-2; cities and set fields use
arrays. Base offers remain separate; literal benefit statements or non-base offer
objects can label other compensation. Required technologies deliberately abstain
in the deterministic baseline; technology mentions are not requirements.

`review_freeze` selects 60 from the existing 200-job population, with source quotas
and diversity proxies. Up to four signaled non-standard examples from that same
population are reserved. Sampling uses predictions, but labels are never filled
from predictions or provider AI. Selection IDs, source/input hashes, snapshot and
version metadata form a reproducible manifest. This is an archived active/recent
snapshot, not a live market-size estimate.

Review HTML shows original text/structured fields first. Predictions and provider
suggestions are separate closed panels. JSONL labels and editable CSV contain only
blank labels. Predictions and review inputs are separate files. CSV categorical
values are plain strings; boolean and collection values are JSON (`false`,
`["Python"]`, separate offer objects). Set reviewer ID/time when completing labels.
The original 200-job population is read-only.

```bash
python -m app.job_profile.review_freeze \
  --sample /tmp/career-os-extraction-v1-20261005/sample.json \
  --population /tmp/career-os-job-profile-v1-20261005/evaluation/review.jsonl \
  --profiles /tmp/career-os-job-profile-v12-20261006/after-1000.jsonl \
  --output /tmp/NEW_EMPTY_REVIEW_DIRECTORY

python -m app.job_profile.human_evaluation metrics \
  --labels /tmp/COMPLETED_LABELS.jsonl \
  --profiles /tmp/FROZEN_PREDICTIONS.jsonl --output /tmp/human-metrics.json
```

The metrics command also accepts the generated CSV. Scoring uses exact normalized
facts/items: precision, recall, F1, decision coverage, abstention and conflict rate.
Explicit predicted absence is a decision. Strict matching includes CEFR comparator
and separate offer scope; there is no semantic/fuzzy credit. Empty denominators
return null. Unlabeled inputs cannot supply accuracy or passing thresholds.
Labels cannot silently attach to different input hashes; changed versions need a
deliberately reviewed evaluation comparison. Human labels and adjudication must
precede selective semantic extraction and factual precision acceptance criteria.
