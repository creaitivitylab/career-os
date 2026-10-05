#!/usr/bin/env bash

set -u

BASE_URL="http://127.0.0.1:8000"

run_source() {
    NAME="$1"
    ENDPOINT="$2"
    PAYLOAD="${3:-}"
    if [ -z "$PAYLOAD" ]; then
        PAYLOAD='{}'
    fi

    echo
    echo "=================================================="
    echo "$NAME"
    echo "=================================================="

    RESPONSE="$(
        curl -sS \
            --fail-with-body \
            -X POST \
            "$BASE_URL$ENDPOINT" \
            -H "Content-Type: application/json" \
            -d "$PAYLOAD" \
            2>&1
    )"

    STATUS=$?

    if [ "$STATUS" -ne 0 ]; then
        echo "FAILED"
        echo "$RESPONSE"
        return 1
    fi

    echo "$RESPONSE" | python3 -m json.tool

    return 0
}


FAILED=0


run_source \
    "SmartRecruiters" \
    "/ingest/ats/smartrecruiters" \
    || FAILED=$((FAILED + 1))


run_source \
    "Greenhouse" \
    "/ingest/ats/greenhouse" \
    || FAILED=$((FAILED + 1))


run_source \
    "Workable" \
    "/ingest/ats/workable" \
    || FAILED=$((FAILED + 1))


run_source \
    "Ashby" \
    "/ingest/ats/ashby" \
    || FAILED=$((FAILED + 1))


run_source \
    "Lever" \
    "/ingest/ats/lever" \
    || FAILED=$((FAILED + 1))


run_source \
    "Workday" \
    "/ingest/ats/workday" \
    || FAILED=$((FAILED + 1))


run_source \
    "SuccessFactors RMK (validated scopes)" \
    "/ingest/ats/successfactors" \
    '{"validated_scopes":true}' \
    || FAILED=$((FAILED + 1))


echo
echo "=================================================="
echo "DIRECT ATS RUN COMPLETE"
echo "FAILED SOURCES: $FAILED"
echo "=================================================="


if [ "$FAILED" -gt 0 ]; then
    exit 1
fi
