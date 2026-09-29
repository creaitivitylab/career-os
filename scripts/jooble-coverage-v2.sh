#!/usr/bin/env bash

set -euo pipefail

BASE_URL="http://127.0.0.1:8000"

queries=(
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

echo "===== Jooble coverage V2 ====="
date
echo

for keyword in "${queries[@]}"; do

    echo "=== $keyword ==="

    curl \
      --fail \
      --silent \
      --show-error \
      -X POST \
      "$BASE_URL/ingest/jooble" \
      -H "Content-Type: application/json" \
      -d "{
        \"keywords\": \"$keyword\",
        \"location\": \"Czech Republic\",
        \"page\": 2,
        \"results_per_page\": 100
      }"

    echo
    echo

    sleep 1

done

echo "===== Finished ====="
date
