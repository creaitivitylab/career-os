#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source "$ROOT/scripts/_freshness-worker.sh"
freshness_worker
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
TARGET="$ROOT/logs/freshness/reports/$STAMP"
freshness_execute report report "$TARGET" '' "$@"
