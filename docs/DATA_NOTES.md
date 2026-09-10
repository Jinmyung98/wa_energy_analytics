# Data notes: AEMO WA public market data

Audit run 1 September 2026 against files downloaded the same day. Every claim
below is reproduced by `scripts/audit.py`.

Source: `https://data.wa.aemo.com.au/public/public-data/datafiles/`

## What was loaded

| Series | Files | Resolution | Rows | Coverage (local AWST) |
|---|---|---|---|---|
| Operational demand and withdrawal | `OperationalDemandWithdrawal-{2023..2026}.csv` | 5 min | 307,008 | 2023-10-01 08:00 to 2026-09-01 07:55 |
| Estimated distributed PV | `distributed-pv-new-{2023..2026}.csv` | 5 min | 307,007 | 2023-10-01 08:00 to 2026-09-01 07:55 |
| Reference trading price | `ReferenceTradingPrice-{2023..2026}.csv` | 30 min | 51,168 | 2023-10-01 08:00 to 2026-09-01 07:30 |
| Storage facility SCADA | `FacilityScada-YYYY-MM.csv`, filtered to `_BESS`/`_ESR` | 5 min | 1,096,453 | 2023-10-01 08:00 to 2026-09-01 07:55 |
| Facility registry | `facilities.csv` | n/a | 176 | snapshot |
| Facility max daily temperature | `facility-temperature-{2024..2026}.csv` | daily | 55,727 | 2024-01-02 to 2026-08-31 |

## Completeness

- Demand: **0 missing intervals, 0 duplicates** across all 307,008 expected 5-minute intervals.
- Price: **0 missing intervals, 0 duplicates** across all 51,168 expected 30-minute intervals.
- Distributed PV: **1 missing interval** (2026-07-13 13:35), 0 duplicates. Single
  isolated gap, not a run.
- Storage SCADA: complete for every month once truncated downloads are re-fetched
  (see "Download integrity" below).

The interval data is unusually clean, and for it the data-quality story is not
about holes but about definitions and file conventions. **The facility
temperature file is the exception** and needs reading before use: nine of its 61
facility codes carry registered placeholder constants rather than measurements,
and they are numeric and non-null. See quirks 11 and 12.

## Verified quirks

### 1. Timestamps are AWST, and files are partitioned by UTC period

`OperationalDemandWithdrawal-2025.csv` spans **2025-01-01 08:00 to 2026-01-01
07:55** local, not the 2025 calendar year. The 08:00 boundary is midnight UTC:
files are cut on UTC periods but stamped in local Perth time (UTC+8).

The same applies to monthly SCADA files: `FacilityScada-2026-02.csv` runs
2026-02-01 08:00 to 2026-03-01 07:55 local.

**Consequence:** any per-year figure built from a single file is wrong at both
ends by eight hours, and a "2026 year to date" figure silently omits 1 January.
The loaders concatenate all available files and de-duplicate, so slice by local
date afterwards, never by file.

### 2. No daylight saving

Western Australia has not observed daylight saving since 2009. The interval grid
is a clean fixed +08:00 series with no spring-forward gap and no autumn fold, and
therefore no duplicated or missing hour anywhere in the record. Series are kept
tz-naive AWST. This removes a whole class of problem that NEM-region analyses have
to handle.

### 3. `Extracted At` is a file-level watermark, not revision history

Populated on the **first row only** (1 of 105,120 rows in the 2025 demand file);
blank on every other row. It records when the file was extracted, not when an
individual interval was last revised.

**Consequence:** revision behaviour cannot be studied from these annual archives.
Doing that would need repeated snapshots of the daily/real-time endpoints taken
over time, which is out of scope here. The watermarks are reported in the audit so
the vintage of each file is on the record: the 2026 files were extracted
2026-09-01, the 2025 demand file 2026-07-30, the 2025 DPV file 2026-06-26.

### 4. Descending row order, dd/MM/yyyy dates

Rows arrive newest-first and dates are day-first. `facilities.csv` and
`facility-temperature-*.csv` use ISO dates instead. The loader parses both and
returns ascending order.

### 5. Demand identity, and what the three demand columns mean

Verified exactly to rounding (max absolute residual 0.01 MW) over all 307,008
intervals:

```
Operational Demand = Unscheduled Operational Demand - Operational Withdrawal
```

`Operational Withdrawal` is always <= 0 (range -1,158 to -0.40 MW), so equivalently
`Operational Demand = Unscheduled Operational Demand + |Operational Withdrawal|`.

AEMO's published definitions match this arithmetic:

- **Operational Demand**: sent-out generation supplied by all market-registered
  Energy Producing Systems, including transmission and distribution losses. It is
  met by grid-connected generation, so it is *net* of behind-the-meter rooftop PV.
- **Unscheduled Operational Demand**: operational demand **excluding** consumption
  associated with scheduled loads, that is, excluding Electric Storage Resource
  charging.
- **Operational Withdrawal**: the withdrawal by those scheduled loads.

**Consequence, and it matters for the headline result:** the gap between
Operational Demand and Unscheduled Operational Demand *is* grid-scale storage
charging. Operational Demand includes battery charging and so is flattered by it;
Unscheduled Operational Demand is the underlying consumer-side series. Minimum
demand must be reported on both, because they now move in opposite directions.

Sources: [WEM Registration Technical Guide v4.2](https://www.aemo.com.au/-/media/files/electricity/wem/participant_information/guides-and-useful-information/wems-registration-technical-guide-v42clean.pdf),
[2024 WEM Electricity Statement of Opportunities](https://www.aemo.com.au/-/media/files/electricity/wem/planning_and_forecasting/esoo/2024/2024-wem-electricity-statement-of-opportunities.pdf?la=en).

### 6. `Average MWh` is energy per 5 minutes, not MW

`FacilityScada` reports energy over the dispatch interval. Multiply by 12 for
average MW. `load_facility_scada` does this and exposes an explicit `mw` column.

### 7. `System Size (MW)` for storage is the two-sided span

New finding, and the sibling of quirk 6. For every fully commissioned battery the
registered `System Size (MW)` equals **maximum discharge plus maximum charge**, not
the one-way power rating:

| Facility | Max discharge (MW) | Max charge (MW) | Observed span | Registered `System Size (MW)` |
|---|---|---|---|---|
| KWINANA_ESR1 | 100.7 | -100.1 | 200.8 | 200 |
| COLLIE_ESR1 | 200.0 | -200.1 | 400.1 | 400 |
| KWINANA_ESR2 | 226.4 | -223.7 | 450.0 | 450 |
| COLLIE_BESS2 | 300.1 | -300.0 | 600.0 | 600 |
| COLLIE_ESR4 | 252.0 | -252.3 | 504.3 | 500 |
| COLLIE_ESR5 | 252.7 | -252.5 | 505.2 | 500 |
| ALINTA_WGP_ESR1 | 25.2 | -25.4 | 50.7 | 200 (still commissioning) |

Six of seven match within 1.2%. ALINTA_WGP_ESR1 is the exception because it only
began dispatching on 25 July 2026 and is still ramping.

**Consequence:** quoting `System Size (MW)` as battery power overstates it by
roughly 2x. COLLIE_BESS2 is a **300 MW** battery, not a 600 MW one.

### 8. No fuel type in the registry

`facilities.csv` carries `Facility Class` and `System Size (MW)` but no technology
column, so technology is inferred from facility-code suffixes: `_WF` wind, `_PV`/`_SF`
solar, `_ESR`/`_BESS` storage, `_GT`/`_U*`/`_G*` thermal. This is a **stated
assumption**, not authoritative. It classifies 61 of 176 facilities; the remaining
115 are mostly Demand Side Programme registrations whose codes do not follow the
convention. It is reliable for the seven storage facilities, which is what the
analysis depends on.

### 9. Administered price parameters changed between years

Relevant to any volatility comparison:

| Year | Min price | Times at min | Max price | Times at max |
|---|---|---|---|---|
| 2023 (Oct-Dec) | -716.71 | 2 | 738.00 | 43 |
| 2024 | -1000.00 | 44 | 738.00 | 123 |
| 2025 | -150.52 | 1 | 1100.00 | 1 |
| 2026 (to Aug) | -16.72 | 1 | 1000.00 | 1 |

The "times at min/max" column separates two very different things:

- **The cap is a rule change.** In 2023 and 2024 the maximum is exactly 738.00 on
  43 and 123 intervals, which is a binding administered cap. From 2025 the top
  prices are unique values under a higher ceiling. Comparing 2024 upside
  volatility with 2026 upside volatility is therefore partly comparing
  administered parameters.
- **The floor's disappearance is a market outcome, not a rule change.** In 2024
  the minimum is exactly -1000.00 on 44 intervals, a binding floor. In 2025 and
  2026 the single lowest price is unique and nowhere near any plausible floor
  (-150.52, then -16.72). Prices stopped reaching the floor; the floor did not
  move up to meet them.

So downside price results can be compared across years at face value, while
upside volatility comparisons need the cap change stated. Report interquartile
and trimmed measures alongside the standard deviation.

### 10. `Failure Reason` and manual overrides

| Failure Reason | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|
| AffectedDispatchInterval | 2 | 267 | 238 | 49 |
| MarketAnalystOverride | 0 | 1 | 237 | 207 |
| DispatchEngineFailedToRun | 0 | 17 | 24 | 10 |
| MarketIsSuspended | 0 | 0 | 16 | 0 |

`MarketAnalystOverride` jumps from 1 interval in 2024 to 237 in 2025 and 207 in
2026 (about 1.3% and 1.8% of intervals). These are manually set prices. They are
retained in the base series but flagged, and any price result should be checked
with them excluded.

### 11. Nine facility temperature series are placeholders, not measurements

`facility-temperature-*` publishes a `Maximum Daily Temperature` per facility per
day. For nine of the 61 facility codes the value is a registered constant:

| Group | Facility codes | Evidence |
|---|---|---|
| Constant 41.0 | `ALINTA_WWF`, `BADGINGARRA_WF1`, `MERSOLAR_PV1`, `WARRADARGE_WF1`, `YANDIN_WF1`, `SBSOLAR1_CUNDERDIN_PV1` | one distinct value across all 973 days, standard deviation exactly 0 |
| Sentinel -1.4 | `GREENOUGH_RIVER_PV1`, `MUNGARRA_GT1`, `MUNGARRA_GT3` | 968 of 973 readings are one of `-1.401`, `-1.64`, `-1.636`; median is -1.4 in **every month of the year** |
| Empty | `ALBANY_WF1`, `GRASMERE_WF1` | 973 rows, zero values |

The first two groups are the dangerous ones, because they are numeric and
non-null: a `notna()` filter passes all 8,785 of those readings straight through.
41.0 is also a *plausible* WA summer maximum, so it fails only on variance.

**Detection does not rely on knowing the magic values.** Every genuine SWIS
series must track the fleet-wide daily median, because the whole footprint shares
one seasonal cycle. Measured against it:

- genuine series: correlation **0.797 to 0.985** (50 facilities)
- the -1.4 sentinel: **-0.096**
- the 41.0 constants: undefined, zero variance

The threshold in `wa_data.PLACEHOLDER_CORR` is 0.5, sitting in an empty gap 0.89
wide. `load_temperature` drops these series by default;
`temperature_diagnostics` returns the per-facility evidence.

**Consequence, and it is subtler than it looks:** the two defects push in
opposite directions, so the *fleet mean* moves only +0.26 degC when they are
included (25.60 against 25.34) and an aggregate sanity check will not catch them.
Any per-facility or per-station figure takes the full error: `YANDIN_WF1` reports
a 41.00 degC mean and `MUNGARRA_GT1` a -1.36 degC mean, against a fleet median of
24.5 degC.

One further reading is a genuine sensor fault rather than a placeholder:
`EDWFMAN_WF1` reports **62.779 degC on 2024-11-22**, against the WA all-time
record of 50.7 degC and 28.0 degC at the nearest real station the same day. Twenty
more readings sit at 51.2 degC across the Pinjar group, marginally above the
record. These are **flagged, not dropped** - `load_temperature` returns an
`implausible` column - because unlike the placeholders they are single-day events
rather than a broken series. They matter most to max-minus-min statistics: the
one 62.8 degC value alone inflates the maximum same-day spread across stations
from 20.7 to 41.5 degC.

### 12. Temperature is reported per facility but measured per station

The 50 genuine facility series contain only **21 distinct signals**. Facilities
at the same site share one instrument, and the shared series are identical to ten
decimal places, not merely similar:

| Station | Facilities |
|---|---|
| Pinjar / Neerabup | 10 (`NEWGEN_NEERABUP_GT1`, `PINJAR_GT1..GT11`) |
| Alinta Wagerup / Collie / Bluewaters / Muja | 7 |
| Kalgoorlie / Southern Cross group | 6 |
| Kwinana / Cockburn group | 4 |
| six further pairs | 2 each |
| nine single-facility sites | 1 each |

**Consequence:** any analysis that treats facility columns as independent
observations multiply-counts a handful of instruments - a correlation matrix
across facilities is mostly measuring r = 1.0 between duplicated columns, and a
fleet mean weights the Pinjar station ten times and Muja once.
`temperature_stations` returns the facility-to-station mapping.

Nine of the 21 stations cover under 900 of the 973 days, because they belong to
storage facilities that commissioned mid-record. Ranking stations by mean
temperature without a coverage filter therefore ranks late-commissioning sites
artificially cool, for having missed two summers rather than for being cold.

### 13. What the temperature data can and cannot support

Established in `notebooks/EDA_facility_temperature.ipynb`, recorded here because
it bears on how the series should be used:

- **Peak demand responds to temperature as a U, not a line.** Pearson's r between
  daily maximum temperature and daily peak operational demand is **0.27**, which
  reads as "no relationship". Fitted as heating and cooling degree days around a
  balance point it reaches **R-squared 0.72**. Reporting the linear correlation
  for this pair is an error, not a weak result.
- **The balance point is around 25 degC, and is not stable to better than a couple
  of degrees.** A single fit on the Pinjar station returns 27.0 degC, but refitting
  independently on each of the 18 stations with enough coverage gives a range of
  **23.3 to 27.3 degC**, median 25.3. Quote roughly 25 degC. What *is* stable is the
  slopes - about +90 MW per cooling degree and +81 MW per heating degree, holding
  within 5% across weekdays, weekends and all three years.
- **Northern-hemisphere convention does not transfer.** The usual 18 degC balance
  point is far below anything the WA data supports.
- **Daily resolution is a hard ceiling.** Maximum-only, once-daily values cannot
  reach the 5-minute demand series, so peak *timing*, temperature-driven ramp
  rates, and the overnight minima that drive multi-day heatwave load are all out
  of scope. Reaching them needs a Bureau of Meteorology hourly source, which is a
  different dataset with a different licence.

## Download integrity

Streaming 35 monthly SCADA files in parallel produced **three silently truncated
files** (2026-04, 2026-05, 2026-06) that ended mid-line. pandas failed loudly on
one of them, but a truncated file that happens to end on a line boundary would
have been read as merely short.

`scripts/fetch_bess_scada.sh` therefore validates each file's final line against
the expected row pattern and re-fetches up to four times, exiting non-zero if any
month is still bad. Row counts per month are also a useful check: they step with
the number of commissioned facilities, from 8,928 (one facility, 31 days) to
62,496 (seven facilities).

## Out of scope: pre-reform data

History begins 2023-09-26 with the new WEM. The `prereform/` archive uses
schemas that do not join cleanly, and the October 2023 market start is a
structural break in both price formation and demand measurement. Three
post-reform years are used and the pre-reform period is excluded deliberately,
not for want of data.
