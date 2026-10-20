"""Guarded synchronous orchestration for scheduled native freshness.

Runs inside the worker, not through an HTTP client that can time out while the
server keeps writing. --apply is required. No dedupe, merging or enrichment.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import importlib
import json
import logging
import math
from pathlib import Path
import re
import sys
import time
import uuid

from app.operation_budget import DeadlineExceeded, ingestion_budget
from app.transient_retry import (METRICS, acquisition_summary, transient_retry_policy,
                                 validate_policy)

LIGHT = ('smartrecruiters_direct', 'greenhouse_direct', 'workable_direct', 'ashby_direct', 'lever_direct')
HEAVY = ('workday_direct', 'successfactors_direct')
GROUPS = {'light': LIGHT, 'heavy': HEAVY, 'all': LIGHT + HEAVY}
EXIT_CODES = {'SUCCESS': 0, 'SUCCESS_WITH_KNOWN_GAPS': 0, 'PARTIAL': 2, 'FAILED': 1, 'SKIPPED_LOCKED': 75}
CONFIG = Path(__file__).parent / 'config' / 'freshness.json'
LOCK = Path('/tmp/career-os-freshness/runner.lock')
log = logging.getLogger('career_os.freshness')


def utc_now():
    return datetime.now(timezone.utc)


def positive_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('Deadline/budget must be a finite positive number')
    return value


def load_config(path=CONFIG):
    c = json.loads(Path(path).read_text())
    if c.get('version') != 1 or set(c['groups']) != set(GROUPS) or set(c['source_deadlines_seconds']) != set(LIGHT + HEAVY):
        raise ValueError('Invalid freshness configuration')
    for value in c['groups'].values():
        positive_number(value['deadline_seconds']); positive_number(value['expected_seconds'])
    for value in c['source_deadlines_seconds'].values():
        positive_number(value)
    if not 0 < c['collapse_ratio'] < 1:
        raise ValueError('Invalid collapse ratio')
    positive_number(c['busy_warning_seconds'])
    validate_policy(c)
    return c


def safe_error(exc):
    # Never expose a connection string, API token or unbounded native response.
    text = str(exc)
    text = re.sub(r'\b(?:https?|postgres(?:ql)?)://\S+', '[redacted URL]', text)
    text = re.sub(r'(?i)(token|password|secret|api[_-]?key)\s*[=:]\s*\S+', r'\1=[redacted]', text)
    return {'kind': type(exc).__name__, 'message': text[:500]}


@contextmanager
def outer_lock(path=LOCK, now=utc_now):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            try:
                owner = json.load(handle)
                age = max(0, (now() - datetime.fromisoformat(owner['started_at'])).total_seconds())
            except (ValueError, KeyError, TypeError):
                owner, age = {}, None
            yield {'acquired': False, 'owner_started_at': owner.get('started_at'), 'busy_seconds': age}
            return
        handle.seek(0); handle.truncate()
        json.dump({'started_at': now().isoformat()}, handle); handle.flush()
        try:
            yield {'acquired': True, 'busy_seconds': 0}
        finally:
            # The inode must remain. Unlinking a locked file permits two locks.
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def source_request(source, request):
    """Validate current endpoint contracts without changing acquisition filters."""
    from app import main
    models = dict(zip(LIGHT + HEAVY, [main.SmartRecruitersIngestionRequest, main.GreenhouseIngestionRequest,
        main.WorkableIngestionRequest, main.AshbyIngestionRequest, main.LeverIngestionRequest,
        main.WorkdayIngestionRequest, main.SuccessFactorsIngestionRequest]))
    request = dict(request or {})
    if source == 'successfactors_direct':
        from app.successfactors_scopes import load_scope_gate
        gate = load_scope_gate()  # Always fail closed on absent/invalid gate.
        if request.get('host'):
            # Bounded operator validation is permitted only inside the gate;
            # explicit reviewed ingestion applies the same native/locale rule.
            if set(request) - {'host', 'brand'} or not any(
                    (r.host, r.brand) == (request['host'], request.get('brand', '')) for r in gate.scopes):
                raise ValueError('Bounded RMK request must match one reviewed host/brand, with no locale override')
            request.setdefault('brand', '')
        else:
            if request not in ({}, {'validated_scopes': True}):
                raise ValueError('RMK requires validated scopes; unrestricted mode is forbidden')
            request = {'validated_scopes': True}
    model = models[source]
    if set(request) - set(model.model_fields):
        raise ValueError('Unknown ingestion request field')
    validated = model.model_validate(request).model_dump(exclude_none=True)
    if source == 'smartrecruiters_direct' and validated['country'].lower() != 'cz':
        raise ValueError('Freshness runner only supports the reviewed SmartRecruiters CZ inventory')
    return validated


def baseline_from_runs(group, requests, runs):
    """Seed first-run volume alarms from comparable successful native history.

    This comparison never authorizes reconciliation. Old metadata may not encode
    every past discovery limit, so registry changes require anomaly review.
    """
    filters = {'company_identifier', 'board_token', 'tenant_slug', 'board_name', 'site',
               'instance', 'host', 'tenant', 'brand', 'locale', 'max_companies', 'max_boards', 'max_sites'}
    rows = []
    for source, wanted in requests.items():
        candidates = []
        for run in runs:
            m = run.get('metadata') or {}
            if run['source_name'] != source or run['status'] != 'success' or run['records_failed']:
                continue
            if any(m.get(k) for k in ('site_errors', 'board_errors', 'tenant_errors', 'unresolved_sites', 'failed_sites')):
                continue
            if any(m.get(k) != wanted.get(k) for k in filters):
                continue
            if source == 'successfactors_direct' and m.get('validated_scopes') != wanted.get('validated_scopes'):
                continue
            if source == 'smartrecruiters_direct' and m.get('country', 'cz') != wanted['country']:
                continue
            candidates.append(run)
        if candidates:
            latest = max(candidates, key=lambda r: r['started_at'])
            rows.append({'source_name': source, 'request': wanted, 'ingestion_status': 'success', 'fetched': latest['records_fetched']})
            known = sorted(h['scope'] for h in (latest.get('metadata') or {}).get('scope_health', [])
                           if h.get('expected_gap') is True)
            if known:
                rows[-1]['known_gap_scopes'] = known
    return {'schema_version': 'freshness-run-v1', 'runner_type': group, 'sources': rows}


def database_baseline(group, requests):
    import os
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row, connect_timeout=15,
                        options='-c default_transaction_read_only=on') as conn:
        conn.read_only = True
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        assert conn.execute('show transaction_read_only').fetchone()['transaction_read_only'] == 'on'
        conn.execute("set local statement_timeout='45s'")
        runs = conn.execute('''select source_name,status,started_at,records_fetched,records_failed,metadata
            from public.ingestion_runs where source_name=any(%s)''', (list(requests),)).fetchall()
        return baseline_from_runs(group, requests, runs)


def execute_source(source, request):
    module = importlib.import_module('app.' + source.removesuffix('_direct') + '_ingestion')
    fn = getattr(module, 'ingest_' + source.removesuffix('_direct') + '_jobs')
    # Request models include only existing function arguments.
    return fn(**request)


def count(result, key, default=None):
    v = result.get(key, default)
    if type(v) is not int or v < 0:
        raise ValueError('Missing/invalid counter: ' + key)
    return v


def normalize_outcome(source, result, previous=None, collapse_ratio=0.2, now=None):
    if not isinstance(result, dict) or result.get('status') not in {'success', 'failed'} or not result.get('run_id'):
        raise ValueError('Malformed ingestion outcome')
    requested_key = 'tenants' if source in (LIGHT[0], LIGHT[2]) else 'boards' if source in (LIGHT[1], LIGHT[3]) else 'sites'
    requested = count(result, requested_key)
    lifecycle = result.get('lifecycle')
    if not isinstance(lifecycle, dict) or not isinstance(lifecycle.get('scopes'), list):
        raise ValueError('Outcome lacks lifecycle scope diagnostics')
    scopes = lifecycle['scopes']
    if len(scopes) > requested or any(type(s.get('successful')) is not bool or type(s.get('complete')) is not bool for s in scopes):
        raise ValueError('Invalid lifecycle scope diagnostics')
    if any(s['complete'] and not s['successful'] or count(s, 'deactivated', 0) and not s['complete'] for s in scopes):
        raise ValueError('Unsafe/inconsistent lifecycle outcome')
    health = result.get('scope_health', [])
    if not isinstance(health, list) or any(not isinstance(h, dict) for h in health):
        raise ValueError('Invalid scope-health diagnostics')
    if len({tuple(h.get('scope', [])) for h in health}) != len(health):
        raise ValueError('Duplicate scope-health diagnostics')
    gaps = []
    if source == 'lever_direct':
        from app.lever_scope_health import accepted_gaps
        gaps = accepted_gaps(health, scopes, now=now)
    gap_scopes = sorted(g['scope'] for g in gaps)
    errors = []
    for key in ('tenant_errors', 'board_errors', 'site_errors'):
        for error in result.get(key, []):
            errors.append(safe_error(RuntimeError(json.dumps(error, default=str))))
    failed_records = count(result, 'failed')
    fetched_key = 'raw_rows' if source == 'workable_direct' else 'listing_records' if source in HEAVY else 'fetched'
    complete = sum(s['complete'] for s in scopes)
    successful = sum(s['successful'] for s in scopes)
    deactivated = count(lifecycle, 'deactivated'); reactivated = count(lifecycle, 'reactivated')
    if deactivated != sum(count(s, 'deactivated', 0) for s in scopes) or reactivated != sum(count(s, 'reactivated', 0) for s in scopes):
        raise ValueError('Lifecycle aggregate does not match scopes')
    out = {'source_name': source, 'run_id': result['run_id'], 'attempted': True,
        'ingestion_status': result['status'], 'scopes_attempted': requested, 'scopes_reported': len(scopes),
        'scopes_completed': successful, 'scopes_complete': complete,
        'scopes_failed': sum(not s['successful'] for s in scopes),
        'scopes_incomplete': sum(s['successful'] and not s['complete'] for s in scopes),
        'scopes_unreported': requested - len(scopes), 'scopes': scopes,
        'fetched': count(result, fetched_key), 'created': count(result, 'created'),
        'updated': count(result, 'updated'), 'attached': count(result, 'attached'),
        'deactivated': deactivated, 'reactivated': reactivated,
        'canonical_closed': lifecycle.get('canonical_closed'),
        'failed_records': failed_records, 'errors': errors, 'anomalies': [], 'timeout': False}
    out.update(scope_health=health, known_gaps=gaps, known_gap_scopes=gap_scopes,
               scopes_known_gaps=len(gaps), scopes_unexpected_uncomplete=requested - complete - len(gaps),
               coverage_degraded=complete != requested,
               volume_comparison='unavailable')
    out.update(scope_attempts=result.get('scope_attempts', []),
               retry_metrics=result.get('retry_metrics', dict.fromkeys(METRICS, 0)))
    if requested == 0:
        out['anomalies'].append('no_scopes_requested')
    if not complete and requested != len(gaps):
        out['anomalies'].append('no_authoritative_complete_scopes')
    if source in HEAVY and result.get('site_results'):
        from app.heavy_scope_health import evaluate_source
        heavy = evaluate_source(source, result, now)
        if any(h['native_inventory_complete'] and not h['lifecycle_eligible'] for h in heavy['scopes']):
            raise ValueError('Unsafe authoritative HEAVY inventory')
        gap_scopes = sorted(g['scope'] for g in heavy['known_gaps'])
    else:
        heavy = None
    comparable = previous and previous.get('known_gap_scopes', []) == gap_scopes
    if previous and not comparable:
        out['volume_comparison'] = 'not_comparable_coverage_changed'
    if comparable and previous.get('fetched', 0) > 0:
        out['volume_comparison'] = 'comparable'
        if out['fetched'] == 0:
            out['anomalies'].append('previously_nonempty_source_now_zero')
        elif out['fetched'] < previous['fetched'] * collapse_ratio:
            out['anomalies'].append('unexpected_result_collapse')
    if result['status'] == 'failed' or requested and not successful and requested != len(gaps):
        out['status'] = 'FAILED'
    elif complete + len(gaps) != requested or errors or failed_records or out['anomalies'] or any(h.get('alert_worthy') for h in health):
        out['status'] = 'PARTIAL'
    elif gaps:
        out['status'] = 'SUCCESS_WITH_KNOWN_GAPS'
    else:
        out['status'] = 'SUCCESS'
    if heavy is not None:
        out['heavy_scope_health'] = heavy
        out['known_gaps'] = heavy['known_gaps']
        out['known_gap_scopes'] = sorted(g['scope'] for g in heavy['known_gaps'])
        out['scopes_known_gaps'] = len(heavy['known_gaps'])
        out['scopes_unexpected_uncomplete'] = sum(not h['native_inventory_complete'] and
            (not h['known_gap'] or h['alert_worthy']) for h in heavy['scopes'])
        # An expected lack of global authority is a coverage fact, not an anomaly.
        if heavy['known_gaps'] and len(heavy['known_gaps']) == requested:
            out['anomalies'] = [a for a in out['anomalies'] if a != 'no_authoritative_complete_scopes']
        out['status'] = heavy['status']
        if out['anomalies'] and out['status'] != 'FAILED':
            out['status'] = 'PARTIAL'
    out['alert_worthy'] = out['status'] in {'PARTIAL', 'FAILED'}
    return out


def aggregate_status(sources):
    statuses = [s['status'] for s in sources]
    if statuses and all(s in {'SUCCESS', 'SUCCESS_WITH_KNOWN_GAPS'} for s in statuses):
        return 'SUCCESS_WITH_KNOWN_GAPS' if 'SUCCESS_WITH_KNOWN_GAPS' in statuses else 'SUCCESS'
    return 'PARTIAL' if any(s in {'SUCCESS', 'SUCCESS_WITH_KNOWN_GAPS', 'PARTIAL'} for s in statuses) else 'FAILED'


def next_baseline(previous, result):
    """Preserve healthy comparison data across failures and bounded requests."""
    rows = {(r['source_name'], json.dumps(r.get('request', {}), sort_keys=True)): r
            for r in (previous or {}).get('sources', [])}
    for r in result.get('sources', []):
        heavy_healthy = (r.get('source_name') in HEAVY and
            r.get('heavy_scope_health', {}).get('status') in {'SUCCESS', 'SUCCESS_WITH_KNOWN_GAPS'}
            and r.get('status') in {'SUCCESS', 'SUCCESS_WITH_KNOWN_GAPS'}
            and not r.get('timeout') and not r.get('anomalies'))
        if heavy_healthy or ((r.get('source_name') not in HEAVY or 'heavy_scope_health' not in r)
                and (r.get('ingestion_status') == 'success' and r.get('scopes_completed', 0) > 0
                and r['scopes_completed'] + r.get('scopes_known_gaps', 0) == r.get('scopes_attempted') and not r.get('errors')
                and not r.get('failed_records') and not r.get('timeout')
                and not set(r.get('anomalies', [])) - {'no_authoritative_complete_scopes'})):
            key = r['source_name'], json.dumps(r.get('request', {}), sort_keys=True)
            rows[key] = {k: r[k] for k in ('source_name', 'request', 'ingestion_status', 'fetched')}
            if r.get('known_gap_scopes'):
                rows[key]['known_gap_scopes'] = r['known_gap_scopes']
    return {'schema_version': 'freshness-run-v1', 'runner_type': result['runner_type'],
            'sources': [rows[k] for k in sorted(rows)]}


def run_group(group, config, requests=None, only_source=None, previous=None, execute=execute_source,
              lock_path=LOCK, clock=time.monotonic, now=utc_now, baseline_loader=None):
    start = clock()
    selected = [only_source] if only_source else list(GROUPS[group])
    if any(s not in GROUPS[group] for s in selected):
        raise ValueError('Source is outside runner group')
    requests = requests or {}
    if set(requests) - set(selected):
        raise ValueError('Request does not belong to selected source')
    validated = {s: source_request(s, requests.get(s)) for s in selected}
    if any(s in HEAVY for s in selected):
        from app.heavy_scope_health import load_policy
        load_policy()  # Reject malformed policy before any source transaction.
    result = {'schema_version': 'freshness-run-v1', 'operation_id': str(uuid.uuid4()), 'runner_type': group,
        'started_at': now().isoformat(), 'finished_at': None, 'duration_seconds': 0,
        'sources': [], 'dedupe_enabled': False, 'anomalies': [], 'timeout': False}
    with outer_lock(lock_path, now) as lock:
        result['lock'] = lock
        if not lock['acquired']:
            result['status'] = 'SKIPPED_LOCKED'
            result['sources'] = [{'source_name': s, 'attempted': False, 'status': 'SKIPPED_LOCKED',
                                  'timeout': False, 'errors': []} for s in selected]
            if lock['busy_seconds'] is not None and lock['busy_seconds'] > config['busy_warning_seconds']:
                result['anomalies'].append('lock_busy_beyond_budget')
        else:
            end = start + config['groups'][group]['deadline_seconds']
            if baseline_loader is not None:
                previous = baseline_loader(validated)
            for source in selected:
                if clock() >= end:
                    result['timeout'] = True
                    result['sources'].append({'source_name': source, 'attempted': False,
                        'status': 'FAILED', 'timeout': True, 'errors': [{'kind': 'GroupDeadline', 'message': 'Not attempted: group deadline exhausted'}]})
                    continue
                before = clock()
                source_start = now().isoformat()
                budget = None
                acquisitions = []
                log.info('source started source=%s group=%s', source, group)
                try:
                    with (
                        ingestion_budget(min(end, before + config['source_deadlines_seconds'][source]), clock) as budget,
                        transient_retry_policy(config if source in LIGHT else None) as acquisitions,
                    ):
                        native = execute(source, validated[source])
                    old = next((s for s in (previous or {}).get('sources', []) if s.get('source_name') == source
                                and s.get('ingestion_status') == 'success' and s.get('request') == validated[source]), None)
                    item = normalize_outcome(source, native, old, config['collapse_ratio'], now=now())
                    if clock() >= budget.deadline:
                        item['timeout'] = True
                        item['anomalies'].append('deadline_exceeded_after_normal_return')
                        item['status'] = 'PARTIAL' if budget.commit_succeeded else 'FAILED'
                except Exception as exc:
                    item = {'source_name': source, 'attempted': True, 'status': 'FAILED',
                        'run_id': budget.run_id if budget else None,
                        'timeout': isinstance(exc, DeadlineExceeded) or bool(budget and budget.timed_out),
                        'errors': [safe_error(exc)], 'commit_started': bool(budget and budget.commit_started),
                        'commit_succeeded': bool(budget and budget.commit_succeeded),
                        'requires_operator_check': bool(budget and budget.commit_started and not budget.commit_succeeded)}
                    item.update({k: None for k in ('scopes_attempted', 'scopes_completed', 'scopes_complete',
                        'scopes_failed', 'scopes_incomplete', 'fetched', 'created', 'updated', 'attached',
                        'deactivated', 'reactivated', 'canonical_closed')})
                item.update(request=validated[source], started_at=source_start, finished_at=now().isoformat(), duration_seconds=round(clock() - before, 3))
                if acquisitions:
                    item.update(acquisition_summary(acquisitions, aborted=item['status'] == 'FAILED'))
                item['alert_worthy'] = item['status'] in {'PARTIAL', 'FAILED'}
                result['timeout'] |= item['timeout']
                result['sources'].append(item)
                log.info('source finished source=%s status=%s seconds=%s', source, item['status'], item['duration_seconds'])
                if item.get('requires_operator_check'):
                    # Do not compound a possibly ambiguous COMMIT result.
                    for rest in selected[len(result['sources']):]:
                        result['sources'].append({'source_name': rest, 'attempted': False, 'status': 'FAILED',
                            'timeout': False, 'errors': [{'kind': 'UncertainCommit', 'message': 'Stopped: operator verification required'}]})
                    break
            result['status'] = aggregate_status(result['sources'])
    result['finished_at'] = now().isoformat()
    result['duration_seconds'] = round(clock() - start, 3)
    if result['duration_seconds'] > config['groups'][group]['expected_seconds']:
        result['anomalies'].append('duration_exceeded_expected_budget')
        if result['status'] in {'SUCCESS', 'SUCCESS_WITH_KNOWN_GAPS'}:
            result['status'] = 'PARTIAL'
    result['coverage_degraded'] = any(s.get('coverage_degraded') or s['status'] in {'PARTIAL', 'FAILED'} for s in result['sources'])
    result['retry_metrics'] = {key: sum(s.get('retry_metrics', {}).get(key, 0)
                                      for s in result['sources']) for key in METRICS}
    result['alert_worthy'] = result['status'] in {'PARTIAL', 'FAILED'} or bool(result['anomalies'])
    result['exit_code'] = EXIT_CODES[result['status']]
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('group', choices=GROUPS)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--config', type=Path, default=CONFIG)
    p.add_argument('--total-deadline', type=float)
    p.add_argument('--source-deadline', type=float)
    p.add_argument('--only-source', choices=LIGHT + HEAVY)
    p.add_argument('--request-json', default='{}')
    p.add_argument('--baseline-stdin', action='store_true')
    args = p.parse_args(argv)
    c = load_config(args.config)
    if args.total_deadline is not None:
        c['groups'][args.group]['deadline_seconds'] = positive_number(args.total_deadline)
    if args.source_deadline is not None:
        if not args.only_source:
            p.error('--source-deadline requires --only-source')
        c['source_deadlines_seconds'][args.only_source] = positive_number(args.source_deadline)
    request = json.loads(args.request_json)
    if not isinstance(request, dict) or request and not args.only_source:
        p.error('--request-json requires one selected source')
    sources = [args.only_source] if args.only_source else list(GROUPS[args.group])
    if any(s not in GROUPS[args.group] for s in sources):
        p.error('Source is outside runner group')
    if not args.apply:
        print(json.dumps({'plan_only': True, 'group': args.group, 'sources': sources,
                          'rmk_policy': 'validated scopes only', 'config': c, 'dedupe': False}, indent=2))
        return 0
    previous = json.load(sys.stdin) if args.baseline_stdin else None
    if previous and (previous.get('schema_version') != 'freshness-run-v1' or previous.get('runner_type') != args.group):
        p.error('Baseline must belong to this runner group')
    def load_baseline(validated):
        seed = database_baseline(args.group, validated)
        if previous:
            # Durable cache is preferred over legacy DB volume history.
            cached = {(r['source_name'], json.dumps(r['request'], sort_keys=True)) for r in previous['sources']}
            seed['sources'] = [r for r in seed['sources'] if (r['source_name'], json.dumps(r['request'], sort_keys=True)) not in cached] + previous['sources']
        return seed
    formatter = logging.Formatter('%(asctime)sZ %(levelname)s %(message)s')
    formatter.converter = time.gmtime
    handler = logging.StreamHandler(); handler.setFormatter(formatter)
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    log.setLevel(logging.INFO)
    logging.getLogger('uvicorn.error').setLevel(logging.INFO)
    result = run_group(args.group, c, {args.only_source: request} if args.only_source else {}, args.only_source,
                       previous, baseline_loader=load_baseline)
    print(json.dumps(result, indent=2))
    return result['exit_code']


def entrypoint(argv=None):
    start, started = time.monotonic(), utc_now().isoformat()
    try:
        return main(argv)
    except Exception as exc:
        # Preflight/config/transport failures must not print a traceback with
        # credentials or be mistaken for a clean operational JSON result.
        args = list(argv if argv is not None else sys.argv[1:])
        group = args[0] if args and args[0] in GROUPS else 'unknown'
        print(json.dumps({'schema_version': 'freshness-run-v1', 'runner_type': group,
            'started_at': started, 'finished_at': utc_now().isoformat(),
            'duration_seconds': round(time.monotonic() - start, 3), 'status': 'FAILED', 'exit_code': 1,
            'sources': [], 'lock': {'acquired': False, 'reason': 'preflight_or_orchestration_error'},
            'errors': [safe_error(exc)], 'requires_operator_check': True, 'timeout': False}))
        return 1


if __name__ == '__main__':
    sys.exit(entrypoint())
