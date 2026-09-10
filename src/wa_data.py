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

8. facility-temperature carries NON-MEASUREMENT PLACEHOLDER SERIES. Nine of the
   61 facility codes report a registered constant rather than a weather reading:
   six sit at exactly 41.0 for every day on record, and three report a
   near-constant negative sentinel (-1.401 / -1.64 / -1.636 on 968 of 973 days).
   Two more are entirely null. These are numeric and non-null, so `notna()` does
   not catch them and they bias every aggregate they enter. `load_temperature`
   drops them by default; `temperature_diagnostics` shows the evidence.

9. Temperature is reported PER FACILITY but measured PER STATION. The 50 genuine
   facility series contain only 21 distinct signals - the Pinjar/Neerabup group
   alone is 10 facility codes on one identical series. Treating facility columns
   as independent observations multiply-counts a handful of stations.
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


# Highest temperature ever recorded in Western Australia (Mardie, 19 Feb 1998),
# per the Bureau of Meteorology. Used only to FLAG implausible readings, never to
# silently clip them.
WA_RECORD_MAX_C = 50.7

# A genuine weather series in the SWIS correlates strongly with the fleet-wide
# daily median, because the whole footprint shares one seasonal cycle. Placeholder
# series do not. The observed gap is wide enough that the threshold is not a
# tuning parameter: genuine series score 0.797 to 0.985, the negative sentinel
# scores -0.096, and the constants are undefined (zero variance).
PLACEHOLDER_CORR = 0.5


def _temperature_raw(years=(2024, 2025, 2026), raw=None):
    """Long-format temperature with placeholders still in. See load_temperature."""
    raw = raw or DATA_RAW
    frames = []
    for y in years:
        p = os.path.join(raw, "facility-temperature-%d.csv" % y)
        if os.path.exists(p):
            frames.append(_read(p, "Trading Date"))
    if not frames:
        raise FileNotFoundError("no facility-temperature files matched")
    return (
        pd.concat(frames, ignore_index=True)
        .rename(columns={
            "Trading Date": "date",
            "Facility Code": "facility_code",
            "Maximum Daily Temperature": "temp_max_c",
        })
        .sort_values(["date", "facility_code"])
        .reset_index(drop=True)
    )


def temperature_wide(years=(2024, 2025, 2026), raw=None, drop_placeholders=True):
    """Temperature as a date x facility_code matrix."""
    df = _temperature_raw(years, raw)
    w = df.pivot_table(index="date", columns="facility_code", values="temp_max_c")
    if drop_placeholders:
        d = temperature_diagnostics(years, raw)
        w = w[[c for c in w.columns if c in set(d.index[d.verdict == "weather"])]]
    return w


def temperature_diagnostics(years=(2024, 2025, 2026), raw=None):
    """Per-facility evidence for QUIRK 8: which series are real measurements.

    Returns one row per facility code with the counts the verdict rests on, so the
    classification can be audited rather than taken on trust. `verdict` is one of:

      weather     - correlates with the fleet-wide daily median (see PLACEHOLDER_CORR)
      constant    - zero variance: a registered value repeated every day
      sentinel    - varies, but does not track the weather; a placeholder
      empty       - no values at all
    """
    df = _temperature_raw(years, raw)
    w = df.pivot_table(index="date", columns="facility_code", values="temp_max_c")
    # pivot_table drops facilities whose every reading is null, but those are a
    # finding in their own right, so put the columns back as all-NaN.
    w = w.reindex(columns=sorted(df.facility_code.unique()))
    # Median across facilities is the reference seasonal signal. It is robust to
    # the placeholder columns, which are a small minority of the fleet. Computed
    # over the columns that carry data, so the all-null ones do not raise on every
    # row of an otherwise fine calculation.
    have = [c for c in w.columns if w[c].notna().any()]
    ref = w[have].median(axis=1)

    rows = {}
    for c in w.columns:
        s = w[c]
        n, sd = int(s.notna().sum()), s.std()
        corr = s.corr(ref) if n > 2 and sd and sd > 0 else float("nan")
        if n == 0:
            verdict = "empty"
        elif not sd or sd == 0:
            verdict = "constant"
        elif not (corr > PLACEHOLDER_CORR):
            verdict = "sentinel"
        else:
            verdict = "weather"
        nan = float("nan")
        rows[c] = dict(n=n, n_distinct=int(s.nunique()), std=sd, corr_fleet=corr,
                       # guarded: reducing an all-null column warns rather than
                       # returning NaN quietly, and "empty" is a verdict, not an error
                       min=s.min() if n else nan,
                       median=s.median() if n else nan,
                       max=s.max() if n else nan,
                       n_implausible=int((s > WA_RECORD_MAX_C).sum()),
                       verdict=verdict)
    return pd.DataFrame(rows).T.infer_objects().sort_values(["verdict", "corr_fleet"])


def temperature_stations(years=(2024, 2025, 2026), raw=None):
    """Map facility_code -> station id, collapsing QUIRK 9 duplicate series.

    Facilities reporting a byte-identical series are reading the same weather
    station. Stations are numbered by member count and named for a representative
    member, because AEMO publishes no station identifier to name them by.
    """
    w = temperature_wide(years, raw, drop_placeholders=True)
    groups = {}
    for c in w.columns:
        key = tuple(w[c].round(3).fillna(-999.0))
        groups.setdefault(key, []).append(c)
    out = {}
    ordered = sorted(groups.values(), key=lambda v: (-len(v), sorted(v)[0]))
    for i, members in enumerate(ordered, 1):
        rep = sorted(members)[0]
        name = "S%02d_%s" % (i, rep)
        for m in members:
            out[m] = name
    return pd.Series(out, name="station").rename_axis("facility_code")


def load_temperature(years=(2024, 2025, 2026), raw=None, drop_placeholders=True):
    """AEMO per-facility maximum daily temperature (degC). Daily resolution only.

    QUIRK 8: nine facility codes report registered placeholder constants rather
    than measurements, and they are numeric and non-null. They are dropped by
    default. Pass drop_placeholders=False for the unfiltered file, and see
    `temperature_diagnostics` for the per-facility evidence.

    QUIRK 9: the surviving facility series are not independent - use
    `temperature_stations` to collapse them to distinct weather stations.

    A `station` column is attached, and readings above the WA record of 50.7 degC
    are flagged in `implausible` rather than dropped.
    """
    df = _temperature_raw(years, raw).dropna(subset=["temp_max_c"])
    if drop_placeholders:
        d = temperature_diagnostics(years, raw)
        keep = set(d.index[d.verdict == "weather"])
        df = df[df.facility_code.isin(keep)]
        df = df.join(temperature_stations(years, raw), on="facility_code")
    df["implausible"] = df.temp_max_c > WA_RECORD_MAX_C
    return df.sort_values(["date", "facility_code"]).reset_index(drop=True)


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
