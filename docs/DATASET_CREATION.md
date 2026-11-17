# Dataset creation

How the raw feeds in this repository became the modelling dataset the team
scored and clustered. Written by Dhananajay, who built it.

The end product is one row per HDB carpark per sample, with that carpark's
car occupancy, the utilisation of EV chargers within 300 m, and around 300
columns describing the residents, housing value, existing charger supply and
grid load around it. A lean 69-column cut and a one-row-per-carpark table
were then made for scoring and clustering.

```
LTA + HDB live feeds ──► utilisation log (this repo, 85 samples, 11–24 Sep)
                              │
HDB carpark information ──────┤  inner joins on carpark and sample time
                              ▼
                    1,984 carparks × 85 samples
                              │
URA subzones + Census 2020 ───┤
HDB property information ─────┤  left joins: address match first,
HDB resale prices ────────────┤  then distance
LTA EV charging register ─────┤
log-derived profiles ─────────┘
                              ▼
            per_run: 168,526 rows × 327 columns
                              │  column selection
                              ▼
     lean per_run: 69 columns   ·   carpark_model_table: 1,984 × 54
```

## 1. Sources

| Source | What it gives | Grain |
|---|---|---|
| LTA DataMall EV charging availability (live, via `sg_utilisation_logger.py`) | Status of every connector: available, in use, blank | connector × sample |
| data.gov.sg HDB carpark availability (live, same logger) | Total and free lots per carpark | carpark × sample |
| HDB Carpark Information | Carpark number, address, SVY21 coordinates, type, parking system | carpark |
| HDB Property Information | Block, street, dwelling units by flat type, commercial use flags | HDB block |
| HDB resale flat prices (Jan 2017 onward) | Transaction price, floor area, block, street, town, month | transaction |
| LTA EV charging points register (Jun 2026) | Every registered public charging point, with location | charging point |
| Census of Population 2020, residents by subzone | Resident population by planning area and subzone | subzone |
| URA Master Plan subzone boundaries | Polygons to place each carpark in a subzone | subzone |
| data.gov.sg context layers (planning areas, hawker centres, URA parking, ACRA) | Area names and commercial activity around each site | varies |

The live feeds are the only part that cannot be recreated later. Everything
else is a published file that can be downloaded again.

## 2. The utilisation log

The logger ran 4–7 times a day. The V1 dataset uses the 85 samples taken
from 11 to 24 September 2026. Each sample stores:

- one status row per connector (about 11,400), with only the id, status code
  and time, since nothing else changes hour to hour;
- one row per carpark (about 2,000): total lots, free lots, occupancy;
- one row for the whole network: totals, AC/DC split, utilisation;
- a set of quality checks.

Descriptive columns such as station name, operator, power rating and price
are written once a day to separate dimension files and joined back when the
workbook is built. That keeps a month of data near 70 MB rather than 2 GB.

The workbook `utilisation_log.xlsx` exposes this as Network history,
Station history, Carpark history, Station daily, Carpark daily, Hourly
profile, Latest connectors and Data quality. The dataset build reads from
those sheets.

## 3. The modelling dataset (`per_run`)

### 3.1 Core joins (inner)

```
HDBCarparkInformation
  ⋈ Carpark history   on car_park_no, sampled_at
  ⋈ Station history   EV stations within 300 m, same sample
```

| Step | Carparks |
|---|---|
| HDB carpark information | 2,272 |
| Present in the live availability feed | 1,995 |
| With at least one EV station within 300 m | 1,984 |

Inner joins were used here on purpose. A carpark with no availability data
has no target to model, and one with no charger nearby has no EV utilisation
to measure. Those carparks still matter as candidate sites, and they are kept
in the outer-join workbooks the pipeline writes (`output/outer/`).

Result: 168,526 rows, one per carpark per sample. That is slightly under
1,984 × 85 because a few carparks drop out of individual feed responses.

Distances use the haversine formula on WGS84 coordinates. HDB coordinates come
in SVY21 and are converted in `svy21_to_wgs84()` in the pipeline.

### 3.2 Enrichment (left joins)

Every layer below is a left join, so no carpark is lost if a layer has no
match. Where a layer can be matched by address, that is tried first; distance
is the fallback.

**Population.** Each carpark is placed in a URA subzone by point-in-polygon
and given that subzone's 2020 census population. Tengah, Punggol and Sengkang
have grown a lot since 2020, so for those three the verified figures from the
team deck (slide 11) are used instead. The census ethnic-group columns were
deliberately left out: they are not relevant to charger siting and should not
drive it.

**Housing blocks served.** The property file has no coordinates. Blocks are
placed by matching block number and street against carpark and EV station
addresses, then by taking the nearest-numbered block on the same street.
Median placement error is about 160 m, which is acceptable at the 300 m and
500 m radii used. Output: blocks served directly, plus block and
dwelling-unit counts within 300 m and 500 m, with the flat-type mix.

**Housing value.** Resale transactions from the last 24 months, summarised at
several levels: the served blocks, within 500 m, within 1 km, the street, the
town, and the town's year-on-year price change.

> `house_value_weight` = local resale price per m² ÷ national median price per m²

Above 1 means the area is pricier than the national median. It was used as a
proxy for ability to adopt an EV.

**Existing charger supply.** From the LTA register: points on site (within
30 m or a matching address), within 300 m, 500 m and 1 km, and the distance to
the nearest point. The register and the live feed overlap but are not the same
list, so both are kept.

**Context from the log.** For each carpark-sample row:

- network-wide EV utilisation and car occupancy at that moment;
- the carpark's and nearby stations' daily averages for that day;
- the hour-of-day profile;
- price and maximum connector power within 300 m (from Latest connectors);
- 14-day overnight, evening and p90 profiles for car occupancy and EV use;
- lag-1 values (the previous sample) for car occupancy and EV utilisation;
- `is_weekend` and `time_band`;
- a count of suspect faulty stations within 300 m.

Network history had a few duplicated rows from a git merge conflict during
collection. These were dropped before joining.

**Faulty versus busy.** A connector unavailable in more than 95% of sampled
hours across 14 days, overnight included, is counted as faulty. A charger that
never frees up at 3 a.m. is almost certainly broken, not popular. Without this
flag, broken chargers would read as the highest-demand sites.

### 3.3 MCDA inputs

These columns feed the team's multi-criteria scoring directly. Exact formulas
are in the data dictionary.

| Column | Meaning |
|---|---|
| `charging_urgency` | Mean of the 14 daily peak EV utilisations within 300 m. The main demand signal |
| `resident_share` | Resident population term for the carpark |
| `lot_share` | Parking capacity term for the carpark |
| EV adoption proxy | Built without the national EV share term, which is the same for every carpark and so adds nothing to a ranking |
| commercial density | Commercial activity around the carpark, from the context layers |
| EVCP saturation | How well existing charging points already cover the area |

### 3.4 Validation

Ranking planning areas by mean `charging_urgency` puts **Punggol (79.9)**,
**Sengkang (76.6)** and **Tengah (72.5)** at the top. Those are the three ground-truth
towns in the team deck. The dataset recovers
them from raw utilisation alone, which was the main sign the joins and
derived columns were working.

Other checks run during the build:

- conservation through every join: carparks in = matched + unmatched, none
  duplicated;
- 300 m station counts agree with the team workbook for 2,269 of 2,272
  carparks;
- all coordinates inside Singapore's bounding box;
- the quality log for every sample (see section 6).

## 4. Enriching the team's Base_Table workbook

The team had a Base_Table workbook (9 sheets) with QA formulas that referred
to fixed column letters. The aim was to add the new features without breaking
any of them.

**Matching.** Carparks were matched by `car_park_no`, then normalised address,
then coordinates within 50 m. All 2,272 matched on `car_park_no`, with
identical addresses and 0 m coordinate difference, so the fallbacks were never
needed.

**Base_Table, +6 columns per sample:** `is_weekend`, `time_band`,
`car_occupancy_pct_lag1`, `ev_300m_util_pct_lag1`, `network_ev_util_pct`,
`network_car_occupancy_pct`.

**Carpark_Summary, +57 columns per carpark:** match audit, 14-day profiles and
car–EV correlation, suspect faulty stations, census and verified population,
HDB blocks within 300/500 m, resale prices and `house_value_weight`, LTA
register counts, connector price and power, and the MCDA inputs.

All new columns were appended at the right with teal headers, so every
existing column letter stayed where it was and QA_Checks kept working. The
README, QA_Checks, Profile and Data_Dictionary sheets were extended to cover
them. A flat CSV version is 193,120 rows (2,272 × 85) × 98 columns.

## 5. The lean dataset

327 columns is too many to score or cluster sensibly. Columns were removed in
this order, and each removal is logged in the `column_selection` sheet:

| Rule | Removed |
|---|---|
| More than 20% empty | 8 |
| Near-constant (one value in ≥ 99% of rows) | 10 |
| Exact duplicate of another column | 3 |
| Not relevant to the problem statement | 234 |
| Correlated at \|r\| ≥ 0.95 with a kept column | 3 |
| **Kept** | **69** |

The kept columns cover: ids and time, location, carpark capacity, the two
targets (`occupancy_pct`, `ev300_utilisation_pct`), observed demand
(`charging_urgency` and the 14-day profiles), residents and housing value,
commercial density, existing EV supply, a grid-load proxy (installed kW and
maximum connector power), tariff, the faulty-station flag and the MCDA inputs.

`carpark_model_table` collapses this to one row per carpark (1,984 × 54) for
TOPSIS and K-means. Features should be standardised before clustering, so
large-scale columns such as installed kW do not dominate.

## 6. Data quality

Every sample ran 11 checks. Over 173 samples:

| Check | Result |
|---|---|
| Feeds not empty, ids unique, no negative lots, coordinates inside Singapore | ok every time |
| Connector count stable, sample is new | ok every time it applied |
| Blank-status share normal | warned 3 times |
| Free lots not above total | warned every sample |

The last one comes from the feed itself: a few carparks report more free lots
than they have. It is a source issue, not a pipeline one, but those rows are
worth handling explicitly in any model that uses occupancy.

## 7. Limitations

- **Cadence.** GitHub's scheduler delays and drops cron runs, so the log has
  4–7 samples a day, not 24. Hour 10 (SGT) was never sampled; in the V1 window, hours 14 and 23 are missing too.
- **Window.** V1 covers 14 days. That is enough for weekday/weekend and
  day/night patterns, not for anything seasonal.
- **Block placement.** Roughly 160 m median error, from address-based
  placement.
- **Census age.** 2020 figures, corrected only for the three fastest-growing
  towns.
- **Carparks without chargers** are outside the modelling table by design,
  and appear only in the outer-join candidate pool.

## 8. Output files

Not committed here: they are large, and the CSVs in `data/` rebuild them.

| File | Contents |
|---|---|
| `hdb_carpark_ev_model_dataset.zip` | `per_run.csv`, `per_day.csv`, `carpark_features.csv`, `data_dictionary.csv` |
| `hdb_carpark_ev_model_guide.xlsx` | README, join funnel, data dictionary, carpark features, profiles |
| `hdb_carpark_ev_lean_per_run.zip` | 168,526 × 69 |
| `hdb_carpark_ev_lean_dataset.xlsx` | `carpark_model_table`, `kept_columns`, `column_selection` |
| `Base_Table_with_added_columns.xlsx` | Team workbook plus 63 columns |
| `Base_Table_flat_with_added_columns.zip` | 193,120 × 98 |
