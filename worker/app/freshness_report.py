"""Read-only source freshness report; never changes canonical activity flags."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import statistics

import psycopg
from psycopg.rows import dict_row

from app.direct_freshness import LIGHT, HEAVY

DIRECT = set(LIGHT + HEAVY)
WINDOWS = (24, 72, 168, 336, 720)


def dt(value):
    if value is None or isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def newest(values):
    return max((dt(v) for v in values if v is not None), default=None)


def composition(sources):
    names = {s['source_name'] for s in sources}
    if names & DIRECT:
        return 'direct_plus_aggregator' if names - DIRECT else 'direct_only'
    if names == {'fantastic_jobs_apify'}:
        return 'fantastic_only'
    if names == {'jooble_direct'}:
        return 'jooble_only'
    if names == {'fantastic_jobs_apify', 'jooble_direct'}:
        return 'fantastic_plus_jooble'
    return 'other'


def bounded_run(run):
    m = run.get('metadata') or {}
    return any(m.get(k) is not None for k in ('company_identifier', 'board_token', 'tenant_slug', 'board_name',
               'site', 'host', 'max_companies', 'max_boards', 'max_sites'))


def proven_scope(source, runs, state):
    """Metadata must prove exact scope ownership, not merely an HTTP success."""
    life = source.get('lifecycle') or {}
    run = runs.get(str(life.get('run_id')))
    if (source['source_name'] not in DIRECT or not run or run['source_name'] != source['source_name']
            or run['status'] != 'success' or life.get('inventory_complete') is not True
            or life.get('state') != state):
        return False
    if source['source_name'] in HEAVY and bounded_run(run):
        return False  # Never promote a recovery slice to authoritative verification.
    scope = life.get('scope')
    tokens = source['source_job_id'].split(':')
    name = source['source_name']
    if not isinstance(scope, list) or not scope:
        return False
    if name == 'workday_direct':
        native = source.get('native_scope') or {}
        owned = len(scope) == 3 and [native.get('host'), *tokens[:2]] == scope
    elif name == 'successfactors_direct':
        native = source.get('native_scope') or {}
        owned = len(scope) == 3 and [native.get('host'), tokens[0], native.get('brand')] == scope
    else:
        owned = tokens[:-1] == scope
        if name == 'smartrecruiters_direct':
            owned &= str(source.get('native_country') or '').lower() == 'cz'
    scopes = ((run.get('metadata') or {}).get('lifecycle') or {}).get('scopes', [])
    return owned and any(s.get('scope') == scope and s.get('complete') is True and s.get('successful') is True for s in scopes)


def classify(job, sources, runs, now, native_hours=72):
    reasons = []
    if not sources:
        return 'NEEDS_VERIFICATION', ['no_sources']
    direct = [s for s in sources if s['source_name'] in DIRECT]
    active = [s for s in direct if s['is_active']]
    withdrawn = [s for s in direct if not s['is_active'] and proven_scope(s, runs, 'absent')]
    providers = [s for s in sources if s['source_name'] not in DIRECT]
    if len(withdrawn) == len(sources) and job['status'] in {'inactive', 'expired'}:
        return 'CONFIRMED_INACTIVE', ['all_authoritative_sources_confirmed_absent']
    if withdrawn and providers and not active:
        reasons.append('direct_withdrawn_but_aggregator_present')
    verified = [s for s in active if proven_scope(s, runs, 'observed') and
                dt(s.get('last_seen_at')) is not None and
                dt(s['last_seen_at']) >= now - timedelta(hours=native_hours) and
                dt(s.get('last_verified_at')) is not None and
                dt(s['last_verified_at']) >= now - timedelta(hours=native_hours)]
    expiry = dt(job.get('expires_at'))
    if verified and job['status'] == 'active':
        return 'VERIFIED_ACTIVE', ['recent_complete_native_scope_evidence'] + (['expiry_overridden_by_verified_native'] if expiry and expiry < now else [])
    if not direct:
        reasons.append('discovery_only')
    if active and all(dt(s.get('last_verified_at')) is None or dt(s['last_verified_at']) < now - timedelta(hours=native_hours) for s in active):
        reasons.append('direct_evidence_stale')
    if active and not any(proven_scope(s, runs, 'observed') for s in active):
        reasons.append('authoritative_completeness_unproven')
    if expiry and expiry < now:
        reasons.append('expired_timestamp_unconfirmed')
    if job['status'] != 'active' and any(s['is_active'] for s in sources):
        reasons.append('canonical_source_activity_conflict')
    return 'NEEDS_VERIFICATION', reasons + ['no_recent_native_verification']


def full_complete(run):
    m = run.get('metadata') or {}
    life = m.get('lifecycle') or {}
    scopes = life.get('scopes') or []
    requested = next((m[k] for k in ('tenant_count', 'board_count', 'site_count', 'sites') if k in m), None)
    return (run['status'] == 'success' and not bounded_run(run) and requested is not None and requested > 0
            and len(scopes) == requested and all(s.get('successful') is True and s.get('complete') is True for s in scopes))


def generate_report(jobs, sources, runs, now, native_hours=72):
    now = dt(now)
    run_index = {str(r['id']): r for r in runs}
    by_job, by_source = defaultdict(list), defaultdict(list)
    for source in sources:
        by_job[str(source['job_id'])].append(source)
        by_source[source['source_name']].append(source)
    states = Counter(j['status'] for j in jobs)
    classes = Counter()
    reasons = Counter()
    job_results = []
    age = defaultdict(lambda: {'active_jobs': 0, 'no_seen': {str(h): 0 for h in WINDOWS},
                              'no_verified': {str(h): 0 for h in WINDOWS}})
    for job in sorted(jobs, key=lambda j: str(j['id'])):
        support = by_job[str(job['id'])]
        group = composition(support)
        state, why = classify(job, support, run_index, now, native_hours)
        classes[state] += 1; reasons.update(why)
        job_results.append({'job_id': str(job['id']), 'canonical_status': job['status'], 'composition': group,
                            'freshness_status': state, 'reasons': why})
        if job['status'] != 'active':
            continue
        # Negative verification of an inactive source never counts as support.
        active_support = [s for s in support if s['is_active']]
        for key, field in [('no_seen', 'last_seen_at'), ('no_verified', 'last_verified_at')]:
            last = newest(s.get(field) for s in active_support)
            for h in WINDOWS:
                if last is None or last < now - timedelta(hours=h):
                    age[group][key][str(h)] += 1
        age[group]['active_jobs'] += 1
    family = []
    complete_sources = []
    recent_lifecycle = []
    for name in sorted(set(by_source) | DIRECT | {'fantastic_jobs_apify', 'jooble_direct'}):
        rows = by_source[name]; active = [s for s in rows if s['is_active']]
        history = sorted((r for r in runs if r['source_name'] == name), key=lambda r: dt(r['started_at']))
        successes = [r for r in history if r['status'] == 'success']
        complete = [r for r in history if full_complete(r)] if name in DIRECT else []
        authoritative_scopes = []
        for r in history:
            life = (r.get('metadata') or {}).get('lifecycle') or {}
            checked = [s for s in life.get('scopes', []) if s.get('successful') is True and s.get('complete') is True]
            if checked and r['status'] == 'success' and not (name in HEAVY and bounded_run(r)):
                authoritative_scopes.append({'run_id': str(r['id']), 'finished_at': r.get('finished_at'), 'scopes': [s['scope'] for s in checked]})
            if life and dt(r['started_at']) >= now - timedelta(days=7):
                recent_lifecycle.append({'source_name': name, 'run_id': str(r['id']),
                    'complete_scopes': len(checked), 'deactivated': life.get('deactivated', 0),
                    'reactivated': life.get('reactivated', 0), 'canonical_closed': life.get('canonical_closed')})
        def median_age(field):
            values = [(now - dt(s[field])).total_seconds() / 86400 for s in active if s.get(field)]
            return round(statistics.median(values), 3) if values else None
        family.append({'source_name': name, 'total': len(rows), 'active': len(active), 'inactive': len(rows) - len(active),
            'distinct_canonical_jobs': len({str(s['job_id']) for s in rows}),
            'newest_seen': newest(s['last_seen_at'] for s in rows), 'newest_verified': newest(s.get('last_verified_at') for s in rows),
            'oldest_active_seen': min((dt(s['last_seen_at']) for s in active), default=None),
            'oldest_active_verified': min((dt(s['last_verified_at']) for s in active if s.get('last_verified_at')), default=None),
            'median_active_seen_days': median_age('last_seen_at'), 'median_active_verified_days': median_age('last_verified_at'),
            'latest_success': {'run_id': str(successes[-1]['id']), 'started_at': successes[-1]['started_at'], 'finished_at': successes[-1].get('finished_at')} if successes else None})
        if name in DIRECT:
            complete_sources.append({'source_name': name, 'class': 'light' if name in LIGHT else 'heavy',
                'latest_complete_unbounded_run': {'run_id': str(complete[-1]['id']), 'finished_at': complete[-1].get('finished_at')} if complete else None,
                'latest_authoritative_complete_scope_verification': authoritative_scopes[-1] if authoritative_scopes else None,
                'complete_scope_verification_is_not_full_population': True})
    current_absence = [s for s in sources if not s['is_active'] and proven_scope(s, run_index, 'absent')]
    totals = {'active_jobs': sum(v['active_jobs'] for v in age.values()),
        **{key: {str(h): sum(v[key][str(h)] for v in age.values()) for h in WINDOWS} for key in ('no_seen', 'no_verified')}}
    report = {'schema_version': 'freshness-report-v1', 'snapshot_at': now.isoformat(),
        'canonical': {'total': len(jobs), 'active': states['active'], 'inactive': states['inactive'], 'status_distribution': dict(sorted(states.items()))},
        'source_rows': {'total': len(sources), 'active': sum(s['is_active'] for s in sources), 'inactive': sum(not s['is_active'] for s in sources)},
        'sources': family, 'active_age_by_composition': dict(sorted(age.items())), 'active_age_totals': totals,
        'direct_verification': complete_sources, 'derived_status_counts': {k: classes[k] for k in ('VERIFIED_ACTIVE', 'CONFIRMED_INACTIVE', 'NEEDS_VERIFICATION')},
        'derived_subreason_counts_overlapping': dict(sorted(reasons.items())),
        'lifecycle': {'last_7d_runs': recent_lifecycle,
            'deactivated_last_7d': sum(r['deactivated'] for r in recent_lifecycle),
            'reactivated_last_7d': sum(r['reactivated'] for r in recent_lifecycle),
            'canonical_closure_events_last_7d': sum(r['canonical_closed'] for r in recent_lifecycle) if recent_lifecycle and all(r['canonical_closed'] is not None for r in recent_lifecycle) else None,
            'currently_closed_with_complete_absence_evidence': classes['CONFIRMED_INACTIVE'],
            'currently_inactive_sources_with_complete_absence_evidence': len(current_absence),
            'closure_event_history_available': False},
        'jobs': job_results,
        'semantics': {'native_verification_budget_hours': native_hours,
            'aggregator_verified_timestamp_is_observation_only': True,
            'filtered_heavy_runs_are_not_authoritative_complete_verification': True,
            'legacy_finished_at_may_use_transaction_start': True,
            'canonical_activity_not_modified': True,
            'status_is_not_market_size': True}}
    return json.loads(json.dumps(report, default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value)))


def collect_report(database_url, native_hours=72):
    with psycopg.connect(database_url, row_factory=dict_row, connect_timeout=15,
                        options='-c default_transaction_read_only=on') as conn:
        conn.read_only = True
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.execute("set local statement_timeout='45s'")
        assert conn.execute('show transaction_read_only').fetchone()['transaction_read_only'] == 'on'
        now = conn.execute('select now() as snapshot').fetchone()['snapshot']
        jobs = conn.execute('select id,status,expires_at from public.jobs order by id').fetchall()
        sources = conn.execute("""select job_id,source_name,source_job_id,is_active,last_seen_at,last_verified_at,
            raw_payload->'_lifecycle' as lifecycle,
            case when source_name='workday_direct' then raw_payload->'workday_site'
                 when source_name='successfactors_direct' then raw_payload->'rmk_site' end as native_scope,
            raw_payload->'location'->>'country' as native_country
            from public.job_sources order by source_name,source_job_id""").fetchall()
        runs = conn.execute('select id,source_name,status,started_at,finished_at,metadata from public.ingestion_runs order by started_at,id').fetchall()
        return generate_report(jobs, sources, runs, now, native_hours)


def markdown(report):
    c, s = report['canonical'], report['source_rows']
    lines = ['# Career OS read-only freshness report', '', 'Snapshot UTC: ' + report['snapshot_at'], '',
        f"Canonical: {c['total']} total / {c['active']} active / {c['inactive']} inactive.",
        f"Sources: {s['total']} total / {s['active']} active / {s['inactive']} inactive.", '',
        'Stored activity is not verified market size. Aggregator retrieval is discovery evidence.', '',
        '| Source | Total | Active | Inactive | Last successful start UTC | Median active evidence age (days) |',
        '| --- | ---: | ---: | ---: | --- | ---: |']
    for row in report['sources']:
        lines.append(f"| {row['source_name']} | {row['total']} | {row['active']} | {row['inactive']} | {(row['latest_success'] or {}).get('started_at', 'unknown')} | {row['median_active_verified_days']} |")
    lines += ['', '## Active supporting evidence age', '', '| Composition | Jobs | No seen ≤24h | ≤72h | ≤7d | ≤14d | ≤30d |', '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for name, row in [*report['active_age_by_composition'].items(), ('TOTAL', report['active_age_totals'])]:
        lines.append('| ' + ' | '.join(map(str, [name, row['active_jobs'], *[row['no_seen'][str(h)] for h in WINDOWS]])) + ' |')
    lines += ['', 'Separate last_verified age counts are retained in JSON; an inactive source is not supporting evidence.', '',
        '## Derived reporting states', '', '```json', json.dumps(report['derived_status_counts'], indent=2), '```', '',
        'Subreasons (overlapping):', '```json', json.dumps(report['derived_subreason_counts_overlapping'], indent=2), '```', '',
        '## Native authoritative completeness', '', '```json', json.dumps(report['direct_verification'], indent=2), '```', '',
        'Bounded complete scopes are not full-population verification. Filtered/partial heavy scopes remain unproven.', '',
        '## Lifecycle', '', '```json', json.dumps(report['lifecycle'], indent=2), '```', '',
        'Canonical closure-event counts are unknown for legacy runs without transition metrics; current closed-with-absence counts are not historical event counts.',
        'Legacy run finished_at may predate actual completion because of PostgreSQL now(); use new runner monotonic summaries for duration.', '']
    return '\n'.join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--native-hours', type=float, default=72)
    args = p.parse_args(argv)
    if not 0 < args.native_hours <= 720:
        p.error('Native evidence budget must be between0 and720 hours')
    report = collect_report(os.environ['DATABASE_URL'], args.native_hours)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    (args.output_dir / 'report.md').write_text(markdown(report))
    print(json.dumps({k: report[k] for k in ('snapshot_at', 'canonical', 'source_rows', 'derived_status_counts')}, indent=2))
    return 0


if __name__ == '__main__':
    main()
