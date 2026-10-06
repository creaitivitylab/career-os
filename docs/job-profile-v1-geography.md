# Job Profile V1.1 geography quality

This changes pure projection only. It adds no geocoding requests, ingestion,
profile enrollment, persistence, scheduler, schema migration or LLM calls.
The SQL tables already support versioned JSONB; no database change is needed.

## Semantics and provenance

`workplace.locations` contains structured places with at least one established
country, city or administrative region. Country is ISO alpha-2, with a separate
normalized country name. City means municipality/locality. Region means an
administrative subdivision, including an explicitly identified district.
Primary/additional kinds remain source-specific. Delimited multi-place layouts
without a primary designation use `unspecified`, never a fabricated primary.

`workplace.raw_location_labels` retains unrecognized labels, wrong-type values,
workplace/broad-area labels and quarantined non-place records, with the original
field semantics, kind, reason and evidence references. Unknown raw semantics do
not make a normalized location. `Evidence.raw_value` retains the original field
wording alongside its normalized assertion; source/path/hash/observation and
confidence are retained. Country inferred from a reviewed Czech city alias is
explicitly distinguished from a country actually stated in the input.

Navigation, marketplace/domain and prose records are quarantined as a whole.
This covers the observed Ashby secondary record labelled “Group career pages”
whose attached country incorrectly says Zambia, as well as domain-like job-board
labels. Neither label is a company-specific exception. The associated values
remain rejected raw evidence. A remote label with an explicit native country
is different: its country survives, while the remote label stays separate.

## Audited source paths

Paths below are relative to `raw_payload`; numeric indexes represent source
arrays, not a global canonical-primary designation.

| Source | Structured geography | Weaker labels |
|---|---|---|
| SmartRecruiters | `location.country/city/region` | `location.fullLocation` |
| Greenhouse | none | `location.name`, `offices[i].location` |
| Workable | `country/city/state`, `locations[i].countryCode/city/region` | city display values |
| Ashby | `address.postalAddress.addressCountry/addressLocality/addressRegion`, corresponding secondary addresses | `location`, `secondaryLocations[i].location` |
| Lever | `country` | `categories.location/allLocations[i]` |
| Workday | `[detail.]jobPostingInfo.country.descriptor` | same container's `location/additionalLocations[i]` |
| SuccessFactors | `detail.locations[i].country/native.addressLocality/native.addressRegion`; fallback `detail.native_fields.country/city/state` | `detail.locations[i].text`, fallback city display |
| Fantastic | `locations[i].address` or `Address`, then `addressCountry/addressLocality/addressRegion` | `locations_alt[i]` |
| Jooble | none | `location` |

Unmapped generic `labels`, `categories`, geocoder outputs and `ai_*` suggestions
are never automatically geography. Provider address paths remain provider
evidence, not employer-native facts. Country values in city/region fields are
retained with type-mismatch diagnostics rather than promoted to those concepts.

## Offline normalization and selection

The licensed, repository-owned ISO snapshot supplies 249 countries and 5,046
administrative subdivisions. Its version/license/source are in
`worker/app/job_profile/data/README.md`. Runtime normalization is offline.
Explicit region fields require subdivision evidence and country context; Czech
regions additionally support reviewed English/native labels. Country/subdivision
name collisions such as Georgia are resolved through the explicit country.

A modest Czech municipality alias set supports Prague/Praha, Brno, Ostrava,
Plzeň/Pilsen, Pardubice, Kutná Hora, Rožnov pod Radhoštěm and other existing seed
cities. Diacritics and case are normalized. ISO districts are not a city gazetteer.
Typed locality fields support conservative postal-district/layout normalization
and sane municipalities outside the alias set. Weak labels require whole
recognized components; city mentions buried in prose are insufficient.
Explicit country wrappers, geographical hierarchies and independently valid
semicolon-separated places are handled without flattening multiple countries
onto one city. Unknown facility codes and ambiguous labels remain raw evidence.

Locations are ordered using the existing field-specific evidence rank:
explicit direct address, direct location parser, provider address, provider
label parser. They remain separate source-specific addresses; consumers must
not combine fields from different entries to invent an address. Equal top-tier
primary country/city/region disagreements flag conflict and preserve alternatives.
Weaker provider evidence never replaces native fields.

## Versions and invalidation

Schema: `job-profile-v1.1` (additive geography/raw-evidence fields).
Projector: `native-v1.1`. New normalizer layer: `geography-v1.1`.
Description cleaner, deterministic text parser, technology dictionary and taxonomy
versions are unchanged. The existing salary-parser city dictionary is unchanged.

Old V1.0 profiles remain readable. New input/version hashes differ; a later
explicit bounded enqueue would mark the old profile stale normally. Geography
changes require native/selection/profile rebuilding and can reuse the unchanged
text-layer cache. Nothing marks production profiles stale automatically here.

## Verification and limits

The reproducible archived 1,000-job sample is dated 2026-10-05. Coverage counts
describe available assertions, not human-labelled accuracy or live-market size.
Country coverage changed 935→966; city 757→712; region 253→243. Removed city claims
include countries, remote labels and ambiguous/mixed place strings. Raw evidence
is retained. The structural anomaly scan flags 131 jobs before and zero after;
this only means its defined checks pass, not that all geography is correct.

Final artifacts, source-family breakdowns, flagged-value CSV, city-loss review,
read-only same-three previews and database fingerprints are under
`/tmp/career-os-geography-v11-20261006/`.

Limits: the city aliases are intentionally incomplete; unknown countries' local
spelling, facility identifiers and uncertain administrative abbreviations can
abstain. A typed city field supplies semantics but is not independently geocoded.
Conservative quarantine can discard an attached address from a malformed label
record; its evidence remains available for review. Human labels are still needed
for accuracy assessment. No corrected production version has been persisted.
