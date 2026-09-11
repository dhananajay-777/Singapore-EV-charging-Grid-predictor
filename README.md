# Singapore EV charging site selection: data collection

Two scripts. One samples the live feeds every hour, the other rebuilds the
workbooks once a day. Everything else here is setup.

## Start here

| If you want to | Read |
|---|---|
| Run it on GitHub so your laptop can be shut | `GITHUB_SETUP.md` |
| Run it on your own PC | `SCHEDULING.md`, then `setup_windows_tasks.ps1` |
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

## Running them by hand

```
python sg_utilisation_logger.py              # take one sample now
python sg_utilisation_logger.py --status     # what has been collected so far
python sg_utilisation_logger.py --rebuild-only   # workbook from existing CSV, no API call
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

**The key that was hardcoded in the earlier versions of these scripts should be
regenerated at DataMall.** Once a key has been in a file, assume it has leaked.

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
