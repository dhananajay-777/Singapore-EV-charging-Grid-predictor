# My contribution

**Dhananjay**: data acquisition and governance lead, and Power BI dashboard
co-lead, for *Data-Driven Spatial Priority Modelling for Public EV Charging
Infrastructure in Singapore* (team Vector Analytics, NUS MTech EBA).

The project asks where Singapore should add public EV chargers, at the level
of an HDB carpark. Demand is measured directly: the share of charging
connectors within 300 m of a carpark that are in use between midnight and
6 a.m. That measure only exists because the live feeds were logged, and
getting that data, and turning it into a model the team could analyse, was my
part.

## My role in the project


| Stage | My role |
|---|---|
| Data acquisition and governance | **Lead** |
| Data understanding, charts and Power BI dashboards | **Co-lead** |
| Problem framing and objectives | Contributor |
| Modelling and analysis | Contributor |
| Shortlist and recommendations | Contributor |
| Write-up and presentation | Contributor |

Every member worked on every stage; each stage had a lead who owned the
quality of its output.

## What I built

Everything in this repository's code is my work.

| Area | What it is | Where |
|---|---|---|
| Live data logging | A logger that polled every LTA charging connector and every HDB carpark's free lots, stamping both feeds with the same time. 85 snapshots in the 14-day project window; 173 over 34 days in total | `sg_utilisation_logger.py` |
| Scheduling | GitHub Actions jobs so collection ran with no laptop on, small hours included; a Windows Task Scheduler fallback | `.github/workflows/`, `setup_windows_tasks.ps1` |
| Join pipeline | Matches each charger to its carpark (150 m and 300 m), adds six data.gov.sg context layers, writes seven workbooks | `sg_ev_carpark_pipeline.py` |
| Storage design | Fact/dimension split that cut storage from ~2 GB a month to ~70 MB with no loss | `data/utilisation/` |
| Automated checks | 11 checks on every sample, logged to `quality.csv` and a Data quality sheet | `data/utilisation/quality.csv` |
| Audit trail | Every API response kept as gzipped JSON; `--replay` rebuilds all CSVs from it | `data/raw/` |
| Key handling | No key in code; read from a secret or a gitignored file; a pre-commit hook blocks key-shaped strings | `tools/pre-commit`, `.gitignore` |
| Team workbook enrichment | 63 columns added to the team's Base_Table workbook without breaking its QA formulas | [DATASET_CREATION.md](DATASET_CREATION.md#4-enriching-the-teams-base_table-workbook) |
| Extended dataset | 168,526 rows × 327 columns, cut to a lean 69-column version and a 1,984-row carpark table for clustering | [DATASET_CREATION.md](DATASET_CREATION.md) |
| 34-day extension | Charts and town ranking on the full collection period | `analysis/make_figures.py` |

## The stage I led: data acquisition and governance

What this stage delivered. I led it; teammates contributed to each part, and stewardship of the data
dictionary, quality checks and logging schedule was shared across the team.

- **Eight open datasets in one carpark-level model.** A fact table of
  2,272 carparks × 85 readings (193,120 rows) with three dimension tables,
  and a data dictionary covering all 72 columns.
- **Integration rules.** SVY21 coordinates converted to WGS84 for all 2,272
  carparks; every carpark placed in a URA subzone by point-in-polygon;
  stations matched to carparks by address (median 0.1 m from the carpark
  point); haversine counts of connectors within 300 m and of homes and
  registered chargers within 500 m.
- **Profiling and quality.** No duplicate keys across 193,120 rows; every
  reading reconciled to LTA's and HDB's network totals; checks recorded by
  quality dimension.
- **Busy or broken.** The feed cannot tell a broken connector from one in
  use. Stations with no free connector in any reading between 02:00 and 05:59
  were flagged as suspected faulty, and the 35 carparks within 300 m of one
  were left out of the model. Without this, broken chargers would have looked
  like the busiest ones.

## What was collected

| | |
|---|---|
| Period | 11 Sep – 14 Oct 2026 (34 days); the team analysis uses 11–24 Sep |
| Samples | 173 network snapshots (85 in the 14-day analysis window) |
| EV connectors tracked | 11,429 → 11,698 over the period (2,755 → 2,799 stations) |
| HDB carparks tracked | 2,007 in the availability feed |
| Connector status rows | 1.99 million |
| Station rows | 479,000 |
| Carpark rows | 420,000 |
| Raw API snapshots kept | 209 per feed |

## Decisions worth explaining

**Why log live at all.** LTA publishes connector status as it is right now.
There is no history feed, so a sample not taken is gone for good. Without the
logger the project would have known where chargers are, but not how busy they
get.

**Why GitHub Actions.** Overnight demand and the faulty-station rule both
need the small hours, which is exactly when a laptop sleeps. Running on
GitHub meant those hours were in the data.

**Why the raw JSON is kept.** If a CSV is damaged, or a new derived column is
added later, everything can be recomputed from source across the full history,
not patched from today onward.

## The rest of the project

The team analysis (hypothesis tests, regression with town
effects, K-means priority tiers, a decision-tree screening rule and a
20-carpark pilot shortlist) was team work, run on the carpark-level model
described above. I contributed to each of those stages; the analysis code is
not in this repository.
