# Data collection: how it runs

> Collection ran from 11 Sep to 15 Oct 2026 and has ended. Both GitHub
> workflows are disabled; the data in `data/` is the complete archive.

Two scripts. One samples the live feeds every hour, the other rebuilds the
workbooks once a day. Everything else here is setup.

## Start here

| If you want to | Read |
|---|---|
| Build the workbook on this PC, now | `python run_local.py`, and the next section |
| Run collection on GitHub so your laptop can be shut | `GITHUB_SETUP.md` |
| Run it on your own PC | `SCHEDULING.md`, then `setup_windows_tasks.ps1` (in the project root) |
| Understand what gets collected and why | the rest of this file |

## The two scripts

**`sg_utilisation_logger.py`** — hourly. Two API calls, a few seconds. Records
the status of every connector and the free lots in every HDB carpark, and
appends the rows to compressed CSV.

This is the only time-critical part of the project. Connector status changes
minute to minute and there is no historical feed, so an hour not sampled is an
hour lost for good. Everything else can be rerun.

**`sg_ev_carpark_pipeline.py`** — daily. Two to three minutes. Fetches the
chargers and carparks, pulls six reference datasets from data.gov.sg, matches
chargers to carparks at 150 m and 300 m, and writes seven workbooks.

Do not run the pipeline hourly. It would hit data.gov.sg 144 times a day for
datasets (planning areas, hawker centres, URA parking, ACRA) that change perhaps
once a year, which is what produced the rate-limiting errors earlier.

## The everyday command

```
python run_local.py
```

That is the one to remember. It pulls whatever the GitHub runs have collected,
takes a fresh sample so the workbook includes right now, and rebuilds the
workbook from **every sample ever taken** — GitHub's, your earlier local runs,
and the one just taken. Run it before you sit down to analyse anything.

| Flag | Effect |
|---|---|
| `--no-pull` | skip the git pull (offline, or no repo) |
| `--no-fetch` | rebuild only, do not call the APIs |
| `--history-days N` | row-level window in the workbook (default 30 locally) |
| `--replay` | rebuild the whole CSV store from the raw JSON snapshots |
| `--full` | also run the daily pipeline: both joins, seven workbooks |

### Why the workbook is built here and not on GitHub

An `.xlsx` is a zip. Git cannot diff it, so every change stores a whole new
copy. Measured at full scale the utilisation workbook is 2 MB without
row-level history and 9 MB with three days of it — committed hourly that runs
to gigabytes, and GitHub starts refusing pushes long before the project ends.

So the split is: **git holds the CSVs, this machine builds the workbooks.** The
CSVs are text, they compress and diff, and a month of them is about 70 MB.
`output/` is gitignored for the same reason a `build/` directory usually is —
it is derived, and `run_local.py` reproduces it in seconds.

It also removes a collision. GitHub's daily run and your local run would
otherwise both write `output/utilisation/utilisation_log.xlsx`, and every
`git pull` after a local build would stop on a binary conflict that cannot be
merged.

If you want a workbook without building one, every Actions run publishes them
as artifacts. `GITHUB_SETUP.md` section 6.

### `--replay`, and why the raw JSON is kept

Every sample is saved as gzipped JSON exactly as the API returned it, under
`data/raw/`. That is the audit trail, and `--replay` rebuilds the entire CSV
store from it.

Two things this buys you. If a CSV is ever damaged, you recover without having
lost an hour of collection. And if you add a derived column later, you can
recompute it across all history rather than having it start from today — the
numbers come from source, not from a patch.

Replay moves the old store aside as `data/utilisation.before-replay-<stamp>`
rather than deleting it, and it is idempotent: running it twice gives the same
store, not a doubled one.

## Running the pieces by hand

```
python sg_utilisation_logger.py              # take one sample now
python sg_utilisation_logger.py --status     # what has been collected so far
python sg_utilisation_logger.py --rebuild-only   # workbook from existing CSV, no API call
python sg_utilisation_logger.py --rebuild-only --history-days 14   # longer row-level window
python sg_ev_carpark_pipeline.py             # all seven workbooks
python sg_ev_carpark_pipeline.py --no-outer  # inner join only
```

The hourly script needs only `requests`. The pipeline needs `pandas` and
`openpyxl` too, and installs them on first run; `--no-install` turns that off if
you manage your own environment. Or install everything up front with
`pip install -r requirements.txt`.

## The key

Neither script contains an API key. They look in two places, in order:

1. the `LTA_KEY` environment variable
2. `lta_key.txt` in the same folder as the script

`.gitignore` excludes `lta_key.txt`, so it cannot be committed by accident.
Copy `lta_key.txt.example` to `lta_key.txt` and paste your DataMall key in.
`tools/pre-commit` is a hook that blocks any commit containing a key-shaped
string, for the case where one gets pasted into a script; `GITHUB_SETUP.md`
says how to install it.

The key that was hardcoded in early local copies of these scripts (never
committed) was replaced with a new one in November 2026. The new key lives only
in the `LTA_KEY` repository secret and in local `lta_key.txt` files.

## What comes out

```
output/
  utilisation/utilisation_log.xlsx          hourly series, 9 sheets
  combined/ev_carpark_combined_150m.xlsx    inner join, chargers with carparks
  combined/ev_carpark_combined_300m.xlsx
  outer/carpark_ev_outer_150m.xlsx          outer join, the Step 3 candidate pool
  outer/carpark_ev_outer_300m.xlsx
  ev_charging/ev_charging_points.xlsx       chargers only
  carpark/carpark_availability.xlsx         carparks only
  context/datagov_context.xlsx              the six reference datasets

data/
  utilisation/fact/    hourly rows: ids, status, counts. Small, one file per month
  utilisation/dim/     the descriptive columns, one file per day
  raw/ev/, raw/carpark/   gzipped API responses, the audit trail
  reference/           cached copies of the data.gov.sg datasets
```

Filenames are stable. Each run updates the same workbook rather than leaving a
new one, so a link or a Power BI connection keeps working.

### Why the hourly data is split in two

The first design wrote every column on every connector every hour. At 11,429
connectors that is 35 MB a day and roughly 2 GB a month, and 99% of it is the
station name and address repeated 720 times.

So the hourly file carries only what actually changes — connector id, status
code, timestamp — and the descriptive columns are written once a day to a
separate file. Joining the two gives back the full picture. About 70 MB a month
instead of 2 GB, with nothing lost.

`Latest connectors` in the workbook is that join already done: all 23 columns
the pipeline produces, for the most recent sample.

### What accumulates and what does not

This is the part that trips people up. Nothing is ever thrown away, but not
every sheet is a time series.

| Sheet | Behaviour |
|---|---|
| `Network history` | One row per sample, the whole period |
| `Station history` | One row per sample per station, the last 21 days |
| `Carpark history` | One row per sample per carpark, the last 21 days |
| `Station daily`, `Carpark daily` | Daily averages, the whole period |
| `Hourly profile` | Averaged by hour of day, the whole period |
| `Data quality` | One row per check per sample, the whole period |
| `Run history`, `Town history` (combined workbooks) | One row per run, appended |
| `Latest connectors`, `Latest stations`, `Latest carparks` | **The most recent sample only, deliberately** |
| `Matched stations`, `Matched connectors` | Current state, replaced each run |

If a `Latest` sheet shows one moment in time, that is correct — the earlier
samples are in `Station history` and in the daily averages. And if
`Network history` shows two rows after two runs, that is also correct: it gains
one row per sample, so it only looks like a series after a few dozen.

Full history lives in `data/utilisation` as CSV, which has no row limit. The
workbook carries three days at row level by default. Measured at 2,755
stations: three days is 198k rows and 9 MB, a week is 463k rows and 21 MB, and
a complete series passes Excel's 1,048,576-row cap in about fifteen days.
`--history-days N` changes the window; `--history-days 0` leaves the two
history sheets out and keeps the file near 2 MB.

## Data integrity

Both scripts run checks and write the results to a **Data quality** sheet, and
to `data/utilisation/quality.csv`. Worth knowing about the ones that fire:

- **`carparks_have_coordinates`** warns: eight HDB carparks have no coordinates
  in the source data, so they cannot be matched to a charger. Genuine gap.
- **`available_not_over_capacity`** warns occasionally: a carpark reports more
  free lots than it has. A feed error, not ours.

Everything else should read `ok`. The ones worth watching:

- **conservation** on the joins. `1858 + 422 against 2280 carparks` means every
  carpark ended up either matched or unmatched, none lost, none duplicated. If
  that ever fails, the join is wrong and the numbers downstream are wrong.
- **`matches_within_threshold`**. The furthest 150 m match should be under
  150 m. Currently 147.2 m.
- **coordinate bounds**. Anything outside Singapore's bounding box is dropped
  and counted rather than silently mapped into the sea.

## Edge cases the scripts handle

Learned mostly by hitting them:

| Situation | What happens |
|---|---|
| The workbook is open in Excel | Detected before the three-minute fetch, not after. Falls back to a stamped filename rather than crashing |
| A run is killed midway | Files are written to a temp name and moved into place, so a partial file never replaces a good one |
| data.gov.sg returns 429 | Backs off, honours `Retry-After`, falls back to the cached copy |
| The LTA feed returns nothing | Run aborts before touching any output. An empty sample is worse than a missing one |
| A CSV gets corrupted | Set aside as `.corrupt-<stamp>` and collection continues rather than stopping |
| A feed adds a column | Old file set aside as `.schema-<stamp>`, new one started, both readable |
| Two runs overlap | The GitHub `concurrency` group and Task Scheduler's `IgnoreNew` both prevent it |
| A postal code starts with 0 | Kept as text throughout. It is the join key |

## After 48 hours, check this

```
python sg_utilisation_logger.py --status
```

Look at `overnight hours seen:`. It must include 02, 03, 04 and 05.

The fault-detection rule — a connector unavailable in more than 95% of sampled
hours across 14 days, including the small hours, is broken rather than busy —
only works if those hours are in the data. On GitHub they always are. On a
laptop they are exactly the hours it is asleep.
