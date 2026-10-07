# Independent Job Profile human reviewer

This is an offline research tool, not a Career OS production route. There is no
production database connection, processing command, authentication service or LLM.
The standalone HTML embeds the frozen 60-job inputs and optional references.
Generated artifacts, exports and human annotations belong outside Git.

## Open and review

The generated artifact for this milestone is:

`/tmp/career-os-human-reviewer-20261007/reviewer.html`

Copy that **one file** to the reviewer's computer and open it in a modern browser.
It works without an internet connection; no adjacent CSS/JS files are needed.
For example, with your actual SSH hostname:

```bash
scp careeros@YOUR_VPS:/tmp/career-os-human-reviewer-20261007/reviewer.html ./reviewer.html
```

1. Enter a reviewer name or stable ID.
2. Read the employer description and, if useful, original structured fields.
3. For each field choose Unlabeled, Known, Unknown, Not mentioned or Ambiguous.
   Unlabeled means not reviewed. Unknown means insufficient evidence. Not mentioned
   is an explicit reviewed absence. Ambiguous means competing interpretations.
4. Known fields open value controls. Add/remove items for locations, skills,
   technologies, language statements, experience constraints and separate offers.
5. Use optional evidence/notes for job family, management, requirements, language,
   experience, money and opportunity judgments. Never infer people management from
   Manager/Lead, requirements from tool mentions, or CEFR from fluent/advanced.
6. Click **REVIEW COMPLETE** deliberately. Unlabeled fields may remain, after an
   explicit confirmation; they remain unscored. Editing a completed review reopens
   it as partial. Use Mark incomplete when revisiting a decision.
7. Move with Previous/Next, the job dropdown or Ctrl+Alt+Left/Right. Search covers
   title/company/source; Incomplete only limits navigation. Statuses are untouched,
   partial and explicitly completed; the summary always counts all 60 jobs.

Predictions are below the human-label area in a closed panel. Provider/Fantastic
suggestions are separately labelled and closed. Both close again on navigation.
No control copies those references into a human value.

## Save and resume

Every edit autosaves a **human-only workspace** to localStorage, keyed by the frozen
cohort fingerprint. Same browser/origin resumes on refresh. Browser storage may be
blocked, cleared or full; the tool then displays an explicit warning. It is not a
backup and should not be the only copy.

- **Export completed JSONL**: only explicitly completed, validated reviews.
- **Export progress JSONL**: all 60 records, including untouched/partial records;
  every Known value must be valid. No predictions are exported.
- **Export CSV**: canonical states/values/evidence/item details and identity metadata.
  The UI and Python loader can both read this CSV.
- **Draft backup**: human-only workspace JSON, including unfinished invalid value
  controls. This preserves work even while a Known value is incomplete. It is a UI
  backup, not an evaluation input.
- **Import / resume**: choose a canonical JSONL/CSV or draft backup. IDs, input hashes,
  snapshot, cohort fingerprint and versions are checked. Import is atomic; unknown
  jobs, changed inputs, predictions, unsupported fields and duplicate reviews are
  rejected. Only jobs included in the file replace their existing local records,
  after confirmation. Duplicate reviewers need adjudication outside this tool.

Export regularly, especially before changing computers, clearing browser data or
closing an incognito session. Keep completed exports separately from draft backups.
Downloaded labels can be brought back to the VPS for offline evaluation.

If local-file storage is restricted, optionally serve a private directory locally:

```bash
python3 -m http.server 8765 --bind 127.0.0.1 \
  --directory /tmp/career-os-human-reviewer-20261007
```

Do not bind to 0.0.0.0 or expose a public port. For a remote VPS, use an existing SSH
connection with `-L 8765:127.0.0.1:8765`, then open
`http://127.0.0.1:8765/reviewer.html` on your computer. File and HTTP origins have
separate browser storage; use an exported backup to move between them. No server
is required for the recommended single-file workflow.

## Label contract

The shared `HumanReview` contract now accepts `job-profile-human-v1.2`, while v1.0
and v1.1 exports remain readable. This is a **human-label schema** extension only;
Job Profile schema/projector/parser/geography versions and DB schema are unchanged.
The original frozen templates are not rewritten.

Each record retains canonical ID, frozen profile input hash, snapshot,
`frozen_versions`, `cohort_hash`, reviewer ID, reviewed_at, explicit
`review_complete`, per-field states/values and reviewer notes. Each label supports
`evidence_text` and optional `item_details` (normalized value, original wording,
required/preferred/unknown). String collections remain canonical string arrays;
notes and item evidence do not silently become factual values.

Fields cover opportunity type (including other), normal vacancy, role/family/level,
people management, workplace/city/country, schedule/relationship/duration, explicit
base-pay presence, separate base offers, other components/benefits, mentions,
required technologies, required/preferred skills, languages and experience ranges.
The legacy all-compensation field is retained for import/evaluation compatibility;
the new UI labels base and other compensation separately.

Country values use ISO alpha-2. Career levels use entry/junior/mid/senior/staff/
principal; a leadership word does not force a career level. Avoid an invented role
or skill taxonomy: agree normalized names, retain original evidence and adjudicate
inconsistencies before comparing outputs.

Compensation offers keep separate amounts/currencies/units/scopes; unknown values
remain unknown. Never annualize or copy job geography onto an unlinked offer.
Other benefits may be literal employer wording or separate non-base monetary
components. Base compensation presence can be known even if amounts are unknown.

Languages retain language code, requirement flag, original wording, explicit CEFR,
comparator exact/at_least/at_most/range/unknown, and a CEFR upper bound for range.
CEFR has its own evidence state. Fluent wording with no explicit level must not
be assigned an invented CEFR. Experience needs explicit minimum/maximum years,
not a seniority inference.

## Build from the frozen cohort

Use the worker dependencies locally or a disposable container with the repository
mounted read-only. Do not deploy/restart the production worker just to build a UI.
No DATABASE_URL or production credentials are needed.

```bash
PYTHONPATH=worker python3 -m app.job_profile.reviewer build \
  --cohort /tmp/career-os-job-profile-v12-20261006/review60-frozen \
  --output /tmp/NEW_REVIEWER.html
```

The builder checks exactly 60 distinct IDs, input/template/prediction identity,
snapshot/hash/version agreement and all nine source families. It does not discover
new jobs, re-run extraction, copy predictions into labels or overwrite an existing
artifact. Preserve the manifest, source files and frozen predictions together.

If temporary artifacts are lost, regenerate with the existing V1.2 `review_freeze`
from the **same archived 1,000-job sample and original 200-job review population**,
then compare the 60 ordered IDs/input hashes against your saved manifest. Do not
substitute a new population or use newly fetched descriptions. If the archives or
saved identity are unavailable, stop and restore them rather than silently select
another cohort.

## Evaluate completed human reviews

Copy a completed JSONL export or canonical CSV back to a private analysis path:

```bash
PYTHONPATH=worker python3 -m app.job_profile.reviewer evaluate \
  --cohort /tmp/career-os-job-profile-v12-20261006/review60-frozen \
  --labels /tmp/career-os-completed-labels.jsonl \
  --output /tmp/career-os-human-metrics.json
```

A CSV export can replace the JSONL path. Draft backups cannot be evaluated. The
command uses only frozen predictions and manually exported labels. It validates
membership/hash/version, excludes incomplete reviews and scores only known or
not_mentioned gold. Any subset (e.g. 10, 30 or 60 completed jobs) works; untouched,
unknown and ambiguous fields do not create accuracy measurements.

Reports include per-field precision/recall/F1, decision coverage, abstention and
conflict rate, including opportunity, career level, workplace, technologies,
required technologies, skills, role/family, management, experience and separate
base/other compensation. Language presence, requirement flags and explicit CEFR
are scored separately so unknown subattributes are not mistaken for bad language
presence. The existing strict whole-language-statement metric remains marked
`legacy_language_statements`. CEFR comparisons use literal level/comparator/range;
there is no fuzzy interval or semantic partial credit.

Set-valued scores are micro item counts. Explicit predicted absence is a decision;
unknown/conflict/insufficient predictions abstain. Strict normalized/literal
matching is not semantic equivalence. Null denominators stay null. No thresholds
or accuracy claims may be invented from an unlabeled cohort. Independent labels,
normalization conventions and adjudication precede semantic extraction decisions.

## Verification boundaries

Python tests use synthetic jobs/labels and isolated PostgreSQL, never production.
JavaScript tests inspect fresh/partial/completed states, CSV/JSONL round trips,
frozen identity, old imports, CEFR comparators and draft recovery. Browser tests
use synthetic IDs and synthetic descriptions in a separate fixture; any downloaded
test labels are clearly synthetic and cannot pass the real cohort membership gate.
Chromium runs without a network and verifies editing, refresh, keyboard movement,
download/upload, closed prediction panels, mobile layout and zero external requests.
Actual cohort templates/labels remain untouched and unannotated.
