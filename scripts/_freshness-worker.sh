#!/usr/bin/env bash
# Sourced by shell entrypoints. Application Python runs ONLY in the worker.
freshness_worker() {
    CONTAINER="$(docker compose ps --status running -q worker)" || {
        printf 'FAILED: cannot locate Compose worker; no host Python fallback\n' >&2
        return 1
    }
    if [[ -z "$CONTAINER" || "$CONTAINER" == *$'\n'* ]]; then
        printf 'FAILED: exactly one running Compose worker is required; no host Python fallback\n' >&2
        return 1
    fi
}

freshness_execute() {
    local mode="$1" group="$2" target="$3" baseline="$4"
    shift 4
    local runtime="$ROOT/scripts/freshness-runtime.py"
    local remote="/tmp/career-os-freshness-runtime-$(date -u +%Y%m%dT%H%M%SZ)-$$-$mode-$group"
    local status codes
    mkdir -p "$target/artifacts"
    # Length-framed operational code followed by baseline JSON. Neither is
    # interpreted on the host. All imports use the deployed worker's /app.
    set +e
    {
        wc -c < "$runtime"
        cat "$runtime"
        if [[ -n "$baseline" && -f "$baseline" ]]; then cat "$baseline"; else printf 'null\n'; fi
    } | docker exec -i -w /app "$CONTAINER" env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app \
        python -c 'import sys; n=int(sys.stdin.buffer.readline()); code=sys.stdin.buffer.read(n); exec(compile(code, "<freshness-runtime>", "exec"))' \
        "$mode" "$remote" "$group" "$@" \
        2> "$target/command.log" | tee "$target/command.stdout"
    codes=("${PIPESTATUS[@]}")
    set -e
    status="${codes[1]}"
    cat "$target/command.log" >&2
    if [[ "${codes[0]}" != 0 || "${codes[2]}" != 0 ]]; then
        printf 'FAILED: operational input/log transport failed; inspect state before any retry. Logs: %s\n' "$target" >&2
        return 1
    fi
    # Copy using the SAME resolved container, never a newly recreated worker.
    # Raw stdout/stderr above survive even if validation/copy fails after commit.
    if ! docker cp "$CONTAINER:$remote/." "$target/artifacts/"; then
        printf 'FAILED: cannot recover worker artifacts; inspect state before any retry. Logs: %s\n' "$target" >&2
        return 1
    fi
    if [[ ! -f "$target/artifacts/validated-$status" ]]; then
        printf 'FAILED: worker workflow did not validate; inspect state before any retry. Logs: %s\n' "$target" >&2
        return 1
    fi
    if [[ -n "$baseline" && -f "$target/artifacts/baseline.json" ]]; then
        # Host lock remains held through atomic durable baseline promotion.
        cp "$target/artifacts/baseline.json" "$baseline.$$.tmp"
        mv "$baseline.$$.tmp" "$baseline"
    fi
    printf 'Freshness %s artifacts: %s\n' "$mode" "$target" >&2
    return "$status"
}
