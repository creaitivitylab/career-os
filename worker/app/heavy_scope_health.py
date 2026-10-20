"""HEAVY operational policy, independent of native lifecycle authority.

A review acknowledges a bounded coverage gap; it never approves a host/locale,
changes an inventory's complete flag, or makes an unknown identity safe.
"""
from collections import Counter
from datetime import datetime, timezone, timedelta
import json
import re
from pathlib import Path

from app.record_quality import legacy_quality

STATES = {'HEALTHY', 'TRANSIENT_FAILURE', 'KNOWN_UNAVAILABLE', 'NEEDS_REVIEW'}
COVERAGE = {'AUTHORITATIVE_COMPLETE', 'NON_AUTHORITATIVE_EXPECTED', 'INCOMPLETE', 'UNAVAILABLE', 'UNKNOWN'}
HEAVY = {'workday_direct', 'successfactors_direct'}
POLICY = Path(__file__).with_name('config') / 'heavy_scope_health.json'


def timestamp(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Review timestamps require timezone')
    return result


def load_policy(path=POLICY):
    data = json.loads(Path(path).read_text())
    if data.get('version') != 1:
        raise ValueError('Unsupported HEAVY policy')
    for key in ('scopes', 'discovery_backlog'):
        data[key] = [{**data.get('review', {}), **row} for row in data[key]]
    seen = set()
    for row in data['scopes'] + data['discovery_backlog']:
        key = (row['source_name'], tuple(row['scope']))
        if key in seen or row['source_name'] not in HEAVY or row['state'] not in STATES:
            raise ValueError('Invalid/duplicate HEAVY review')
        seen.add(key)
        if (len(row['scope']) != (3 if row['source_name'] == 'workday_direct' else 2)
                or any(not isinstance(v, str) for v in row['scope'])):
            raise ValueError('Invalid exact operational scope')
        if row in data['scopes']:
            if 'expected_error' in row:
                expected = row['expected_error']
                if not expected.get('kind') or not expected.get('error'):
                    raise ValueError('Reviewed error must be exact and nonempty')
            elif not isinstance(row.get('coverage_signature'), dict):
                raise ValueError('Review lacks exact coverage signature')
        start, end = timestamp(row['reviewed_at']), timestamp(row['review_expires_at'])
        if not timedelta(0) < end - start <= timedelta(days=31) or not row['reason'] or not row['evidence']:
            raise ValueError('Invalid bounded HEAVY review')
    return data


def review_valid(row, now):
    return bool(row and timestamp(row['reviewed_at']) <= now < timestamp(row['review_expires_at']))


def scope_key(source, row):
    # Operational lookup only. Native ownership matching remains rmk_ownership's job.
    return [row['host'], row['tenant'], row['site']] if source == 'workday_direct' else [row['host'], row.get('brand', '')]


def coverage_signature(source, row):
    """Review semantic coverage boundaries, not result counts or arbitrary aliases."""
    if source == 'workday_direct':
        return {'mode': row.get('discovery_mode'), 'foreign_pruning': bool(row.get('fallback_skipped_foreign'))}
    config = row.get('config', {})
    return {'modes': sorted({d.get('mode') for d in row.get('discovery', [])}),
            'locales': sorted(config.get('locales', [])),
            'advertised_locales': sorted(config.get('advertised_locales', [])),
            'foreign_pruning': bool(row.get('fallback_skipped_foreign')),
            'unapproved_locales': sorted(row.get('unapproved_locales', [])),
            'tenant': config.get('tenant'), 'brand': config.get('brand')}


def policy_allows_lifecycle(source, row, policy):
    """A reviewed quarantine can only veto, never confer native authority.

    Expiry does not lift quarantine. A recovered request still needs review.
    """
    key = scope_key(source, row)
    review = next((r for r in policy['scopes'] if r['source_name'] == source and r['scope'] == key), None)
    return not review or review['state'] not in {'NEEDS_REVIEW', 'KNOWN_UNAVAILABLE'}


def failure_state(row):
    # A persistent/configuration/pagination failure is not a transport timeout.
    if row.get('kind') == 'transient' or (row.get('kind') == 'http' and
            re.search(r'HTTP (429|502|503|504)\b', row.get('error', ''))):
        return 'TRANSIENT_FAILURE'
    return 'NEEDS_REVIEW'


def classify_scope(source, row, now, policy):
    key = scope_key(source, row)
    review = next((r for r in policy['scopes'] if r['source_name'] == source and r['scope'] == key), None)
    valid = review_valid(review, now)
    life = row.get('lifecycle') or {}
    recorded_quality = row.get('record_quality') or {}
    quality = dict(recorded_quality if recorded_quality.get('version') == 1 else legacy_quality(row))
    for field in ('identity_preserving_failures', 'identity_unknown_failures', 'unclassified_failures',
                  'required_field_failures', 'parsing_enrichment_failures', 'unexpected_detail_fetch_failures'):
        if type(quality.get(field, 0)) is not int or quality.get(field, 0) < 0:
            raise ValueError('Invalid record quality counter')
    if quality.get('version') == 1:
        known = quality.get('identity_preserving_failures', 0)
        unknown = quality.get('identity_unknown_failures', 0)
        total = quality.get('parsing_enrichment_failures', 0)
        minimum = row.get('failed', 0) - (row.get('status') == 'failed')
        if (total != known + unknown or total < minimum
                or quality.get('required_field_failures', 0) > total
                or sum(g['count'] for g in quality.get('signatures', [])) != total):
            raise ValueError('Inconsistent record quality ledger')
    # Recalculate alert semantics; a serialized boolean cannot suppress risk.
    quality['alert_worthy'] = bool(quality.get('identity_unknown_failures')
        or quality.get('unclassified_failures') or quality.get('required_field_failures')
        or quality.get('unexpected_detail_fetch_failures') or quality.get('systemic_failure'))
    manifest = row.get('identity_manifest')
    if manifest is not None:
        from app.native_observation import identity_coverage
        presence_complete = identity_coverage(manifest, source, life.get('scope', []))
        if quality.get('identity_unknown_failures', 0) != manifest['identities_unknown']:
            raise ValueError('Quality and native identity accounting disagree')
        quality_blocks_authority = not presence_complete or bool(quality.get('systemic_failure'))
    else:
        # Old rows lack independent listing/native-ID proof; retain conservative compatibility.
        quality_blocks_authority = quality['alert_worthy']
    acquisition = row.get('status') == 'success'
    complete = acquisition and life.get('successful') is True and life.get('complete') is True
    alerts = []
    if review and not valid:
        alerts.append('review_expired_or_not_yet_effective')
    matched = False
    if valid and 'expected_error' in review:
        expected = review['expected_error']
        matched = all(row.get(k) == v for k, v in expected.items())
        if acquisition:
            alerts.append('reviewed_exception_changed_behavior_requires_review')
    elif valid:
        matched = review.get('coverage_signature') == coverage_signature(source, row)
    if complete:
        coverage = 'AUTHORITATIVE_COMPLETE'
        if review and review['state'] != 'HEALTHY':
            alerts.append('reviewed_non_authoritative_scope_claimed_complete')
            coverage = 'INCOMPLETE'
    elif valid and matched:
        coverage = 'NON_AUTHORITATIVE_EXPECTED' if acquisition or row.get('status') == 'unresolved' else 'UNAVAILABLE'
    else:
        coverage = 'INCOMPLETE' if acquisition else 'UNAVAILABLE'
        alerts.append('unexpected_coverage_gap' if acquisition else 'unexpected_acquisition_failure')
    quarantined = review and review['state'] in {'KNOWN_UNAVAILABLE', 'NEEDS_REVIEW'}
    state = review['state'] if quarantined or valid and matched else 'HEALTHY' if acquisition else failure_state(row)
    if acquisition and (not life or type(life.get('complete')) is not bool
                        or type(life.get('successful')) is not bool):
        coverage = 'UNKNOWN'
        alerts.append('missing_lifecycle_scope_evidence')
    elif acquisition and life.get('successful') is not True:
        coverage = 'INCOMPLETE'
        alerts.append('acquisition_inventory_status_mismatch')
    if quality.get('alert_worthy'):
        alerts.append('record_quality_requires_review')
        if complete and quality_blocks_authority:
            coverage = 'INCOMPLETE'
    if quality.get('identity_unknown_failures') or quality.get('unclassified_failures'):
        if complete:
            alerts.append('identity_coverage_requires_downgrade')
    if quality.get('systemic_failure'):
        alerts.append('systemic_record_failure')
    return {'scope': key, 'native_scope': life.get('scope'), 'state': state,
            'acquisition_health': 'HEALTHY' if acquisition else state,
            'acquisition_result': row.get('status'), 'coverage': coverage,
            'lifecycle_eligible': complete and not quality_blocks_authority and not quarantined,
            'native_inventory_complete': complete,
            'lifecycle_reason': ('Reviewed quarantine requires explicit review' if quarantined else
                'Record quality does not prove complete native identity coverage' if quality_blocks_authority else
                life.get('reason', 'No completed native inventory')),
            'native_lifecycle_reason': life.get('reason'),
            'record_quality': quality,
            'identity_accounting': {k: v for k, v in manifest.items()
                if k not in {'identities', 'failure_observations', 'unknown_observations'}} if manifest else None,
            'alerts': alerts, 'alert_worthy': bool(alerts),
            'known_gap': valid and matched and not complete and coverage in {'NON_AUTHORITATIVE_EXPECTED', 'UNAVAILABLE'},
            'review': review, 'duration_seconds': row.get('duration_seconds'),
            'diagnostics': {k: row[k] for k in ('kind', 'error', 'blocked_transition') if k in row}}


def evaluate_source(source, result, now=None, policy=None):
    now = timestamp(now or datetime.now(timezone.utc))
    policy = load_policy() if policy is None else policy
    rows = result.get('site_results', [])
    health = [classify_scope(source, row, now, policy) for row in rows]
    native = {(tuple(r.get('scope', []))): r for r in result.get('lifecycle', {}).get('scopes', [])}
    if len(native) != len(result.get('lifecycle', {}).get('scopes', [])):
        raise ValueError('Duplicate native inventories')
    for row, item in zip(rows, health):
        life = row.get('lifecycle')
        if life and native.get(tuple(life['scope'])) != life:
            raise ValueError('Conflicting native scope diagnostics')
        if item['native_inventory_complete'] and not item['lifecycle_eligible']:
            item['alerts'].append('legacy_authority_requires_record_quality_review')
            item['alert_worthy'] = True
    keys = [tuple(row['scope']) for row in health]
    alerts = []
    if len(rows) != result.get('sites') or len(set(keys)) != len(keys):
        alerts.append('missing_or_duplicate_scope_results')
    if sum(row.get('failed', 0) for row in rows) != result.get('failed', 0):
        alerts.append('record_failure_aggregate_mismatch')
    # Policy cannot suppress an outer error absent from the corresponding row.
    for error in result.get('site_errors', []):
        matching = [h for h in health if h['scope'] == scope_key(source, error)]
        if (len(matching) != 1 or matching[0]['acquisition_result'] == 'success' or not matching[0]['known_gap']
                or any(error.get(k) != matching[0]['diagnostics'].get(k) for k in ('kind', 'error'))):
            alerts.append('unexpected_source_error')
    backlog = []
    if source == 'successfactors_direct':
        for category in ('unapproved_discovered_scopes', 'excluded_scopes'):
            for row in result.get(category, []):
                key = [row['host'], row.get('brand', '')]
                review = next((r for r in policy['discovery_backlog'] if r['scope'] == key and r['category'] == category), None)
                valid = review_valid(review, now)
                # Changed exclusion reason is new evidence, not an automatic approval.
                valid = valid and (category != 'excluded_scopes' or review['reason'] == row.get('reason'))
                backlog.append({'scope': key, 'category': category, 'state': 'NEEDS_REVIEW', 'review': review,
                                'alert_worthy': not valid, 'lifecycle_eligible': False})
                if not valid:
                    alerts.append('new_or_expired_discovery_backlog')
    unexpected = bool(alerts or any(h['alert_worthy'] for h in health))
    gaps = [h for h in health if h['known_gap']]
    if not rows or (result.get('status') == 'failed' and not (len(gaps) == len(rows) and not unexpected)):
        status = 'FAILED'
    elif unexpected:
        status = 'PARTIAL'
    elif gaps or backlog:
        status = 'SUCCESS_WITH_KNOWN_GAPS'
    else:
        status = 'SUCCESS'
    return {'status': status, 'scopes': health, 'discovery_backlog': backlog,
            'alerts': alerts, 'alert_worthy': unexpected,
            'state_counts': dict(Counter(h['state'] for h in health)),
            'coverage_counts': dict(Counter(h['coverage'] for h in health)),
            'known_gaps': gaps,
            'record_quality_totals': {key: sum(h['record_quality'].get(key, 0) for h in health)
                for key in ('records_fetched', 'records_with_known_identity', 'identity_preserving_failures',
                            'identity_unknown_failures', 'unclassified_failures', 'parsing_enrichment_failures')},
            'identity_totals': {key: sum((h.get('identity_accounting') or {}).get(key, 0) for h in health)
                for key in ('observations_total', 'identities_proved', 'identities_unknown', 'existing_identities_seen',
                            'new_identities_unpersisted_due_parse_failure', 'canonical_parse_failures',
                            'canonical_upsert_failures', 'detail_fetch_failures', 'empty_shells')},
            'systemic_quality_scopes': sum(bool(h['record_quality'].get('systemic_failure')) for h in health),
            'review_never_grants_lifecycle_authority': True}


def health_report(runs, now):
    report = {}
    for source in sorted(HEAVY):
        history = sorted((r for r in runs if r['source_name'] == source), key=lambda r: timestamp(r['started_at']))
        if history:
            latest = history[-1]
            metadata = latest.get('metadata') or {}
            evaluation = evaluate_source(source, {**metadata, 'status': latest['status']}, now)
            for item in evaluation['scopes']:
                observations = [(r, row) for r in reversed(history)
                    for row in (r.get('metadata') or {}).get('site_results', [])
                    if scope_key(source, row) == item['scope']]
                consecutive = 0
                for _, row in observations:
                    if row.get('status') == 'success':
                        break
                    consecutive += 1
                last_failure = next(((r, row) for r, row in observations if row.get('status') != 'success'), None)
                item['history'] = {
                    'consecutive_acquisition_failures': consecutive,
                    'last_successful_acquisition': next((str(r['started_at']) for r, row in observations if row.get('status') == 'success'), None),
                    'last_native_complete_inventory': next((str(r['started_at']) for r, row in observations if (row.get('lifecycle') or {}).get('complete')), None),
                    'last_failure': {'started_at': str(last_failure[0]['started_at']),
                                     'kind': last_failure[1].get('kind'), 'error': last_failure[1].get('error')} if last_failure else None}
            report[source] = {'run_id': str(latest['id']), 'started_at': str(latest['started_at']),
                              **evaluation}
    return report
