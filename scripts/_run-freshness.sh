#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
GROUP="$1"
shift
APPLY=0
for arg in "$@"; do
    if [[ "$arg" == "--apply" ]]; then APPLY=1; fi
done
if [[ "$APPLY" == 0 ]]; then
    # A default invocation is a local plan: no Docker, database, API or log write.
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/worker" python3 -m app.direct_freshness "$GROUP" "$@"
    exit 0
fi
LOG_DIR="$ROOT/logs/freshness"
mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$-$GROUP"
RESULT="$LOG_DIR/$STAMP.json"
BASELINE="$LOG_DIR/baseline-$GROUP.json"
STATUS=0
if [[ -f "$BASELINE" ]]; then
    docker compose exec -T worker env PYTHONDONTWRITEBYTECODE=1 python -m app.direct_freshness \
        "$GROUP" "$@" --baseline-stdin < "$BASELINE" > "$RESULT" 2> "$LOG_DIR/$STAMP.log" || STATUS=$?
else
    docker compose exec -T worker env PYTHONDONTWRITEBYTECODE=1 python -m app.direct_freshness \
        "$GROUP" "$@" < /dev/null > "$RESULT" 2> "$LOG_DIR/$STAMP.log" || STATUS=$?
fi
# The worker process owns flock for its entire execution. Losing this shell/SSH
# cannot release that lock while an ingestion is still running inside Docker.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/worker" python3 - "$RESULT" "$BASELINE" "$STATUS" <<'PY'
import json, os, sys
from pathlib import Path
from app.direct_freshness import next_baseline, EXIT_CODES
path, baseline, status = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
try:
    result = json.loads(path.read_text())
    assert result['schema_version'] == 'freshness-run-v1'
    assert result['exit_code'] == status == EXIT_CODES[result['status']]
except (ValueError, KeyError, AssertionError):
    print('FAILED: incomplete/invalid operational result; inspect logs and ingestion_runs before any retry.', file=sys.stderr)
    sys.exit(1)
if result['status'] != 'SKIPPED_LOCKED':
    previous = json.loads(baseline.read_text()) if baseline.exists() else None
    target = baseline.with_name(baseline.name + '.' + str(os.getpid()) + '.tmp')
    target.write_text(json.dumps(next_baseline(previous, result), indent=2) + '\n')
    target.replace(baseline)
if result.get('alert_worthy', result['status'] in ('PARTIAL', 'FAILED')):
    print('ALERT: freshness execution requires operator review; inspect ' + str(path), file=sys.stderr)
elif result.get('coverage_degraded'):
    print('COVERAGE GAP: reviewed scopes remain unverified; inspect ' + str(path), file=sys.stderr)
print(json.dumps({'status': result['status'], 'duration_seconds': result['duration_seconds'],
    'coverage_degraded': result.get('coverage_degraded', False),
    'alert_worthy': result.get('alert_worthy', result['status'] in ('PARTIAL', 'FAILED')),
    'summary': str(path)}))
PY
exit "$STATUS"
