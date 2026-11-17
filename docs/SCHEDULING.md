# Where to run the collection

## Cadence

| Script | Cadence | Why |
|---|---|---|
| `sg_utilisation_logger.py` | hourly | Connector status changes minute to minute and cannot be back-filled |
| `sg_ev_carpark_pipeline.py` | daily | Both joins and every workbook. Rebuilt from accumulated snapshots, so a missed run costs nothing |

Only the hourly one is time-critical. Miss a day of the other and you rerun it.

The outer-join script is folded into `sg_ev_carpark_pipeline.py`. One run writes
both `output/combined/` and `output/outer/` from a single fetch of the six
context datasets. As two scripts they fetched those six twice per cycle, which
is what produced the HTTP 429s. `--no-outer` skips that half.

## Not Google Colab

A Colab notebook stops when the tab closes, disconnects after about 90 minutes
idle, and is capped at 12 hours. There is no scheduler. It cannot hold an hourly
job for four weeks. Colab is fine for analysing the CSVs afterwards.

## Option A: GitHub Actions — recommended

Runs on GitHub's machines, so nothing depends on your laptop being awake or
online, and it is free for a private repository at this volume.

Full instructions in `GITHUB_SETUP.md`. The short version: push the folder to a
private repo, add `LTA_KEY` as a repository secret, enable Actions. The two
workflow files are already in `.github/workflows/`.

The one caveat: GitHub's cron is best-effort. A run can be delayed ten or twenty
minutes when the platform is busy, and occasionally skipped. Fine here, because
every row records its own `sampled_at` and you analyse against the real
timestamp. Not fine if you needed exact hourly spacing.

## Option B: Windows Task Scheduler

From an Administrator PowerShell, in the project folder:

```
powershell -ExecutionPolicy Bypass -File .\setup_windows_tasks.ps1
```

Add `-LtaKey "your key"` the first time if you have not made `lta_key.txt`.

It registers both tasks, writes `run_hourly.cmd` and `run_daily.cmd`, fires the
hourly one once to prove it works, and prints the result. `LastTaskResult` of 0
means clean; anything else, read `logs\hourly.log`.

Then stop the machine sleeping:

```
powercfg /change standby-timeout-ac 0
```

The script sets `WakeToRun`, which wakes the machine from sleep for a scheduled
run, but it cannot wake a machine that is shut down or hibernating, and on some
laptops Modern Standby ignores wake timers on battery. If the overnight hours
come back missing, that is why, and GitHub Actions is the fix.

**To stop it later:**

```
Disable-ScheduledTask -TaskName "EV utilisation hourly"
Disable-ScheduledTask -TaskName "EV workbooks daily"
```

Or remove them outright with `Unregister-ScheduledTask -TaskName "..." -Confirm:$false`.

## Do not run both

If GitHub Actions is collecting and your PC is too, you get two rows per hour
with slightly different timestamps, and the daily averages quietly weight
whichever machine was awake more. Pick one.

## Either way, check after 48 hours

```
python sg_utilisation_logger.py --status
```

`overnight hours seen:` must list 02, 03, 04 and 05. Those are the hours the
busy-versus-broken rule depends on, and they are exactly the hours a laptop is
asleep.
