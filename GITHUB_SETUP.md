# Running the collection on GitHub

Written for someone who has not used git much. Follow it in order.

---

## 0. Before anything: the API key

**The key must never go into the repository.** Git keeps history, so deleting a
key in a later commit does not remove it from earlier ones. Once pushed, treat it
as public and regenerate it.

Both scripts now read the key from either an environment variable or a file
called `lta_key.txt` that `.gitignore` excludes. Neither script contains a key.

Check before you push anything. In PowerShell, in the project folder:

```
findstr /S /C:"AccountKey" /C:"LTA_KEY =" *.py
```

You should see only the two `_read_key` lines, and no key. If an older copy of a
script still has the key sitting on an `LTA_KEY = "..."` line, delete that file
and use the one in this folder.

**The key that was hardcoded in the earlier versions of these scripts should be
regenerated at DataMall.** Once a key has been written into a file that gets
copied around, assume it has leaked. Regenerating takes a minute and makes the
old one useless.

For local runs, make a file called `lta_key.txt` in the project folder containing
only the key, on one line. `.gitignore` keeps it out.

### A second line of defence

`.gitignore` protects files *named* like secrets. It cannot help if you paste the
key into a `.py` file while debugging and commit that. So there is a hook that
refuses any commit containing something key-shaped. Install it once, after
step 3, from the project folder:

```
cp tools/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

On Windows run those two lines in **Git Bash** (installed with git), not
PowerShell. Test it by pasting a fake key into a script and trying to commit —
it should refuse. Then undo the paste.

If it ever blocks something harmless, `git commit --no-verify` goes through once.

---

## 1. Install git and sign in

1. Download git from <https://git-scm.com/download/win> and install with the
   defaults.
2. Make a GitHub account at <https://github.com> if you do not have one.
3. Open PowerShell in the project folder and set your identity once:

```
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

---

## 2. Create the repository

**Make it private.** The data is public but the repository is yours, and a
private repo is one less thing to worry about with the key.

1. On GitHub press **+** then **New repository**.
2. Name it something like `ev-charging-collection`.
3. Choose **Private**.
4. Do not add a README, .gitignore or licence. The folder already has them.
5. Press **Create repository**. Leave that page open, you need the URL.

---

## 3. Push the folder

In PowerShell, in the project folder:

```
git init
git add .
git status
```

**Stop and read what `git status` prints.** It lists everything about to be
committed. If you see `lta_key.txt` in that list, something is wrong with
`.gitignore`. Fix it before continuing.

Then:

```
git commit -m "EV charging collection scripts"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/ev-charging-collection.git
git push -u origin main
```

The first push opens a browser window to sign in to GitHub. Do that once and it
remembers.

---

## 4. Add the key as a secret

1. In the repository, go to **Settings**, then **Secrets and variables**, then
   **Actions**.
2. Press **New repository secret**.
3. Name: `LTA_KEY` (exactly, capitals included).
4. Secret: paste the DataMall AccountKey.
5. Press **Add secret**.

GitHub hides it after this. The workflows read it as `secrets.LTA_KEY` and it
never appears in the logs.

---

## 5. Turn the schedules on

Go to the **Actions** tab. GitHub asks you to enable workflows on a new
repository; press the green button.

You will see two workflows:

- **Collect hourly** runs `sg_utilisation_logger.py` at five past every hour.
- **Build workbooks daily** runs `sg_ev_carpark_pipeline.py` at 18:10 Singapore
  time and rebuilds the utilisation workbook.

Test the hourly one now rather than waiting:

1. Click **Collect hourly**.
2. Press **Run workflow**, then the green **Run workflow** button.
3. Refresh after a minute. A green tick means it worked. Click into the run and
   open the **Sample the feeds** step to see the numbers it collected.

If it fails, the usual causes are a missing or misspelled `LTA_KEY` secret, or
workflows not enabled.

Then do the same for **Build workbooks daily**. It takes two to three minutes.

### A caveat about GitHub's clock

GitHub's scheduler is best-effort. A run can be delayed ten or twenty minutes
when the platform is busy, and occasionally skipped. That is fine here, because
every row records its own `sampled_at` and you analyse against the real
timestamp. It would not be fine if you needed exact hourly spacing.

---

## 6. Getting the latest output

**Option A: download without git.** In the repository, go to **Actions**, click
the most recent **Build workbooks daily** run, scroll to **Artifacts** at the
bottom, and download `workbooks`. It is a zip of every `.xlsx`, kept for 30 days.

**Option B: browse to one file.** Navigate to `output/combined/` in the
repository, click `ev_carpark_combined_150m.xlsx`, then the **Download** button.

**Option C: pull everything to your PC.** Once, to get a copy:

```
git clone https://github.com/YOUR-USERNAME/ev-charging-collection.git
```

After that, to refresh it:

```
cd ev-charging-collection
git pull
```

`git pull` is the one command you will use most. It downloads whatever the
scheduled runs have committed since you last looked.

### If `git pull` complains

If you have opened a workbook in Excel and it saved changes, git will refuse to
pull over them. The safe fix, which throws away only your local changes to those
files:

```
git checkout -- output/
git pull
```

To avoid it entirely, copy a workbook somewhere else before opening it.

---

## 7. The files, and which one to open

| Path | What it is |
|---|---|
| `output/utilisation/utilisation_log.xlsx` | The hourly series. Latest snapshot, station and carpark daily averages, hourly profile, data quality |
| `output/combined/ev_carpark_combined_150m.xlsx` | Inner join, chargers matched to carparks |
| `output/combined/ev_carpark_combined_300m.xlsx` | Same at the looser threshold |
| `output/outer/carpark_ev_outer_150m.xlsx` | Outer join, the Step 3 candidate pool |
| `output/ev_charging/ev_charging_points.xlsx` | Chargers only |
| `output/carpark/carpark_availability.xlsx` | Carparks only |
| `output/context/datagov_context.xlsx` | The six data.gov.sg reference datasets |

Every one keeps the same filename, so each run refreshes the file rather than
leaving a new one. The combined pair also accumulates `Run history` and
`Town history` sheets across runs.

Full hourly history lives in `data/utilisation` as CSV, which has no row limit.
The workbook holds the latest sample plus daily averages, because at 11,429
connectors an hour the raw history passes Excel's 1,048,576-row cap in about
four days.

To make a workbook from the CSVs at any point, on your own machine:

```
python sg_utilisation_logger.py --rebuild-only
```

---

## 8. Checking it is actually working

**After 48 hours**, this is the check that matters:

```
python sg_utilisation_logger.py --status
```

It prints which hours have been sampled. Look for the line
`overnight hours seen:`. It must list 02, 03, 04 and 05. The
busy-versus-broken rule depends on the window when almost nothing is charging.
If those hours are missing, the collection is not doing its job.

Also open the **Data quality** sheet in any workbook, or `data/utilisation/quality.csv`,
and filter `outcome` to `warn`. One warning is expected and harmless: one HDB
carpark reports more free lots than it has, and eight carparks have no
coordinates.

---

## 8b. When a run goes red

A red X on **Collect hourly** almost always means one of the feeds was down for
that hour. The script exits non-zero on purpose when it gets nothing, so that a
silent gap in the data shows up as a visible failure rather than a quiet
success. Nothing is written, nothing is corrupted, and the next hour carries on
normally. One or two a week is normal.

Worth investigating if it is every run for several hours: check the failed run's
log. `No LTA key` means the secret is missing or misnamed. Repeated `HTTP 429`
means something else is also hammering the API with your key.

**GitHub emails you on every failed scheduled run**, which gets tiresome. To stop
that without turning anything off: your GitHub profile menu, **Settings**,
**Notifications**, then under **Actions** untick *Send notifications for failed
workflows only*, or set it to **Web only**.

---

## 9. Stopping or pausing

**Pause one schedule.** Actions tab, click the workflow, press the **...** menu
at the top right, choose **Disable workflow**. Re-enable it the same way.

**Stop everything.** Disable both workflows the same way.

**Stop permanently.** Delete `.github/workflows/collect-hourly.yml` and
`build-daily.yml`, then:

```
git add -A
git commit -m "stop scheduled collection"
git push
```

**Note:** GitHub disables scheduled workflows automatically after **60 days
without activity** in a repository. For a six-week project that will not bite,
but if the repository goes quiet and the schedule stops, that is why. Pushing
any commit restarts the clock.

---

## 10. The git commands you actually need

| Command | What it does |
|---|---|
| `git pull` | Download what the scheduled runs committed. The one you will use most |
| `git status` | What has changed locally. Read this before committing |
| `git add .` | Stage everything changed, ready to commit |
| `git commit -m "message"` | Save a snapshot locally, with a note |
| `git push` | Send your commits to GitHub |
| `git log --oneline -10` | The last ten commits |
| `git checkout -- <file>` | Throw away your local changes to that file |
| `git reset HEAD <file>` | Unstage a file you added by mistake, keeping the edit |

One trap worth knowing, because it is confusing the first time: `git add` copies
the file into a holding area. After that, `git checkout -- <file>` restores it
*from the holding area*, not from the last commit, so it will not undo something
you have already added. To fully back out an accidental `git add`:

```
git reset HEAD <file>
git checkout -- <file>
```

The normal loop when you change a script:

```
git pull
# edit the file
git add .
git commit -m "what I changed"
git push
```

`git pull` first, every time. It saves you from a conflict with what the
scheduled runs committed while you were away.

---

## 11. Repository size

Roughly what accumulates per month of hourly collection:

| | Per month |
|---|---|
| `data/utilisation/fact` | about 60 MB |
| `data/utilisation/dim` | about 11 MB |
| `data/raw` gzipped snapshots | about 175 MB |
| Workbooks in `output/` | about 25 MB of churn |

Around 270 MB a month, so roughly 400 MB over a six-week project. That is fine
for GitHub, which starts complaining above a few GB. Raw snapshots older than 45
days are deleted automatically; change that with `--keep-days`.

If it does get uncomfortable, uncomment the `data/raw/` line at the bottom of
`.gitignore`. You lose the audit trail on GitHub but keep everything the
analysis uses.
