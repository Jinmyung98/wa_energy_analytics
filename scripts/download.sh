#!/usr/bin/env bash
# Download every input this project uses into data/ (gitignored).
# No auth, no keys. Safe to re-run: existing valid files are skipped.
#
# Usage:  bash scripts/download.sh
set -euo pipefail

BASE=https://data.wa.aemo.com.au/public/public-data/datafiles
RAW=data/raw
YEARS="2023 2024 2025 2026"

mkdir -p "$RAW"

get() {  # get <url-path> <local-name>
  if [ -s "$RAW/$2" ]; then
    echo "  skip  $2"
    return 0
  fi
  echo "  get   $2"
  curl -sS --fail --retry 3 --retry-delay 2 -o "$RAW/$2" "$BASE/$1"
}

echo "Core interval series (5-min demand, 5-min DPV, 30-min price)"
for y in $YEARS; do
  get "operational-demand-withdrawal-csv/OperationalDemandWithdrawal-$y.csv" "OperationalDemandWithdrawal-$y.csv"
  get "estimated-dpv-csv/distributed-pv-new-$y.csv"                          "distributed-pv-new-$y.csv"
  get "reference-trading-price-csv/ReferenceTradingPrice-$y.csv"             "ReferenceTradingPrice-$y.csv"
done

echo "Supporting reference data"
get "post-facilities/facilities.csv" "facilities.csv"
for y in 2024 2025 2026; do
  get "facility-temperature/facility-temperature-$y.csv" "facility-temperature-$y.csv"
done

echo "Storage facility SCADA (streamed and filtered; see script for why)"
bash scripts/fetch_bess_scada.sh

echo
echo "Done. Raw data in $RAW ($(du -sh "$RAW" | cut -f1))."
echo "Run 'python scripts/audit.py' to reproduce the data-quality audit."
