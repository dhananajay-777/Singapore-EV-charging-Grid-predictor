#!/usr/bin/env python3
"""
run_local.py - one command to bring everything on this PC up to date.

    python run_local.py

By default it does three things, in order:

  1. git pull        whatever the GitHub runs have collected since last time
  2. a fresh sample  so the workbook includes right now, not just the last hour
  3. rebuild         the workbook, from every sample ever collected

Step 3 is the point. It reads the whole CSV store - what GitHub collected, what
you collected on earlier local runs, and the sample just taken - and writes one
workbook. Nothing is replaced; each run's workbook is built from more data than
the last.

Useful flags:

  --no-pull            skip the git pull (working offline, or no repo)
  --no-fetch           do not take a new sample, just rebuild from what is there
  --history-days N     row-level window in the workbook (default 30 here)
  --replay             rebuild the whole CSV store from the raw JSON snapshots
  --full               also run the daily pipeline: both joins, seven workbooks

Why run locally at all, when GitHub already does it? Because a workbook is a
binary that git cannot diff, so committing one hourly runs to gigabytes. The
CSVs live in the repo and cost almost nothing; the workbook is built here, from
them, as large as you like.
"""

import argparse
import gzip
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGGER = HERE / "sg_utilisation_logger.py"
PIPELINE = HERE / "sg_ev_carpark_pipeline.py"


def load_logger():
    """Import the collector as a module so the parsers, field lists and
    workbook builder are shared. One parser, not two that drift apart."""
    if not LOGGER.exists():
        sys.exit(f"sg_utilisation_logger.py not found beside this script "
                 f"(looked in {HERE})")
    spec = importlib.util.spec_from_file_location("sgu", LOGGER)
    mod = importlib.util.module_from_spec(spec)
    sys.argv = [sys.argv[0]]          # the logger parses argv at import
    spec.loader.exec_module(mod)
    return mod


def say(msg):
    print(f"  {msg}", flush=True)


# ---------------------------------------------------------------- 1. pull

def git_pull():
    if not (HERE / ".git").exists():
        say("not a git repository, skipping the pull")
        return
    if not shutil.which("git"):
        say("git is not on PATH, skipping the pull")
        return
    print("pulling what GitHub has collected ...")
    r = subprocess.run(["git", "pull", "--rebase", "--autostash"],
                       cwd=HERE, capture_output=True, text=True, timeout=180)
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        say(out.splitlines()[-1] if out else "up to date")
        return
    # A conflict here is almost always a workbook the user opened in Excel.
    say(f"pull failed: {out.splitlines()[-1] if out else r.returncode}")
    say("carrying on with the data already on this PC")
    subprocess.run(["git", "rebase", "--abort"], cwd=HERE,
                   capture_output=True, text=True)


# ---------------------------------------------------------------- 2. sample

def take_sample(sgu):
    print("taking a fresh sample ...")
    if not sgu.LTA_KEY:
        say("no LTA key, so the charger feed will be skipped")
        say("put the key in lta_key.txt beside this script, or set LTA_KEY")
    r = subprocess.run([sys.executable, str(LOGGER)], cwd=HERE, text=True)
    if r.returncode != 0:
        say("the sample did not complete; rebuilding from what is already here")
    return r.returncode == 0


# ---------------------------------------------------------------- replay

# the collector writes %Y%m%d_%H%M%S; the underscore is optional here so a
# snapshot renamed or copied by hand still replays instead of being dropped
STAMP = re.compile(r"(\d{8})_?(\d{6})")


def stamp_to_iso(path):
    """Recover the sample time from the filename the collector wrote:
    ev_charging_raw_20260911_080500.json.gz -> 2026-09-11T08:05:00"""
    m = STAMP.search(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2),
                                 "%Y%m%d%H%M%S").isoformat(timespec="seconds")
    except ValueError:
        return None


def read_json_gz(path):
    try:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:                                   # noqa: BLE001
        say(f"unreadable, skipped: {path.name} ({exc})")
        return None


def replay(sgu):
    """Rebuild the whole CSV store from the raw snapshots.

    The raw JSON is the audit trail: every sample the collector ever took,
    exactly as the API returned it. Replaying is how you recover if a CSV is
    damaged, and how you backfill a column added after the fact - the numbers
    are recomputed from source rather than patched.

    The existing store is moved aside first, never deleted.
    """
    ev = sorted(sgu.DIR_RAW_EV.glob("*.json*")) if sgu.DIR_RAW_EV.exists() else []
    cp = sorted(sgu.DIR_RAW_CP.glob("*.json*")) if sgu.DIR_RAW_CP.exists() else []
    if not ev and not cp:
        say("no raw snapshots found, nothing to replay")
        return False

    print(f"replaying {len(ev)} charger and {len(cp)} carpark snapshots ...")

    by_stamp = {}
    for path, kind in [(p, "ev") for p in ev] + [(p, "cp") for p in cp]:
        iso = stamp_to_iso(path)
        if iso:
            by_stamp.setdefault(iso, {})[kind] = path
        else:
            say(f"no timestamp in the name, skipped: {path.name}")

    if not by_stamp:
        say("no snapshot had a readable timestamp, nothing to replay")
        return False

    # move the old store aside rather than writing over it
    keep = sgu.DIR_UTIL.with_name(
        f"utilisation.before-replay-{datetime.now():%Y%m%d_%H%M%S}")
    if sgu.DIR_UTIL.exists():
        shutil.move(str(sgu.DIR_UTIL), str(keep))
        say(f"previous store kept at {keep.name}")
    for d in (sgu.DIR_FACT, sgu.DIR_DIM):
        d.mkdir(parents=True, exist_ok=True)

    seen_dim_days = set()
    done = 0
    for iso in sorted(by_stamp):
        pair = by_stamp[iso]
        month = iso[:4] + iso[5:7]
        day = iso[:4] + iso[5:7] + iso[8:10]

        conn_rows = stn_rows = []
        dupes = 0
        if "ev" in pair:
            snap = read_json_gz(pair["ev"])
            if snap is not None:
                conn_rows, stn_rows, _feed, dupes = sgu.parse_ev(snap, iso)

        cp_rows = []
        if "cp" in pair:
            payload = read_json_gz(pair["cp"])
            if payload is not None:
                cp_rows = sgu.parse_carparks(payload, iso)

        if not conn_rows and not stn_rows and not cp_rows:
            continue

        if conn_rows:
            sgu.append_rows(
                sgu.DIR_FACT / f"connectors_{month}.csv.gz",
                sgu.FACT_CONNECTOR_FIELDS,
                [{"sampled_at": iso, "connector_id": r["connector_id"],
                  "status_code": r["status_code"]}
                 for r in conn_rows if r["connector_id"]])
        if stn_rows:
            sgu.append_rows(sgu.DIR_FACT / f"stations_{month}.csv.gz",
                            sgu.FACT_STATION_FIELDS,
                            [dict(r, sampled_at=iso) for r in stn_rows])
        if cp_rows:
            sgu.append_rows(sgu.DIR_FACT / f"carparks_{month}.csv.gz",
                            sgu.FACT_CARPARK_FIELDS,
                            [dict(r, sampled_at=iso) for r in cp_rows])

        # one dimension snapshot per day, same rule the collector uses
        if day not in seen_dim_days:
            if conn_rows:
                sgu.append_rows(sgu.DIR_DIM / f"connectors_{day}.csv.gz",
                                sgu.DIM_CONNECTOR_FIELDS, conn_rows)
                sgu.append_rows(sgu.DIR_DIM / f"stations_{day}.csv.gz",
                                sgu.DIM_STATION_FIELDS, stn_rows)
            if cp_rows:
                sgu.append_rows(sgu.DIR_DIM / f"carparks_{day}.csv.gz",
                                sgu.DIM_CARPARK_FIELDS, cp_rows)
            seen_dim_days.add(day)

        # the network summary row for this sample
        tot = sum(r["connectors"] for r in stn_rows)
        free = sum(r["available"] for r in stn_rows)
        lots = sum(r["total_lots"] or 0 for r in cp_rows
                   if r.get("lot_type_code") == "C")
        lots_free = sum(r["lots_available"] or 0 for r in cp_rows
                        if r.get("lot_type_code") == "C")
        sgu.append_rows(sgu.DIR_UTIL / "network.csv", sgu.NETWORK_FIELDS, [{
            "sampled_at": iso, "stations": len(stn_rows), "connectors": tot,
            "available": free, "in_use": tot - free,
            "utilisation_pct": round(100 * (tot - free) / tot, 1) if tot else None,
            "carparks_reporting": len(cp_rows), "car_lots": lots,
            "car_lots_available": lots_free, "car_lots_occupied": lots - lots_free,
            "car_occupancy_pct": (round(100 * (lots - lots_free) / lots, 1)
                                  if lots else None),
            "ev_feed": "ok" if stn_rows else "unavailable",
            "carpark_feed": "ok" if cp_rows else "unavailable",
            "duplicate_connector_ids": dupes,
        }])
        done += 1
        if done % 25 == 0:
            say(f"{done} of {len(by_stamp)} samples replayed")

    say(f"replayed {done} samples into a fresh CSV store")
    if done == 0:
        say("nothing was written; the previous store is still in "
            f"{keep.name} if you need it back")
    return done > 0


# ---------------------------------------------------------------- 3. build

def build(sgu, history_days):
    print("rebuilding the workbook from everything collected ...")
    t0 = time.time()
    path = sgu.rebuild_workbook(history_days)
    if path is None:
        return None
    say(f"took {time.time() - t0:.0f}s")
    return path


def summarise(sgu, path):
    try:
        import pandas as pd
    except ImportError:
        return
    try:
        xl = pd.ExcelFile(path)
    except Exception:                                          # noqa: BLE001
        return
    print("\nwhat is in it:")
    for name in xl.sheet_names:
        try:
            n = len(pd.read_excel(xl, name))
        except Exception:                                      # noqa: BLE001
            continue
        print(f"  {name:<20} {n:>9,} rows")
    try:
        net = pd.read_excel(xl, "Network history")
        ts = net["sampled_at"].astype(str)
        hours = sorted({t[11:13] for t in ts if len(t) > 12})
        print(f"\n  {len(net)} samples, {ts.min()[:16]} to {ts.max()[:16]}")
        missing = [h for h in ("02", "03", "04", "05") if h not in hours]
        if missing:
            print(f"  overnight hours still missing: {', '.join(missing)} "
                  "- the busy-versus-broken rule needs these")
        else:
            print("  overnight window 02-05 covered")
    except Exception:                                          # noqa: BLE001
        pass


def run_pipeline():
    if not PIPELINE.exists():
        say("sg_ev_carpark_pipeline.py not found, skipping --full")
        return
    print("\nrunning the daily pipeline (both joins, two to three minutes) ...")
    subprocess.run([sys.executable, str(PIPELINE), "--no-archive"],
                   cwd=HERE, text=True)


def main():
    ap = argparse.ArgumentParser(
        description="Pull, sample, and rebuild the workbook from all history.")
    ap.add_argument("--no-pull", action="store_true",
                    help="skip the git pull")
    ap.add_argument("--no-fetch", action="store_true",
                    help="do not take a new sample, rebuild from what is here")
    ap.add_argument("--history-days", type=int, default=30,
                    help="days of row-level history in the workbook "
                         "(default 30; 0 leaves those sheets out)")
    ap.add_argument("--replay", action="store_true",
                    help="rebuild the CSV store from the raw JSON snapshots")
    ap.add_argument("--full", action="store_true",
                    help="also run the daily pipeline: both joins, 7 workbooks")
    args = ap.parse_args()

    started = time.time()
    sgu = load_logger()

    if not args.no_pull:
        git_pull()

    if args.replay:
        replay(sgu)

    if not args.no_fetch:
        take_sample(sgu)

    path = build(sgu, args.history_days)
    if path is None:
        print("\nNo workbook written - nothing has been collected yet.")
        print("Run it again once a sample has landed, or use --no-pull "
              "--no-fetch after a git pull.")
        return 1

    summarise(sgu, path)

    if args.full:
        run_pipeline()

    print(f"\ndone in {time.time() - started:.0f}s")
    print(f"open: {path}")
    print("\nThe workbook is not committed - it is rebuilt here from the CSVs, "
          "which are.\nRun this again any time to fold in whatever GitHub has "
          "collected since.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
