# My contribution

**Dhananajay** — data acquisition, data governance and dataset creation for
the Spatial EV Charging Priority Model (team Vector Analytics, NUS MTech EBA).

The project asks where Singapore should add public EV chargers, and how many,
at the level of an HDB carpark. My part was getting the data that question
needs and turning it into one table the team could score and model on. I also
co-led the Power BI dashboard.

## What I built

| Area | What it is | Where |
|---|---|---|
| Live data collection | A logger that sampled every LTA EV connector and every HDB carpark's free lots, 4–7 times a day for 34 days | `sg_utilisation_logger.py` |
| Scheduling | GitHub Actions jobs so collection ran without a laptop being on; a Windows Task Scheduler fallback | `.github/workflows/`, `setup_windows_tasks.ps1` |
| Join pipeline | Matches each charger to its carpark (150 m and 300 m), adds six data.gov.sg context layers, writes seven workbooks | `sg_ev_carpark_pipeline.py` |
| Storage design | Fact/dimension split that cut storage from ~2 GB a month to ~70 MB with no loss | `data/utilisation/` |
| Data quality | 11 checks on every sample, logged to `quality.csv` and a Data quality sheet | `data/utilisation/quality.csv` |
| Audit trail | Every API response kept as gzipped JSON; `--replay` rebuilds all CSVs from it | `data/raw/` |
| Key handling | No key in code; read from a secret or a gitignored file; a pre-commit hook blocks key-shaped strings | `tools/pre-commit`, `.gitignore` |
| Modelling dataset | 168,526 rows × 327 columns, then a lean 69-column version and a 1,984-row carpark table for TOPSIS and K-means | [DATASET_CREATION.md](DATASET_CREATION.md) |
| Team workbook enrichment | 63 columns added to the team's Base_Table workbook without breaking its formulas | [DATASET_CREATION.md](DATASET_CREATION.md#4-enriching-the-teams-base_table-workbook) |
| Data dictionary | Every column documented: source, unit, how it was derived | dataset guide workbook |

## What was collected

| | |
|---|---|
| Period | 11 Sep – 14 Oct 2026 (34 days) |
| Samples | 173 network snapshots |
| EV connectors tracked | 11,429 → 11,698 over the period (2,755 → 2,799 stations) |
| HDB carparks tracked | 2,007 |
| Connector status rows | 1.99 million |
| Station rows | 479,000 |
| Carpark rows | 420,000 |
| Raw API snapshots kept | 209 per feed |

Across the 173 samples, network EV utilisation averaged 27.8% (range
16.6–42.0%) and HDB car occupancy averaged 41.5%.

## Decisions worth explaining

**Why sample live at all.** LTA publishes connector status as it is right now.
There is no history feed, so an hour not sampled is gone for good. Without the
logger the project would have known where chargers are, but not how busy they
get.

**Why GitHub Actions.** The faulty-charger rule (below) needs the small hours,
02:00 to 05:00, which is exactly when a laptop sleeps. Running on GitHub meant
those hours were in the data.

**Busy or broken.** A connector that is unavailable in more than 95% of
sampled hours across 14 days, including overnight, is treated as faulty, not
in high demand. Without this, broken chargers would look like the most
wanted ones and push the model toward the wrong carparks.

**Why the raw JSON is kept.** If a CSV is damaged, or a new derived column is
added later, everything can be recomputed from source across the full history,
not patched from today onward.

## The rest of the project

The scoring and modelling stages (MCDA weighting, TOPSIS ranking, K-means
clustering) and the final report were built on the dataset described here.
That work is not in this repository.
