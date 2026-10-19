# RMK ownership contract

RMK source metadata previously stored `rmk_site.hosts`, while lifecycle SQL and
freshness reporting read `rmk_site.host`. The writer never supplied that scalar.
The guards therefore matched zero rows, even for completed inventories. This
was a deterministic contract mismatch; it did not authorize unsafe closures.

## Identity and authority

`RMKScopeIdentity` in `app.rmk_ownership` defines the exact catalog owner:
`(host, native tenant, native search brand)`. Host is the public HTTPS origin of
the advertised native search form (`search_url`). Brand is the native catalog
path prefix before `/search/`, which may differ from the public discovery brand.
The native tenant is `ssoCompanyId`. Native posting identity remains
`{tenant}:{rmk_posting_id}`. No source identities or canonical assignments change.

`rmk_site.host` now persists that catalog owner. `rmk_site.hosts` remains the
advertised transport allowlist/diagnostic host set. No array position or sort
order elects an owner. The generic adapter can verify advertised aliases by
checking their native tenant; the current unattended validated gate requires
`hosts == {reviewed host}`. No alternate host becomes approved through this fix.
Redirects must remain inside the allowed set. Locale links are views of a
catalog, and their hosts are not implicitly allowed aliases.

Locales define coverage, not a fourth ownership or source-identity dimension.
The primary view must agree with the reviewed locale policy. A reviewed locale
union can recognize older metadata using another member as primary, provided
the exact approved locale set and brand-homepage confirmation are present.
Other views are limited to the existing publicly advertised bounded English
recall policy. The 100-posting alternate fallback guard is unchanged.

## Legacy rows and lifecycle

`resolve_ownership` is pure and shared by lifecycle and reporting. For legacy
rows it requires a persisted native search URL, agreement of catalog path,
tenant and source identity, and the unique reviewed native tenant/brand owner.
The transport and locale metadata must also be compatible with that review.
A singleton `hosts` array alone is insufficient. Conflicting scalar/catalog
hosts are ambiguous. Missing or malformed evidence abstains. A determinable
catalog owner with unapproved aliases/locales remains `REVIEW_REQUIRED` and
cannot authorize absence matching.

Lifecycle selects candidates by exact native tenant, then applies the shared
resolver. Only exact matching owned rows can enter the disappearance set of a
successful complete inventory. It intersects the initial owned snapshot with
currently owned rows and checks the full ownership metadata preimage in the
UPDATE predicate. Unvalidated target scopes cannot claim completion. Normal
positive native-ID observations remain safe across shared brand views; those
observations cannot prove absence ownership. Failed/partial inventories retain
their existing protections and canonical activity rules are unchanged.

Freshness requires the same resolved owner, a successful unbounded reviewed
run, and matching successful complete lifecycle scope evidence. It distinguishes
`EXACT`, `LEGACY_DETERMINISTIC`, `AMBIGUOUS`, `MALFORMED` and `REVIEW_REQUIRED` in
the report. Deterministic ownership does not itself prove recent completeness.

## Offline evidence and optional backfill

The read-only October 19 audit classified all 1,520 RMK rows as deterministic
legacy owners using persisted catalog URLs corroborated by the reviewed gate.
Replay recognizes 123/123 complete positive observations and 42/42 unseen rows
(41 Správa železnic, one Vodafone). Both captured complete inventories would
have been eligible for reconciliation, with 42 counterfactual deactivations.
The remaining 1,355 rows have no complete inventory from this measured run.
No production changes were made by the replay.

Compatibility makes deployment independent of backfill. An optional separately
authorized backfill can fill the scalar for exactly the 1,520 manifest rows;
current categories are SAFE_BACKFILL 1,520, all others zero. Re-audit on execution
day. Back up full preimages outside /tmp, enforce exact IDs/source family and
metadata hashes, lock deterministically and repeat predicates in one transaction.
Update only `raw_payload.rmk_site.host`; do not touch timestamps, lifecycle,
canonical/company links or source identities. Already correct rows must be
no-ops; drift, conflicting scalars or an unexpected row count must abort. Verify
all other fields/tables unchanged before committing once. Retain checksums,
postimages and an exact preimage-based rollback plan. No executable production
backfill or migration is included.

## Separate scope exceptions

- Dachser: same reviewed tenant/brand catalog, cs_CZ and en_US both advertise 192
  results. Primary has Czech facets; alternate does not. Retain the cap and
  require deliberate per-scope fallback review before a broader traversal.
- Gates: primary cs_CZ advertises two results, en_US 167, with no geography
  facets. The same tenant is not proof of the same market. Retain protection;
  review explicit locale coverage and runtime before approving any full catalog.
- Porsche Holding CZ: primary cs_CZ advertises nine results; en_US has no native
  count and no paths. Keep non-authoritative. Diagnose the empty alternate view
  separately; do not promote an uncounted catalog to an empty complete one.
- Nord Anglia: the measured run's exact seed returned 403; a later single public
  GET now returns 200 with the expected native tenant. This disproves persistent
  unavailability, not inventory completeness. Keep ordinary access/failure
  protections; never bypass a future 403 or infer disappearance from it.
- MANN+HUMMEL: both the old homepage and exact seed return 301 to
  `https://careers.mann-hummel.com`. The redirect advertises a candidate new host,
  also present in unapproved discovery. A single public destination-homepage
  diagnostic returns 200 and the same native tenant `mannhummel`, confirming
  tenant continuity, but no advertised GET search form is available there.
  Keep the current block. A separate reviewed host-migration policy must prove
  the catalog/brand and address legacy owner metadata before any host change;
  the gate forbids two independent owners for the same native tenant/brand.

No scope-policy configuration, Workday behavior, company resolution, dedupe,
scheduler or production data is changed by this milestone.

## Verification

`worker/tests/test_rmk_ownership.py` exercises pure ownership, real incident
patterns, lifecycle/reporting parity and real PostgreSQL reconciliation and
rollback. Database tests require a dedicated loopback database named
`career_os_rmk_test` through `RMK_TEST_DATABASE_URL`; never supply a production
URL. Run the full worker suite with its existing isolated profile/company test
databases as well. Production evidence and the row-bounded backfill plan are
under `/tmp/career-os-rmk-ownership-fix-20261019/`.
