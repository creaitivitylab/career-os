#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
TARGET="$ROOT/logs/freshness/reports/$STAMP"
mkdir -p "$TARGET"
# Worker inherits the existing Compose env_file. Never source/print .env in shell.
docker compose exec -T worker env PYTHONDONTWRITEBYTECODE=1 python -m app.freshness_report \
    --output-dir "/tmp/career-os-freshness-report-$STAMP" "$@" > "$TARGET/command.json" 2> "$TARGET/command.log"
CONTAINER="$(docker compose ps -q worker)"
docker cp "$CONTAINER:/tmp/career-os-freshness-report-$STAMP/." "$TARGET/"
printf 'Freshness report: %s\n' "$TARGET"
