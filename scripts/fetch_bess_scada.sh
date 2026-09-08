#!/usr/bin/env bash
# Fetch per-facility SCADA for storage facilities only.
#
# Full FacilityScada is ~27 MB/month x 35 months (~950 MB) and we need 7 of the
# ~176 facilities, so each month is streamed through grep and only storage rows
# are kept. Output files have NO header (src/wa_data.load_facility_scada supplies
# one).
#
# Downloads are validated: a truncated stream leaves a malformed final line, so
# each file is checked and re-fetched up to MAX_TRIES times.
set -u

BASE=https://data.wa.aemo.com.au/public/public-data/datafiles/facility-scada-csv
OUT=${1:-data/raw/scada_bess}
START=2023-10   # WEM reform: SCADA history begins 2023-09-26
END=2026-08
MAX_TRIES=4
PARALLEL=6

mkdir -p "$OUT"

# A well-formed final line looks like:
#   "01/03/2026 07:55:00","COLLIE_BESS2",-12.345,
ROW_RE='^"[0-9]{2}/[0-9]{2}/[0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2}","[A-Z0-9_]+",-?[0-9.]+,'

valid() {
  [ -s "$1" ] || return 1
  tail -1 "$1" | grep -qE "$ROW_RE"
}

fetch_month() {
  m=$1
  f="$OUT/bess-$m.csv"
  valid "$f" && return 0
  t=1
  while [ $t -le $MAX_TRIES ]; do
    curl -sS --retry 3 --retry-delay 2 --max-time 300 \
      "$BASE/FacilityScada-$m.csv" | grep -E '_(BESS|ESR)[0-9]*",' > "$f"
    valid "$f" && return 0
    echo "  retry $t/$MAX_TRIES: $m (truncated)" >&2
    t=$((t + 1))
  done
  echo "  FAILED: $m" >&2
  rm -f "$f"
  return 1
}

months=""
for y in 2023 2024 2025 2026; do
  for mo in 01 02 03 04 05 06 07 08 09 10 11 12; do
    m="$y-$mo"
    [ "$m" \< "$START" ] && continue
    [ "$m" \> "$END" ] && continue
    months="$months $m"
  done
done

i=0
for m in $months; do
  fetch_month "$m" &
  i=$((i + 1))
  [ $((i % PARALLEL)) -eq 0 ] && wait
done
wait

echo "--- validating ---"
bad=0
for m in $months; do
  valid "$OUT/bess-$m.csv" || { echo "INVALID/MISSING: $m"; bad=$((bad + 1)); }
done
echo "months ok: $(( $(echo $months | wc -w) - bad )) / $(echo $months | wc -w)"
[ "$bad" -eq 0 ] || exit 1
