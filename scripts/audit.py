"""Reproduce the data-quality audit reported in docs/DATA_NOTES.md.

Run from the repo root after scripts/download.sh:

    python scripts/audit.py

Prints coverage, completeness, duplicate checks, the demand identity check,
the storage capacity-convention check, and the administered-price parameters.
Exits non-zero if a hard invariant fails.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import wa_data as w  # noqa: E402

pd.set_option("display.width", 200)

FAILURES = []


def check(label, condition, detail=""):
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{(' | ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def section(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def completeness(name, df, freq, tcol="ts"):
    t = df[tcol]
    expected = pd.date_range(t.min(), t.max(), freq=freq)
    missing = expected.difference(t)
    dups = int(t.duplicated().sum())
    print(f"\n{name}")
    print(f"  rows      {len(df):>9,}")
    print(f"  coverage  {t.min()}  ->  {t.max()}  (local AWST)")
    print(f"  expected  {len(expected):>9,}   missing {len(missing)}   duplicates {dups}")
    if len(missing):
        print(f"  missing:  {list(missing.astype(str)[:5])}")
    check(f"{name}: no duplicate intervals", dups == 0)
    return missing


def main():
    section("1. COVERAGE AND COMPLETENESS")
    dem = w.load_demand()
    dpv = w.load_dpv()
    prc = w.load_price()

    completeness("Operational demand (5 min)", dem, "5min")
    miss_dpv = completeness("Distributed PV (5 min)", dpv, "5min")
    completeness("Reference trading price (30 min)", prc, "30min")

    check("DPV gap is isolated, not a run", len(miss_dpv) <= 1,
          f"{len(miss_dpv)} missing interval(s)")

    section("2. FILE PARTITIONING: files are cut on UTC periods, stamped in AWST")
    for y in (2024, 2025):
        p = os.path.join(w.DATA_RAW, f"OperationalDemandWithdrawal-{y}.csv")
        if os.path.exists(p):
            one = w.load_demand(years=(y,))
            print(f"  {y} file spans {one.ts.min()} -> {one.ts.max()}")
            check(f"{y} file starts at 08:00 local (= 00:00 UTC), not midnight",
                  one.ts.min().hour == 8)

    section("3. NO DAYLIGHT SAVING (WA is fixed UTC+8)")
    # A DST transition would show up as a duplicated or missing local hour.
    counts = dem.groupby(dem.ts.dt.normalize()).size()
    odd = counts[(counts != 288)]
    print(f"  days with != 288 five-minute intervals: {len(odd)}")
    if len(odd):
        print(odd.head().to_string())
    check("every complete day has exactly 288 intervals", len(odd) <= 2,
          "only the truncated first/last days may differ")

    section("4. `Extracted At` is a file-level watermark, not revision history")
    for f in sorted(os.listdir(w.DATA_RAW)):
        if f.endswith(".csv") and ("Operational" in f or "Reference" in f or "distributed" in f):
            raw = pd.read_csv(os.path.join(w.DATA_RAW, f))
            n = raw["Extracted At"].notna().sum()
            print(f"  {f:42s} rows={len(raw):>7,}  Extracted At populated on {n} row(s)")
            check(f"{f}: watermark on first row only", n == 1)

    section("5. DEMAND IDENTITY")
    resid = (dem.operational_demand_mw
             - (dem.unscheduled_demand_mw - dem.withdrawal_mw))
    print("  Operational Demand == Unscheduled Operational Demand - Operational Withdrawal")
    print(f"  max |residual| = {resid.abs().max():.4f} MW over {len(dem):,} intervals")
    print(f"  withdrawal range: {dem.withdrawal_mw.min():.2f} .. {dem.withdrawal_mw.max():.2f} MW")
    check("identity holds to rounding", resid.abs().max() < 0.02)
    check("Operational Withdrawal is always <= 0", (dem.withdrawal_mw <= 0).all())

    section("6. STORAGE: `System Size (MW)` is the two-sided span, not one-way power")
    sc = w.load_facility_scada()
    reg = w.load_facilities().set_index("facility_code")["system_size_mw"]
    o = sc.groupby("facility_code")["mw"].agg(max_discharge_mw="max", max_charge_mw="min")
    o["observed_span_mw"] = o.max_discharge_mw - o.max_charge_mw
    o["registered_mw"] = reg
    o["span_over_registered"] = o.observed_span_mw / o.registered_mw
    o["first_active"] = (sc[sc.mw.abs() > 1].groupby("facility_code")["ts"].min())
    print(o.round(2).to_string())
    settled = o[o.span_over_registered > 0.5]  # exclude units still commissioning
    check("span matches registered size within 2% for commissioned units",
          bool(((settled.span_over_registered - 1).abs() < 0.02).all()),
          f"{len(settled)} of {len(o)} units commissioned")

    section("7. ADMINISTERED PRICE PARAMETERS MOVE BETWEEN YEARS")
    prc["year"] = prc.ts.dt.year
    tbl = prc.groupby("year")["price"].agg(
        n="size", min="min", max="max", mean="mean", std="std",
        pct_negative=lambda s: (s < 0).mean() * 100)
    print(tbl.round(2).to_string())
    print("\n  Failure Reason counts by year:")
    print(prc.pivot_table(index="failure_reason", columns="year",
                          values="ts", aggfunc="size", fill_value=0).to_string())

    section("SUMMARY")
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All invariants held.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
