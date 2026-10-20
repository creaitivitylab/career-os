"""Native presence is acquired independently of canonical enrichment.

No IDs are made from paths, requisitions, titles, or diagnostic fingerprints.
Factories use the existing source-native ID contracts and native scope checks.
The ledger contains no SQL writes; InventoryScope owns positive refresh/absence.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json


def diagnostic_context(record, **context):
    """A digest identifies a diagnostic observation, never a source posting."""
    encoded = json.dumps(record, sort_keys=True, default=str).encode()
    return {**context, 'record_keys': sorted(record) if isinstance(record, dict) else [],
            'diagnostic_fingerprint': hashlib.sha256(encoded).hexdigest(),
            'fingerprint_is_source_identity': False}


def rmk_listing_identity(config, listing):
    from app.adapters.successfactors import public_url, parse_rmk_url, source_job_id
    from app.rmk_ownership import RMKScopeIdentity
    owner = RMKScopeIdentity.from_catalog(config.search_url, config.tenant, config.brand)
    parsed = parse_rmk_url(public_url(listing.get('url')))
    if (not parsed or parsed['host'] not in config.hosts or owner.host not in config.hosts
            or parsed['posting_id'] != listing.get('posting_id')):
        raise ValueError('RMK native listing identity is missing or inconsistent')
    return source_job_id(config.tenant, parsed['posting_id'])


def validate_native_identity(source, scope, identity):
    if not isinstance(identity, str):
        raise ValueError('Invalid native identity')
    parts = identity.split(':')
    if source == 'workday_direct':
        from app.adapters.workday import WorkdaySite, source_job_id
        valid = len(parts) == 3 and source_job_id(WorkdaySite(*scope), parts[-1]) == identity
    elif source == 'successfactors_direct':
        from app.adapters.successfactors import source_job_id
        valid = len(parts) == 2 and source_job_id(scope[1], parts[-1]) == identity
    else:
        raise ValueError('Unsupported identity manifest family')
    if not valid:
        raise ValueError('Native identity escaped its exact scope')


def request_context(url):
    from urllib.parse import urlsplit, urlunsplit
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or p.username or p.password or not p.hostname:
            return {'request_url': None}
        return {'request_url': urlunsplit(('https', p.hostname, p.path, '', '')),
                'query_omitted': bool(p.query)}
    except (TypeError, ValueError):
        return {'request_url': None}


@dataclass
class NativeObservation:
    reference: str
    source_job_id: str | None = None
    origin: str = 'identity_unknown'
    context: dict | None = None
    enrichment_status: str = 'pending'
    detail_fetched: bool = False
    diagnostic: dict | None = None


class IdentityManifest:
    def __init__(self, inventory):
        self.inventory = inventory
        self.observations = []
        self.existing = set(inventory.before)
        self.persisted = set()
        self.conflicts = 0
        self.created_at = datetime.now(timezone.utc).isoformat()

    def add(self, reference, identity=None, *, origin='identity_unknown', context=None):
        observation = NativeObservation(str(reference), context=context)
        self.observations.append(observation)
        if identity is not None:
            self.prove(observation, identity, origin)
        return observation

    def prove(self, observation, identity, origin):
        if observation.source_job_id is not None and observation.source_job_id != identity:
            self.conflicts += 1
            raise ValueError('Native observation identity changed within one traversal')
        validate_native_identity(self.inventory.run.source, self.inventory.parts, identity)
        self.inventory.observe(identity)  # Validates exact native scope prefix.
        observation.source_job_id, observation.origin = identity, origin

    def load_existing(self):
        """One read before any canonical writes; includes legitimate RMK brand views."""
        identities = sorted({o.source_job_id for o in self.observations if o.source_job_id})
        if identities:
            self.inventory.run.cur.execute('''select source_job_id, job_id, is_active from public.job_sources
                where source_name = %s and source_job_id = any(%s)''',
                (self.inventory.run.source, identities))
            self.existing.update(row[0] for row in self.inventory.run.cur.fetchall())

    def fetched(self, observation):
        observation.detail_fetched = True

    def success(self, observation, *, persisted=False):
        observation.detail_fetched = True
        observation.enrichment_status = 'success'
        if persisted:
            self.persisted.add(observation.source_job_id)

    def failure(self, observation, exc, *, stage, signature, conflict=False):
        observation.enrichment_status = stage
        observation.diagnostic = {'kind': getattr(exc, 'kind', type(exc).__name__),
                                  'signature': signature, 'stage': stage}
        if getattr(exc, 'blocked_transition', None):
            observation.diagnostic['blocked_transition'] = dict(exc.blocked_transition)
        self.conflicts += bool(conflict)

    @property
    def unknown(self):
        return sum(o.source_job_id is None for o in self.observations)

    @property
    def identity_complete(self):
        return not self.unknown and not self.conflicts

    def summary(self):
        identities = sorted({o.source_job_id for o in self.observations if o.source_job_id})
        failures = [o for o in self.observations if o.diagnostic]
        new_failed = {o.source_job_id for o in failures if o.source_job_id
                      and o.source_job_id not in self.existing and o.source_job_id not in self.persisted}
        return {'version': 1, 'source_name': self.inventory.run.source,
                'scope': list(self.inventory.parts), 'run_id': self.inventory.run.run_id,
                'observed_at': self.created_at, 'observations_total': len(self.observations),
                'identities_proved': sum(o.source_job_id is not None for o in self.observations),
                'identities_unknown': self.unknown, 'distinct_identities_proved': len(identities),
                'identity_conflicts': self.conflicts, 'identity_complete': self.identity_complete,
                'existing_identities_seen': len(self.existing.intersection(identities)),
                'new_identities_unpersisted_due_parse_failure': len(new_failed),
                'details_fetched': sum(o.detail_fetched for o in self.observations),
                'canonical_parse_success': sum(o.enrichment_status == 'success' for o in self.observations),
                'canonical_parse_failures': sum(o.enrichment_status == 'canonical_parse' for o in failures),
                'canonical_upsert_failures': sum(o.enrichment_status == 'canonical_upsert' for o in failures),
                'optional_enrichment_failures': sum(o.enrichment_status == 'optional_enrichment' for o in failures),
                'detail_fetch_failures': sum(o.enrichment_status == 'detail_fetch' for o in failures),
                'empty_shells': sum(o.diagnostic['kind'] == 'withdrawn' and o.enrichment_status == 'canonical_parse' for o in failures),
                'identities': identities,
                # Never truncated: IDs/signatures/context only, not response bodies.
                'failure_observations': [o.__dict__.copy() for o in failures],
                'unknown_observations': [o.__dict__.copy() for o in self.observations if o.source_job_id is None]}


def identity_coverage(manifest, source, scope):
    """Validate presence accounting before separating it from parser alert status."""
    if not isinstance(manifest, dict) or manifest.get('version') != 1:
        raise ValueError('Missing native identity manifest')
    if manifest.get('source_name') != source or manifest.get('scope') != list(scope):
        raise ValueError('Identity manifest belongs to a different native scope')
    for field in ('observations_total', 'identities_proved', 'identities_unknown',
                  'distinct_identities_proved', 'identity_conflicts'):
        if type(manifest.get(field)) is not int or manifest[field] < 0:
            raise ValueError('Invalid identity accounting')
    if manifest['observations_total'] != manifest['identities_proved'] + manifest['identities_unknown']:
        raise ValueError('Incomplete identity accounting')
    identities = manifest.get('identities')
    if (not isinstance(identities, list) or any(not isinstance(i, str) for i in identities)
            or len(set(identities)) != len(identities)
            or len(identities) != manifest['distinct_identities_proved']
            or len(identities) > manifest['identities_proved']
            or manifest['identities_proved'] and not identities):
        raise ValueError('Invalid native identity manifest')
    for identity in identities:
        validate_native_identity(source, scope, identity)
    complete = not manifest['identities_unknown'] and not manifest['identity_conflicts']
    if manifest.get('identity_complete') is not complete:
        raise ValueError('Incorrect identity completeness claim')
    return complete
