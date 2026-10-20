"""Semantic record diagnostics. No identity is inferred from a listing URL.

Only adapter-proved native identities can protect an existing lifecycle row.
Required-field/parser failures remain alerts; optional enrichment is warning-only
when identity is preserved. Aggregates are lossless even when examples are capped.
"""
from collections import Counter


class RecordQuality:
    def __init__(self):
        self.known = 0
        self.unknown = 0
        self.failures = Counter()
        self.examples = []
        self.required_failures = 0

    def failure(self, kind, *, identity=None, stage='required', count=1, signature=None):
        if type(count) is not int or count < 0:
            raise ValueError('Invalid record failure count')
        if identity is not None and (not isinstance(identity, str) or not identity.strip()):
            raise ValueError('Invalid proved identity')
        if stage not in {'required', 'enrichment', 'identity'}:
            raise ValueError('Invalid quality stage')
        if not count:
            return
        self.known += count if identity else 0
        self.unknown += count if not identity else 0
        self.required_failures += count if stage != 'enrichment' else 0
        self.failures[(kind, stage, bool(identity), signature or kind)] += count
        if count and len(self.examples) < 10:
            self.examples.append({'kind': kind, 'stage': stage, 'source_job_id': identity, 'count': count})

    def protect(self, inventory, exc):
        identity = getattr(exc, 'source_identity', None)
        if identity:
            # InventoryScope enforces the exact native prefix/ownership contract.
            inventory.observe(identity)
        self.failure(exc.kind, identity=identity, signature=str(exc)[:160])

    def summary(self, records_fetched, records_with_known_identity, *, usable=None):
        systemic = bool(self.required_failures and usable == 0)
        return {'version': 1, 'records_fetched': records_fetched,
                'records_with_known_identity': records_with_known_identity,
                'identity_preserving_failures': self.known,
                'identity_unknown_failures': self.unknown,
                'records_with_unknown_identity': self.unknown,
                'required_field_failures': self.required_failures,
                'parsing_enrichment_failures': self.known + self.unknown,
                'systemic_failure': systemic,
                'systemic_reason': 'no usable records despite required-record failures' if systemic else None,
                'alert_worthy': bool(self.unknown or self.required_failures or systemic),
                'signatures': [{'kind': k, 'stage': s, 'identity_known': i, 'signature': signature, 'count': n}
                               for (k, s, i, signature), n in sorted(self.failures.items())],
                'examples': self.examples}


def legacy_quality(row):
    """Do not reinterpret capped historical examples as a complete failure ledger."""
    failed = row.get('failed', 0)
    # RMK's legacy counter includes an outer acquisition failure, not a record.
    if row.get('status') != 'success':
        failed = 0
    missing = row.get('listing_missing_path', 0) + row.get('missing_identity', 0)
    failed = max(failed, missing, sum(e.get('kind') not in {'withdrawn', 'unavailable'} for e in row.get('detail_errors', [])))
    withdrawn = row.get('withdrawn', 0)
    proved_404 = sum(e.get('kind') == 'withdrawn' and e.get('error') == 'Public RMK HTTP 404'
                     for e in row.get('detail_errors', []))
    uncertain_shells = max(0, withdrawn - proved_404)
    return {'version': 0, 'records_fetched': row.get('listing_records', 0),
            'identity_unknown_failures': missing + uncertain_shells,
            'identity_preserving_failures': 0,
            'unclassified_failures': max(0, failed - missing),
            'parsing_enrichment_failures': failed + uncertain_shells,
            'legacy_empty_or_unclassified_shells': uncertain_shells,
            'systemic_failure': False,
            'alert_worthy': bool(failed or uncertain_shells),
            'reason': 'Legacy samples do not prove native identity for every failed record',
            'examples': row.get('detail_errors', [])}
