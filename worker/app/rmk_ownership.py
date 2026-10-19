"""Exact RMK catalog ownership shared by writes, lifecycle and reporting.

An owner is (catalog host, native tenant, native search brand). Locales are
views of that owner, not posting identities. ``hosts`` is a transport allowlist,
never an ownership election. The current reviewed gate approves singleton hosts.
No network, database access or metadata mutation occurs here.
"""
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RMKScopeIdentity:
    host: str
    tenant: str
    brand: str

    @property
    def key(self):
        return (self.host, self.tenant, self.brand)

    @classmethod
    def from_catalog(cls, search_url, tenant, brand):
        # Lazy import keeps adapter metadata creation free of an import cycle.
        from app.adapters.successfactors import public_url, COMPONENT
        p = urlsplit(public_url(search_url))
        if (not isinstance(tenant, str) or not COMPONENT.fullmatch(tenant)
                or not isinstance(brand, str) or p.path.rstrip('/') != '/' + (brand + '/' if brand else '') + 'search'):
            raise ValueError('Invalid native RMK catalog identity')
        return cls(p.hostname, tenant, brand)


@dataclass(frozen=True)
class OwnershipResolution:
    identity: RMKScopeIdentity | None
    state: str
    reason: str
    legacy: bool = False

    @property
    def authoritative(self):
        return self.identity is not None and self.state in {'EXACT', 'LEGACY_DETERMINISTIC'}

    def matches(self, scope):
        return self.authoritative and isinstance(scope, (list, tuple)) and self.identity.key == tuple(scope)


def reviewed_identity(scope, gate=None):
    """The configured gate is the only authority for lifecycle scope admission."""
    from app.successfactors_scopes import load_scope_gate
    gate = gate if gate is not None else load_scope_gate()
    if not isinstance(scope, (tuple, list)) or len(scope) != 3:
        return False
    return any((r.host, r.native_tenant, r.native_search_brand) == tuple(scope) for r in gate.scopes)


def resolve_ownership(metadata, source_identity, gate=None):
    """Resolve explicit or legacy metadata without guessing aliases or domains.

    Legacy proof requires an exact native catalog URL and agreement with the
    unique reviewed tenant/brand owner. A singleton array alone proves nothing.
    A known catalog owner with unapproved aliases/locales is REVIEW_REQUIRED.
    """
    from app.successfactors_scopes import load_scope_gate
    from app.adapters.successfactors import bounded_locale_views, normalize_locale, source_job_id
    if not isinstance(metadata, dict):
        return OwnershipResolution(None, 'MALFORMED', 'missing_rmk_site')
    legacy = 'host' not in metadata
    def result(identity, state, reason):
        return OwnershipResolution(identity, state, reason, legacy)
    try:
        owner = RMKScopeIdentity.from_catalog(metadata.get('search_url'), metadata.get('tenant'), metadata.get('brand'))
        tokens = source_identity.split(':')
        if len(tokens) != 2 or source_job_id(tokens[0], tokens[1]) != source_identity or tokens[0] != owner.tenant:
            return result(None, 'MALFORMED', 'native_posting_tenant_mismatch')
        if not legacy and metadata['host'] != owner.host:
            return result(None, 'AMBIGUOUS', 'scalar_host_conflicts_with_catalog')
        hosts = metadata.get('hosts')
        if (not isinstance(hosts, list) or not hosts or any(not isinstance(h, str) for h in hosts)
                or len(set(hosts)) != len(hosts)):
            return result(None, 'MALFORMED', 'invalid_transport_hosts')
        # Validate each host, without choosing any of them as the owner.
        for h in hosts:
            if RMKScopeIdentity.from_catalog('https://' + h + '/search/', owner.tenant, '').host != h:
                return result(None, 'MALFORMED', 'invalid_transport_host')
        if owner.host not in hosts:
            return result(None, 'AMBIGUOUS', 'catalog_outside_transport_hosts')
        gate = gate if gate is not None else load_scope_gate()
        rules = [r for r in gate.scopes if (r.native_tenant, r.native_search_brand) == (owner.tenant, owner.brand)]
        if len(rules) != 1 or rules[0].host != owner.host:
            return result(owner, 'REVIEW_REQUIRED', 'outside_validated_scope')
        rule = rules[0]
        if set(hosts) != {rule.host}:
            return result(owner, 'REVIEW_REQUIRED', 'unreviewed_transport_aliases')
        locale, locales = metadata.get('locale'), metadata.get('locales')
        advertised = metadata.get('advertised_locales', [])
        if (not isinstance(locales, list) or not locales or any(not normalize_locale(v) for v in locales)
                or len(set(locales)) != len(locales) or locale not in locales
                or not isinstance(advertised, list) or any(not normalize_locale(v) for v in advertised)):
            return result(owner, 'MALFORMED', 'invalid_locale_metadata')
        if rule.locale_union is not None:
            allowed = set(rule.locale_union)
            locale_ok = locale in allowed and set(locales) == allowed and metadata.get('locale_home_used') is True
        else:
            allowed = set(bounded_locale_views(rule.preferred_locale, advertised))
            locale_ok = locale == rule.preferred_locale and set(locales).issubset(allowed)
        if not locale_ok:
            return result(owner, 'REVIEW_REQUIRED', 'unreviewed_locale_views')
        return result(owner, 'LEGACY_DETERMINISTIC' if legacy else 'EXACT',
                      'validated_catalog_identity' if legacy else 'explicit_validated_catalog_identity')
    except (TypeError, ValueError, AttributeError):
        return result(None, 'MALFORMED', 'invalid_catalog_or_identity')


def ownership_report(sources, gate=None):
    """Expose unresolved coverage separately from complete-run flags."""
    from collections import Counter
    from app.successfactors_scopes import load_scope_gate
    gate = gate if gate is not None else load_scope_gate()
    rows = []
    for source in sources:
        resolution = resolve_ownership(source.get('native_scope'), source.get('source_job_id'), gate)
        rows.append({'source_job_id': source.get('source_job_id'), 'state': resolution.state,
                     'reason': resolution.reason, 'scope': list(resolution.identity.key) if resolution.identity else None})
    return {'states': dict(sorted(Counter(r['state'] for r in rows).items())),
            'authoritative_rows': sum(r['state'] in {'EXACT', 'LEGACY_DETERMINISTIC'} for r in rows),
            'unresolved_rows': [r for r in rows if r['state'] not in {'EXACT', 'LEGACY_DETERMINISTIC'}],
            'ownership_is_not_inventory_completeness': True}
