# HEAVY native identity before canonical enrichment

This code is not deployed. It follows the committed scope-health policy and
changes neither RMK approvals nor its exact catalog-host/native-tenant/search-brand
ownership contract. No backfill or source identity migration is required.

## Identity contracts

Workday remains `tenant:case-sensitive-site:32-hex-posting-WID`, obtained from
`jobPostingInfo.id` after native request/site/URL checks. Requisition IDs, listing
paths and URL suffixes are not WIDs. An audit of 2,392 captured listing records
found no posting-WID field; captured pathless stubs contain requisition bullet
fields only. No speculative listing `id` field or requisition lookup is introduced.
A trustworthy detail header can prove a WID even when later title parsing fails.
A request failure without such a header remains identity-unknown.

RMK remains `native-tenant:numeric-posting-id`. Tenant-checked native search pages
already produce allowed-host posting URLs and numeric native posting IDs. The
native listing ID is now observed before fetching detail. It is not an internal
requisition, locale ID, URL hash, title hash or newly invented format. A detail
redirect to a different posting is a conflict, not an alias or a canonical reassignment.

## Flow and transaction boundary

1. Complete the existing guarded candidate discovery. Failed/partial discovery
   never authorizes disappearance and does not claim a complete empty catalog.
2. Prove source identity at the earliest supported layer: RMK native listing, or
   Workday's native detail header. Add it immediately to `InventoryScope.seen`.
3. Attempt detail/canonical enrichment. The in-memory identity manifest survives
   a typed parse failure or recoverable canonical-write failure.
4. Existing source refresh uses the existing lifecycle positive path. Only normal
   source activity/observation timestamps and lifecycle metadata are refreshed;
   malformed detail does not overwrite title, description, company or ownership.
5. New unparseable identities stay diagnostic-only: no placeholder company/job,
   orphan source or automatic attachment is created.

Each canonical write uses a nested `conn.transaction()` while the source's outer
transaction is already open. This creates a PostgreSQL savepoint, not a per-record
commit. Data/constraint errors and explicit parse/value errors roll back that
record, including any new company/job, without erasing native presence. Unexpected
SQL/programming/connection failures still abort and roll back the source; no absence
can commit from an aborted traversal. Advisory locks, budgets and deadlines are unchanged.

`IdentityManifest` stores source, exact scope, run/time, all proved IDs, identity
counts and untruncated failure observations. Sanitized request references omit
query strings/credentials. Workday pathless diagnostics include page, offset,
position, field names and a diagnostic digest. A digest is never a source ID.
Native identities are validated with the existing adapters before entering SEEN.

## Authority and quality remain separate

Native authority requires successful complete upstream traversal, complete native
identity coverage, no identity conflicts, policy permission and all the existing
host/tenant/brand/locale/terminal-pagination/pruning guards. Parser success for every
canonical record is no longer an independent presence requirement. Systemic
all-unusable parser failures remain a conservative authority veto.

Required-field/parser and unexpected detail-fetch failures remain alert-worthy
PARTIAL outcomes even when presence coverage is intact. Optional enrichment with
proved identity can be warning-only. A proved RMK listing followed by a native
HTTP 404 remains a visible detail diagnostic; an HTTP-200 empty shell retains its
listing identity but remains a canonical parse diagnostic. Workday detail 404
without a WID remains unknown and cannot prove complete identity coverage.

The scope-health consumer now reads the validated identity manifest to distinguish
presence completeness from enrichment alerts. Its four states, reviewed exceptions,
expiry, outcome vocabulary and policy config remain unchanged. Legacy runs without
a manifest retain the old conservative quality interpretation. Unknown identities,
identity conflicts, detail host escapes and reviewed quarantine still veto authority.
Blocked host transitions remain explicit in the failure ledger. Expiry or a single
recovered request never lifts quarantine. No LIGHT behavior or cron changes.

Structured output separates observations/identities/existing identities/new
unpersisted identities from details fetched, parse/upsert failures, optional
failures, required failures, empty shells and full error-signature counts. Counts
are observations per scope, not globally distinct jobs; brand-view overlap remains
non-authoritative. All failures retain exact native IDs where proved, so future
replay does not depend on the old ten-example diagnostic cap.

## Offline production evidence limits

The latest eight Workday stubs were retained only as counts, two in each of four
scopes. Their raw records, requisition values, page positions and WIDs cannot be
reconstructed. All eight remain unknown; none is re-keyed. Four additional native
detail 404 observations also lack WIDs in the archive. Those scopes were already
non-authoritative; the four complete Workday scopes retain their native guards.

RMK's 42 failed-record observations comprise 27 retained required-field failures,
three retained missing-detail-identity failures, one additional missing-identity
failure known only from aggregate counters, and eleven failures whose signatures
were not retained. Thirty failed native ID values plus all seven empty-shell ID
values are individually retained (37 total). The old candidate loop proves the
other twelve observations also had native listing IDs, but their numeric values
cannot be enumerated. They are not replaced with synthetic IDs. The eleven unknown
signatures remain an unresolved evidence limit; the fourth missing-identity error's
exact message is also unretained. No raw failed HTML was captured to prove a new
required-field parser fallback, so no speculative title/location defaults are added.

Vodafone's catalog had 764 candidates: 763 successful details and one empty shell
with native listing ID `vodafoneprP:1447440233`. That shell had no persisted source.
Its early observation restores identity coverage without creating a job. The
separate genuinely absent `vodafoneprP:1260754601` still closes in the in-memory
replay. Both completed RMK scopes remain eligible under exact ownership; the total
42 Run-2 deactivations are unchanged. Run 1 retained no source-row ownership metadata preimage; its quality counts and
retained listing identities can be replayed, but exact counterfactual absence effects
cannot. Its original zero deactivations reflected the old ownership guard. Later
metadata is not presented as a contemporaneous preimage.

Kaufland's 304 brand-view differences remain non-authoritative. Dachser, Gates,
Porsche Holding CZ and MANN+HUMMEL remain reviewed gaps; Nord Anglia recovery is
acquisition health, not expanded coverage.

Both measured runs replay as PARTIAL. Native presence is no longer lost on these
RMK detail failures, but genuine Workday unknowns, recurring canonical parser defects
and irretrievable historical signatures cannot be declared resolved. No new production
run, deployment or scheduler approval follows from this offline milestone. A future
acceptable validation is SUCCESS_WITH_KNOWN_GAPS only after unexpected identity/parser
blockers are resolved or evidenced; otherwise PARTIAL is correct.
