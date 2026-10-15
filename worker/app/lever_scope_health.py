"""Reviewed coverage gaps, not native inventory evidence or lifecycle policy."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from app.adapters.lever import API_HOSTS, normalize_site, LeverHTTPError, LeverPaginationError

CONFIG = Path(__file__).parent / 'config' / 'lever_scope_health.json'
STATES = {'HEALTHY', 'TRANSIENT_FAILURE', 'KNOWN_UNAVAILABLE', 'NEEDS_REVIEW'}


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Scope review timestamps require a timezone')
    return parsed


def load_policy(path=CONFIG):
    value = json.loads(Path(path).read_text())
    if value.get('version') != 1 or not isinstance(value.get('scopes'), list):
        raise ValueError('Invalid Lever scope-health configuration')
    seen = set()
    for row in value['scopes']:
        scope = row.get('scope')
        if (row.get('source_name') != 'lever_direct' or not isinstance(scope, list) or len(scope) != 2
                or scope[0] not in API_HOSTS or normalize_site(scope[1]) != scope[1]
                or tuple(scope) in seen or row.get('state') not in {'KNOWN_UNAVAILABLE', 'NEEDS_REVIEW'}
                or not isinstance(row.get('reason'), str) or not row['reason'].strip()
                or type(row.get('recheck_hours')) is not int or not 24 <= row['recheck_hours'] <= 720):
            raise ValueError('Invalid reviewed Lever scope')
        if timestamp(row['review_expires_at']) <= timestamp(row['reviewed_at']):
            raise ValueError('Invalid scope review expiry')
        seen.add(tuple(scope))
    return value['scopes']


def entry(scope, policy):
    return next((row for row in policy if row['scope'] == list(scope)), None)


def review_valid(row, now):
    return timestamp(row['reviewed_at']) <= now < timestamp(row['review_expires_at'])


def scope_decision(scope, history, now=None, policy=None):
    now = now or datetime.now(timezone.utc)
    policy = load_policy() if policy is None else policy
    row = entry(scope, policy)
    if row is None:
        return None
    probes = [item for item in history if item.get('scope') == list(scope) and item.get('probed_at')
              and timestamp(row['reviewed_at']) <= timestamp(item['probed_at']) <= now]
    latest = max(probes, key=lambda item: timestamp(item['probed_at']), default=None)
    last_probe = timestamp(latest['probed_at']) if latest else None
    due = last_probe is None or now >= last_probe + timedelta(hours=row['recheck_hours']) or latest.get('alert_worthy') is True
    # Expiry alerts and continues quarantine: it never authorizes a full crawl.
    return {'scope': list(scope), 'state': row['state'], 'reason': row['reason'],
            'reviewed_at': row['reviewed_at'], 'review_expires_at': row['review_expires_at'],
            'recheck_hours': row['recheck_hours'],
            'reviewed': True, 'expected_gap': review_valid(row, now), 'coverage_gap': True,
            'action': 'probe' if due else 'skip', 'probe_due': due,
            'alert_worthy': not review_valid(row, now),
            'last_probe_at': last_probe.isoformat() if last_probe else None,
            'next_probe_at': (last_probe + timedelta(hours=row['recheck_hours'])).isoformat() if last_probe else now.isoformat()}


def probe_scope(decision, adapter, api_site, instance, now):
    result = dict(decision)
    if result['action'] != 'probe':
        return result
    result['probed_at'] = now.isoformat()
    result['next_probe_at'] = (now + timedelta(hours=result['recheck_hours'])).isoformat()
    try:
        status = adapter.probe(api_site, instance)
        result['probe_http_status'] = status
        if result['state'] == 'KNOWN_UNAVAILABLE' and status == 200:
            result.update(state='NEEDS_REVIEW', expected_gap=False, alert_worthy=True,
                          reason='Previously unavailable scope reappeared; review before re-enabling inventory')
        elif not ((result['state'] == 'KNOWN_UNAVAILABLE' and status == 404)
                  or (result['state'] == 'NEEDS_REVIEW' and status == 200)):
            result.update(state='TRANSIENT_FAILURE', expected_gap=False, alert_worthy=True,
                          reason='Unexpected availability probe response')
    except Exception as exc:
        result.update(state='TRANSIENT_FAILURE', expected_gap=False, alert_worthy=True,
                      reason='Availability probe failed', error_kind=type(exc).__name__)
    # A probe is never a complete inventory, even for an empty HTTP-200 response.
    return result


def failure_health(scope, exc):
    review = isinstance(exc, LeverPaginationError) or isinstance(exc, LeverHTTPError) and exc.status_code == 404
    result = {'scope': list(scope), 'state': 'NEEDS_REVIEW' if review else 'TRANSIENT_FAILURE',
              'action': 'inventory', 'coverage_gap': True, 'reviewed': False,
              'expected_gap': False, 'alert_worthy': True, 'reason': str(exc)}
    if isinstance(exc, LeverPaginationError):
        result['pagination'] = exc.diagnostics
    if isinstance(exc, LeverHTTPError):
        result['http_status'] = exc.status_code
    return result


def accepted_gaps(health, scopes, now=None, policy=None):
    """Only current reviewed config can excuse a quarantined non-inventory scope."""
    policy = load_policy() if policy is None else policy
    now = now or datetime.now(timezone.utc)
    accepted = []
    for item in health:
        row = entry(item.get('scope', []), policy)
        outcome = next((s for s in scopes if s['scope'] == item.get('scope')), None)
        checked = False
        if row:
            probe_time = item.get('probed_at') if item.get('action') == 'probe' else item.get('last_probe_at')
            if probe_time:
                time = timestamp(probe_time)
                checked = (timestamp(row['reviewed_at']) <= time <= now
                           and now < time + timedelta(hours=row['recheck_hours']))
                if item.get('action') == 'probe':
                    checked = checked and item.get('probe_http_status') == (404 if item.get('state') == 'KNOWN_UNAVAILABLE' else 200)
        if (row and review_valid(row, now) and item.get('reviewed_at') == row['reviewed_at']
                and item.get('state') == row['state'] and item.get('expected_gap') is True
                and item.get('coverage_gap') is True and item.get('alert_worthy') is False
                and item.get('action') in {'probe', 'skip'} and checked and outcome
                and outcome['complete'] is False and outcome['successful'] is False
                and outcome.get('deactivated', 0) == 0 and outcome.get('reactivated', 0) == 0
                and outcome.get('observed', 0) == 0):
            accepted.append(item)
    return accepted


def scope_health_report(runs, now, policy=None):
    policy = load_policy() if policy is None else policy
    rows = {}
    for run in sorted((r for r in runs if r['source_name'] == 'lever_direct'), key=lambda r: timestamp(str(r['started_at']).replace(' ', 'T'))):
        meta = run.get('metadata') or {}
        recorded = {tuple(h['scope']): h for h in meta.get('scope_health', [])}
        for scope in (meta.get('lifecycle') or {}).get('scopes', []):
            key = tuple(scope['scope'])
            row = rows.setdefault(key, {'scope': list(key), 'consecutive_failed_inventories': 0,
                                        'last_complete_inventory': None})
            if scope.get('successful') and scope.get('complete') and run['status'] == 'success':
                row.update(state='HEALTHY', consecutive_failed_inventories=0,
                           last_complete_inventory={'run_id': str(run['id']), 'finished_at': run.get('finished_at')})
            else:
                health = recorded.get(key)
                if health is None:
                    error = next((e for e in meta.get('site_errors', [])
                                  if [e.get('instance'), e.get('site')] == list(key)), {})
                    message = error.get('error', scope.get('reason', 'Scope failed'))
                    health = {'scope': list(key), 'state': 'NEEDS_REVIEW' if
                              '404' in message or 'pagination' in message.lower() else 'TRANSIENT_FAILURE',
                              'action': 'inventory', 'reason': message, 'alert_worthy': True,
                              'reviewed': False, 'expected_gap': False}
                if not health or health.get('action') == 'inventory':
                    row['consecutive_failed_inventories'] += 1
                row['state'] = health['state'] if health else 'TRANSIENT_FAILURE'
            row.update(last_attempt_at=run['started_at'], last_run_id=str(run['id']),
                       latest_health=recorded.get(key) if scope.get('complete') else health,
                       coverage_gap=row['state'] != 'HEALTHY')
    for reviewed in policy:
        key = tuple(reviewed['scope'])
        row = rows.setdefault(key, {'scope': list(key), 'consecutive_failed_inventories': 0,
                                   'last_complete_inventory': None})
        latest = row.get('latest_health') or {}
        unexpected_probe = latest.get('reviewed_at') == reviewed['reviewed_at'] and latest.get('alert_worthy') is True
        row.update(state=latest.get('state') if unexpected_probe else reviewed['state'],
                   configured_state=reviewed['state'], coverage_gap=True, reviewed_reason=reviewed['reason'],
                   review_expires_at=reviewed['review_expires_at'], review_overdue=not review_valid(reviewed, now),
                   alert_worthy=unexpected_probe or not review_valid(reviewed, now),
                   expected_gap=not unexpected_probe and review_valid(reviewed, now))
    return [rows[key] for key in sorted(rows)]
