#!/usr/bin/env bash

set -euo pipefail

cd /home/careeros/apps/career-os

echo "===== Career OS daily ingest ====="
date

echo
echo "--- Fantastic Jobs ---"

curl \
  --fail \
  --silent \
  --show-error \
  --retry 3 \
  --retry-delay 10 \
  -X POST \
  http://127.0.0.1:8000/ingest/fantastic \
  -H "Content-Type: application/json" \
  -d '{
    "time_range": "24h",
    "location": "Czechia",
    "limit": 5000
  }'

echo
echo
echo "--- Duplicate rebuild ---"

curl \
  --fail \
  --silent \
  --show-error \
  --retry 3 \
  -X POST \
  http://127.0.0.1:8000/dedupe/rebuild

echo
echo
echo "--- Safe candidates ---"

curl \
  --fail \
  --silent \
  --show-error \
  http://127.0.0.1:8000/dedupe/safe-candidates

echo
echo
echo "===== Finished ====="
date
