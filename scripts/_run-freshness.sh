#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source "$ROOT/scripts/_freshness-worker.sh"
GROUP="${1:?Runner group required}"
shift
case "$GROUP" in light|heavy|all) ;; *) printf 'FAILED: invalid runner group\n' >&2; exit 1 ;; esac
LOG_DIR="$ROOT/logs/freshness"
mkdir -p "$LOG_DIR"
# Keep this inode: kernel locks, not stale PID files, determine ownership.
# The worker retains its own lock if this shell/SSH connection disappears.
exec {LOCK_FD}>"$LOG_DIR/runner.lock"
LOCK_STATUS=0
flock -n -E 75 "$LOCK_FD" || LOCK_STATUS=$?
if [[ "$LOCK_STATUS" == 75 ]]; then
    printf '{"schema_version":"freshness-run-v1","runner_type":"%s","status":"SKIPPED_LOCKED","exit_code":75,"duration_seconds":0,"sources":[],"dedupe_enabled":false,"lock":{"acquired":false,"location":"host"}}\n' "$GROUP"
    exit 75
fi
if [[ "$LOCK_STATUS" != 0 ]]; then
    printf 'FAILED: host freshness lock could not be acquired\n' >&2
    exit 1
fi
freshness_worker
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$-$GROUP"
TARGET="$LOG_DIR/runs/$STAMP"
BASELINE="$LOG_DIR/baseline-$GROUP.json"
freshness_execute runner "$GROUP" "$TARGET" "$BASELINE" "$@"
