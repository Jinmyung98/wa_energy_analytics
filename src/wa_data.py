"""
Loading and cleaning helpers for AEMO WA (WEM / SWIS) public market data.

Reused across the WA energy project series, so the quirks live here once.

Verified quirks (evidence in docs/DATA_NOTES.md):

1. Timestamps are local Perth time (AWST, UTC+8). WA does not observe daylight
   saving, so the interval grid is a clean fixed-offset series with no DST folds
   or gaps. Series are returned tz-naive AWST.

2. Annual/monthly files are partitioned by UTC period but stamped in AWST, so
   `...-2025.csv` actually spans 2025-01-01 08:00 to 2026-01-01 07:55 local.
   Never assume one file equals one local calendar year. The load_* helpers
   concatenate and de-duplicate, so slice by local date afterwards.

3. Dates are dd/MM/yyyy in the interval files but ISO in facilities.csv and
   facility-temperature. `_parse_dt` handles both.

4. Rows arrive in DESCENDING time order. Everything here returns ascending.

5. `Extracted At` is populated on the first row only. It is a file-level
   extraction watermark, not per-row revision tracking, so it is dropped by
   default and surfaced separately via `extracted_at()`.

6. FacilityScada reports `Average MWh` over a 5-minute interval, NOT MW.
   Multiply by 12 for average MW. `load_facility_scada` does this and returns
   an explicit `mw` column.

7. Demand identity, verified exactly to rounding across all 307,008 intervals:
       Operational Demand = Unscheduled Operational Demand - Operational Withdrawal
   `Operational Withdrawal` is always <= 0 (scheduled loads, i.e. storage
   charging), so Operational Demand INCLUDES storage charging while Unscheduled
   Operational Demand EXCLUDES it. The gap between them is scheduled storage
   charging, which is directly useful for the battery analysis.
"""

from __future__ import annotations

import glob
import os
import re

import pandas as pd

DATA_RAW = os.environ.get("WA_DATA_RAW", os.path.join("data", "raw"))

# Facility-code suffix to technology. Inferred from the naming convention
# because facilities.csv carries no fuel-type column.
# STATED ASSUMPTION, not authoritative.
TECH_SUFFIX = [
    (r"_(BESS|ESR)\d*$", "storage"),
    (r"_WF\d*$", "wind"),
    (r"_(PV|SF)\d*$", "solar"),
    (r"_(GT|CCGT|U\d+|G\d+)\d*$", "thermal"),
]

SEASONS = {
    12: "Summer", 1: "Summer", 2: "Summer",
    3: "Autumn", 4: "Autumn", 5: "Autumn",
    6: "Winter", 7: "Winter", 8: "Winter",
    9: "Spring", 10: "Spring", 11: "Spring",
}


def _parse_dt(s):
    """Parse AEMO timestamps: dd/MM/yyyy in interval files, ISO elsewhere."""
    return pd.to_datetime(s, dayfirst=True, format="mixed")


def _read(path, tcol, keep_extracted=False):
    df = pd.read_csv(path)
    df[tcol] = _parse_dt(df[tcol])
    if not keep_extracted and "Extracted At" in df.columns:
        df = df.drop(columns=["Extracted At"])
    return df


def extracted_at(path):
    """File-level extraction watermark (the first row's `Extracted At`)."""
    head = pd.read_csv(path, nrows=1)
    return _parse_dt(head["Extracted At"]).iloc[0]


def _concat(paths, tcol, rename="ts"):
    if not paths:
        raise FileNotFoundError("no input files matched")
    frames = [_read(p, tcol) for p in sorted(paths)]
    return (
        pd.concat(frames, ignore_index=True)
        .sort_values(tcol)
        .drop_duplicates(subset=tcol, keep="first")
        .reset_index(drop=True)
        .rename(columns={tcol: rename})
    )


def load_demand(years=(2023, 2024, 2025, 2026), raw=None):
    """5-minute operational demand and scheduled withdrawal, in MW."""
    raw = raw or DATA_RAW
    paths = [os.path.join(raw, "OperationalDemandWithdrawal-%d.csv" % y) for y in years]
    df = _concat([p for p in paths if os.path.exists(p)], "Dispatch Interval")
    return df.rename(columns={
        "Operational Demand": "operational_demand_mw",
        "Unscheduled Operational Demand": "unscheduled_demand_mw",
        "Operational Withdrawal": "withdrawal_mw",
    })


def load_dpv(years=(2023, 2024, 2025, 2026), raw=None):
    """5-minute estimated distributed (rooftop) PV generation, in MW."""
    raw = raw or DATA_RAW
    paths = [os.path.join(raw, "distributed-pv-new-%d.csv" % y) for y in years]
    df = _concat([p for p in paths if os.path.exists(p)], "Timestamp")
    return df.rename(columns={"Estimated DPV Generation(MW)": "dpv_mw"})


def load_price(years=(2023, 2024, 2025, 2026), raw=None):
    """30-minute reference trading price, in $/MWh."""
    raw = raw or DATA_RAW
    paths = [os.path.join(raw, "ReferenceTradingPrice-%d.csv" % y) for y in years]
    df = _concat([p for p in paths if os.path.exists(p)], "Trading Interval")
    return df.rename(columns={
        "Reference Trading Price": "price",
        "Failure Reason": "failure_reason",
    })


def infer_tech(code):
    for pattern, tech in TECH_SUFFIX:
        if re.search(pattern, str(code)):
            return tech
    return "unknown"


def load_facilities(raw=None):
    """Facility registry, with an INFERRED `tech` column (see TECH_SUFFIX)."""
    raw = raw or DATA_RAW
    df = pd.read_csv(os.path.join(raw, "facilities.csv"))
    if "Extracted At" in df.columns:
        df = df.drop(columns=["Extracted At"])
    df = df.rename(columns={
        "Facility Code": "facility_code",
        "Facility Class": "facility_class",
        "System Size (MW)": "system_size_mw",
        "Participant Name": "participant_name",
        "Participant Code": "participant_code",
    })
    df["tech"] = df["facility_code"].map(infer_tech)
    return df


def load_facility_scada(pattern="scada_bess/bess-*.csv", raw=None):
    """Per-facility 5-minute SCADA.

    Converts `Average MWh` (energy over the 5-minute interval) to average MW by
    multiplying by 12. Negative values are withdrawal, i.e. storage charging.
    The pre-filtered storage extracts carry no header row, so one is supplied.
    """
    raw = raw or DATA_RAW
    paths = sorted(glob.glob(os.path.join(raw, pattern)))
    if not paths:
        raise FileNotFoundError("no SCADA files matched %s" % pattern)
    cols = ["Dispatch Interval", "Facility Code", "Average MWh", "Extracted At"]
    frames = []
    for p in paths:
        d = pd.read_csv(p, header=None, names=cols)
        frames.append(d.drop(columns=["Extracted At"]))
    df = pd.concat(frames, ignore_index=True)
    df["Dispatch Interval"] = _parse_dt(df["Dispatch Interval"])
    df = (
        df.sort_values(["Dispatch Interval", "Facility Code"])
        .drop_duplicates(subset=["Dispatch Interval", "Facility Code"], keep="first")
        .reset_index(drop=True)
        .rename(columns={"Dispatch Interval": "ts", "Facility Code": "facility_code"})
    )
    df["mw"] = df["Average MWh"] * 12.0  # GOTCHA: MWh per 5 min -> MW
    return df


def load_temperature(years=(2024, 2025, 2026), raw=None):
    """AEMO per-facility maximum daily temperature (degC). Daily resolution only."""
    raw = raw or DATA_RAW
    frames = []
    for y in years:
        p = os.path.join(raw, "facility-temperature-%d.csv" % y)
        if os.path.exists(p):
            frames.append(_read(p, "Trading Date"))
    df = pd.concat(frames, ignore_index=True)
    return (
        df.rename(columns={
            "Trading Date": "date",
            "Facility Code": "facility_code",
            "Maximum Daily Temperature": "temp_max_c",
        })
        .dropna(subset=["temp_max_c"])
        .sort_values(["date", "facility_code"])
        .reset_index(drop=True)
    )


def to_trading_intervals(df, value_cols, ts="ts", how="mean"):
    """Aggregate 5-minute data to 30-minute trading intervals.

    AEMO trading intervals are LABELLED BY THEIR START: an interval stamped
    12:00 covers the six dispatch intervals 12:00 to 12:25. We take the interval
    MEAN rather than end-of-interval, so a 30-minute MW value stays directly
    comparable to the 5-minute MW series. STATED ASSUMPTION.
    """
    g = (
        df.set_index(ts)[list(value_cols)]
        .resample("30min", label="left", closed="left")
    )
    return getattr(g, how)().reset_index()


def add_time_parts(df, ts="ts"):
    """Attach the calendar and time-of-day helpers used across the notebooks."""
    d = df.copy()
    t = d[ts]
    d["date"] = t.dt.normalize()
    d["year"] = t.dt.year
    d["month"] = t.dt.month
    d["hour"] = t.dt.hour
    d["tod_min"] = t.dt.hour * 60 + t.dt.minute
    d["dow"] = t.dt.dayofweek
    d["is_weekend"] = d["dow"] >= 5
    d["season"] = d["month"].map(SEASONS)  # southern-hemisphere meteorological
    return d
