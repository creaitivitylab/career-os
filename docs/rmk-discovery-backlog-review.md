# RMK discovery backlog review — October 22, 2026

This review acknowledges four discoveries without approving acquisition,
authoritative coverage or lifecycle reconciliation. All 62 validated native
scopes, their locales and the ownership contract remain unchanged.

The existing `heavy_scope_health.json` discovery backlog holds expiring reviews.
The existing `successfactors_scopes.json` gate separately controls validated and
excluded execution scopes. `EXCLUDED` is represented by the existing
`excluded_scopes` category, not a new scope-health state. Its health review can
remain `NEEDS_REVIEW`, while the explicit market exclusion is visible.

| Discovery | Public evidence | Outcome |
| --- | --- | --- |
| `careers.vitra.com / vitra` | Tenant `vitraitser`; advertised `/vitra/search/`; native brand `vitra`; en_US/de_DE; diagnostic page advertised 16 postings, including Praha CZ | NEEDS_REVIEW; no acquisition approval |
| `jobs.aldi-hofer.com / aldi-karrier-hu` | Tenant `HoferSELive`; Hungarian landing/portal; no advertised RMK GET search form; separate AT/CH/HU/IT/SI market links | Explicit exclusion of this exact Hungarian brand; expiring NEEDS_REVIEW health acknowledgement |
| `jobs.kws.com / root` | Tenant `kwssaatse`; advertised `/search/`; native root brand; global multi-market/locale catalog advertised 119 postings in direct diagnostic and 120 in indexed view; Czech listing visible | NEEDS_REVIEW; no acquisition/alternate-locale approval |
| `jobs.vetropack.com / root` | Tenant `vetroconsu`; advertised `/search/`; native root brand; 17 advertised postings with Kyjov CZ and other markets; en_GB/cs_CZ among advertised locales | NEEDS_REVIEW; preferred locale and exact coverage not validated |

Public sources: [Vitra](https://careers.vitra.com/vitra/),
[ALDI Hungary landing](https://jobs.aldi-hofer.com/aldi-karrier-hu/),
[KWS catalog](https://jobs.kws.com/search/),
[Vetropack catalog](https://jobs.vetropack.com/search/).
Seven direct diagnostic GET requests inspected homepages and advertised first
catalog pages, alongside bounded browsing of official public sources. There was no pagination, detail crawl, facets POST, production DB
connection or ingestion. No new host was followed/approved by the worker.
No native GET catalog endpoint, native search-brand identity or catalog size was
established for the ALDI landing; they must not be guessed from its URL path.

Catalog sizes are observations, not completeness proofs. Public counts and locale
views may differ over time. A small first-page catalog is not sufficient for
validation. KWS's large primary catalog does not weaken the global >100 guard for
automatic alternate-locale fallback. No reviewed native tenant/catalog identity
duplicates these four in the existing validated gate.

Each new review explicitly starts `2026-10-22T00:00:00+00:00` and expires
`2026-11-19T00:00:00+00:00` (28 days). Existing scope reviews and their default
review metadata are unchanged. After expiry the review alerts again; expiry never
re-enables acquisition or lifecycle authority.

## What review does and does not authorize

* New unknown discovery: visible NEEDS_REVIEW, alert-worthy, PARTIAL.
* Current reviewed backlog: visible known gap; may contribute to
  SUCCESS_WITH_KNOWN_GAPS; acquisition and lifecycle permission remain false.
* Explicit reviewed exclusion: visible `excluded_scopes` category and exact reason;
  it is not retried as a new failure every run while the review remains current.
* Expired/future review, changed host/brand/category/exclusion reason, or additional
  reported coverage telemetry: PARTIAL until explicitly reviewed.
* Required parser/identity/systemic failures: remain alert-worthy even when all
  discovery backlog reviews are current.

The backlog evaluator compares exact reported telemetry and exposes the review,
known-gap flag, reason, coverage and denied acquisition/lifecycle permission.
It does not fetch unapproved catalogs to assert that remote coverage is unchanged.
No response, success or review can automatically insert a validated scope.

After the review, the captured discovery set contains 62 validated scopes,
22 unapproved discoveries and eight explicit exclusions. All 30 backlog entries
have reviews; the former 19 unapproved and seven excluded entries are unchanged.
Only the Hungarian brand moved from unapproved discovery to explicit exclusion;
other markets sharing the same host/tenant are not excluded or approved by it.

## Workday and outcome limits

The 16 additional Workday discoveries are actual selected scopes under existing
selection rules, not just telemetry. Their presence alone does not trigger the
RMK-only backlog checker. Exact complete Workday inventories can succeed without
a filtered-coverage review. A new filtered/incomplete Workday inventory without a
matching review remains PARTIAL and non-authoritative. No Workday review,
selection, identity, completeness or ingestion code is changed here.

Offline replay must distinguish three questions:

1. Discovery-only acceptance reference, holding approved acquisition/quality
   behavior healthy: four unknown discoveries cause PARTIAL before this review;
   reviewed backlog gives SUCCESS_WITH_KNOWN_GAPS afterward.
2. The real retained historical run: old quality/identity/missing-transition
   diagnostics still cause PARTIAL. This review does not repair or rewrite them.
3. Future live HEAVY: acceptable result is SUCCESS_WITH_KNOWN_GAPS only if there
   are no current unexpected acquisition, coverage or quality blockers. New
   Workday coverage and native record quality are observations to validate, not
   conditions this backlog review fabricates as healthy.

No deployment, live run, cron change, backfill, source rekey, merge or commit is
part of this milestone. After separate commit/deployment authorization, one
separately authorized prospective HEAVY validation can test the live conditions.
