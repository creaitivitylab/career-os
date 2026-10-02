#!/usr/bin/env bash

set -euo pipefail

BASE_URL="http://127.0.0.1:8000"

queries=(
  "AI"
  "software"
  "data"
  "product"
  "marketing"
  "sales"
  "finance"
  "HR"
  "administrativa"
  "účetní"
  "řidič"
  "skladník"
  "výroba"
  "technik"
  "elektrikář"
  "stavebnictví"
  "logistika"
  "zdravotní sestra"
  "lékař"
  "zákaznický servis"
  "retail"
  "nákup"
  "projektový manažer"
)

echo
echo "========================================"
echo "JOOBLE DAILY DELTA"
date --iso-8601=seconds
echo "Queries: ${#queries[@]}"
echo "========================================"

for keyword in "${queries[@]}"; do
    echo
    echo "=== $keyword ==="

    curl \
      --fail \
      --silent \
      --show-error \
      --retry 2 \
      --retry-delay 3 \
      -X POST \
      "$BASE_URL/ingest/jooble" \
      -H "Content-Type: application/json" \
      -d "{
        \"keywords\": \"$keyword\",
        \"location\": \"Czech Republic\",
        \"page\": 1,
        \"results_per_page\": 100
      }"

    echo

    sleep 1
done

echo
echo "=== DEDUPE REBUILD ==="

curl \
  --fail \
  --silent \
  --show-error \
  --retry 2 \
  --retry-delay 3 \
  -X POST \
  "$BASE_URL/dedupe/rebuild"

echo

echo
echo "=== SAFE CANDIDATES ==="

curl \
  --fail \
  --silent \
  --show-error \
  "$BASE_URL/dedupe/safe-candidates"

echo
echo

date --iso-8601=seconds
echo "JOOBLE DAILY DELTA FINISHED"
echo "========================================"
