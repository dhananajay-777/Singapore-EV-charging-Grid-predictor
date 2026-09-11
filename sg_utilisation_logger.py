"""
Hourly sampler for EV connector status and HDB carpark availability.

    python sg_utilisation_logger.py              sample once, append rows
    python sg_utilisation_logger.py --workbook   also rebuild the Excel file
    python sg_utilisation_logger.py --rebuild-only   workbook only, no sampling
    python sg_utilisation_logger.py --status         what has been collected

Why this is separate from sg_ev_carpark_pipeline.py: that script fetches six
data.gov.sg context datasets and takes two to three minutes. This one touches
two endpoints and finishes in seconds, so it can run every hour without
tripping a rate limit.

Storage layout
--------------
    data/raw/ev/ev_charging_raw_<stamp>.json.gz         every response, kept
    data/raw/carpark/carpark_availability_<stamp>.json.gz

    data/utilisation/network.csv          one row per sample, plain text
    data/utilisation/quality.csv          integrity checks, one row per check
    data/utilisation/fact/*.csv.gz        hourly, keyed on an id
    data/utilisation/dim/*.csv.gz         daily, the descriptive columns

    output/utilisation/utilisation_log.xlsx   always the same file, rebuilt
                                              in place from those CSVs

Facts and dimensions are split on purpose. Writing all 11,429 connectors with
their names, operators, ratings and prices every hour comes to roughly 2 GB a
month, which no repository survives. The hourly fact carries an id and a
status; the descriptive columns are captured once a day, because they barely
change. The workbook joins them back together, so the sheet you open has the
same columns as the Connectors sheet in the daily pipeline.

The context layers (planning area, hawker, URA, ACRA) are not fetched here.
Those columns are absent rather than blank, and the daily pipeline adds them.
"""

import argparse
import csv
import gzip
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

HERE = Path(__file__).resolve().parent
DIR_RAW_EV = HERE / "data" / "raw" / "ev"
DIR_RAW_CP = HERE / "data" / "raw" / "carpark"
DIR_UTIL = HERE / "data" / "utilisation"
DIR_FACT = DIR_UTIL / "fact"
DIR_DIM = DIR_UTIL / "dim"
DIR_OUT = HERE / "output" / "utilisation"
WORKBOOK = DIR_OUT / "utilisation_log.xlsx"

def _read_key():
    """Environment first, then a local lta_key.txt that .gitignore excludes.
    No key is written into this file, ever."""
    key = os.environ.get("LTA_KEY", "").strip()
    if key:
        return key
    local = HERE / "lta_key.txt"
    if local.exists():
        return local.read_text(encoding="utf-8").strip()
    return ""


LTA_KEY = _read_key()
EVC_BATCH_URL = "https://datamall2.mytransport.sg/ltaodataservice/EVCBatch"
CARPARK_API = "https://api.data.gov.sg/v1/transport/carpark-availability"

# LTA connector status. "1" is the only value that means a driver can plug in.
# About 3% report blank. They count as unavailable here, the conservative
# reading, and blank_status records how many, so the other reading can be
# recovered without re-collecting.
AVAILABLE = "1"
STATUS_LABEL = {"1": "Available", "0": "Unavailable", "100": "Out of service"}
LOT_TYPES = {"C": "Car", "H": "Heavy vehicle",
             "S": "Motorcycle with side car", "Y": "Motorcycle"}

SG_BOUNDS = (1.15, 1.48, 103.6, 104.1)      # lat min, lat max, lon min, lon max

FACT_CONNECTOR_FIELDS = ["sampled_at", "connector_id", "status_code"]
FACT_STATION_FIELDS = [
    "sampled_at", "location_id", "chargers", "connectors", "available",
    "in_use", "blank_status", "utilisation_pct", "ac_connectors",
    "ac_available", "dc_connectors", "dc_available",
]
FACT_CARPARK_FIELDS = [
    "sampled_at", "carpark_number", "lot_type_code", "total_lots",
    "lots_available", "occupied_lots", "occupancy_pct",
]

# Same names and order as the Connectors sheet in the daily pipeline.
DIM_CONNECTOR_FIELDS = [
    "captured_at", "feed_updated", "connector_id", "location_id",
    "station_name", "address", "postal_code", "latitude", "longitude",
    "charger_index", "charger_name", "operator", "position",
    "operating_hours", "charger_status_code", "charger_status", "plug_type",
    "current", "power_rating_kw", "price", "price_unit",
]
DIM_STATION_FIELDS = [
    "captured_at", "location_id", "station_name", "address", "postal_code",
    "latitude", "longitude", "max_power_kw", "operators", "plug_types",
]
DIM_CARPARK_FIELDS = [
    "captured_at", "carpark_number", "lot_type_code", "lot_type",
    "feed_timestamp", "update_datetime",
]
NETWORK_FIELDS = [
    "sampled_at", "feed_updated", "stations", "chargers", "connectors",
    "available", "in_use", "blank_status", "utilisation_pct",
    "ac_connectors", "ac_in_use", "ac_utilisation_pct",
    "dc_connectors", "dc_in_use", "dc_utilisation_pct",
    "carparks_reporting", "car_lots", "car_lots_available",
    "car_lots_occupied", "car_occupancy_pct", "ev_feed", "carpark_feed",
]
QUALITY_FIELDS = ["sampled_at", "check", "outcome", "detail"]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def num(v):
    """Numeric or None. The feeds mix strings, numbers and empty strings."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f == int(f) else f


def text(v):
    return "" if v is None else str(v).strip()


def get(url, tries=4, **kw):
    """Retry 429 and 5xx with backoff. Returns a response or None."""
    kw.setdefault("timeout", 60)
    for n in range(tries):
        try:
            r = requests.get(url, **kw)
            if r.status_code < 400:
                return r
            if r.status_code in (408, 425, 429, 500, 502, 503, 504):
                wait = min(float(r.headers.get("Retry-After") or 2 * 2 ** n), 60)
                print(f"  HTTP {r.status_code}, waiting {wait:.0f}s "
                      f"(try {n + 1}/{tries})")
                time.sleep(wait)
                continue
            print(f"  HTTP {r.status_code} from {url.split('/')[2]}")
            return None
        except requests.RequestException as exc:
            print(f"  {type(exc).__name__}, retry in {2 * 2 ** n}s")
            time.sleep(2 * 2 ** n)
    return None


def _open(path, mode):
    if str(path).endswith(".gz"):
        return gzip.open(path, mode + "t", newline="", encoding="utf-8")
    return open(path, mode, newline="", encoding="utf-8")


def save_raw(folder, name, payload):
    """Gzipped, and written to a temp name first so a killed run cannot leave
    a half-written snapshot that looks valid."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json.gz"
    tmp = path.with_suffix(".tmp")
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        print(f"  raw snapshot not saved: {exc}")
        return None
    return path


def append_rows(path, fields, rows):
    """Append with a stable header.

    Gzip append writes a second member, which gzip and pandas both read back
    as one file. A header that no longer matches, or a file left unreadable by
    a killed run, is set aside rather than silently mixed with new rows.
    """
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = []
        try:
            with _open(path, "r") as fh:
                existing = next(csv.reader(fh), [])
        except (OSError, EOFError):
            broken = path.with_name(
                f"{path.name}.corrupt-{datetime.now():%Y%m%d%H%M%S}")
            path.rename(broken)
            print(f"  unreadable file set aside as {broken.name}")
        if existing and existing != fields:
            backup = path.with_name(
                f"{path.name}.schema-{datetime.now():%Y%m%d%H%M%S}")
            path.rename(backup)
            print(f"  header changed, previous file kept as {backup.name}")
    new = not path.exists()
    with _open(path, "a") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def read_csv_rows(path):
    if not path.exists():
        return []
    try:
        with _open(path, "r") as fh:
            return list(csv.DictReader(fh))
    except (OSError, EOFError):
        return []


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def parse_ev(snapshot, sampled_at):
    """Connector rows and station rows from one EVCBatch snapshot.

    Shape of the feed, confirmed against live pulls: stations sit under
    evLocationsData rather than value, evIds hang off plugTypes rather than
    the charging point, and there is no locationId, so postalCode is the key.
    """
    if isinstance(snapshot, dict):
        stations = (snapshot.get("evLocationsData") or snapshot.get("value")
                    or snapshot.get("Value"))
        feed_updated = text(snapshot.get("LastUpdatedTime"))
    else:
        stations, feed_updated = snapshot, ""

    conn_rows, stn_rows = [], []
    seen, duplicates = set(), 0

    for st in as_list(stations):
        if not isinstance(st, dict):
            continue
        postal = text(st.get("postalCode"))
        base = {
            "captured_at": sampled_at,
            "feed_updated": feed_updated,
            "location_id": postal,
            "station_name": text(st.get("name")),
            "address": text(st.get("address")),
            "postal_code": postal,
            "latitude": num(st.get("latitude")),
            # the feed misspells it "longtitude"
            "longitude": num(st.get("longtitude", st.get("longitude"))),
        }

        tot = free = blank = ac = dc = ac_free = dc_free = 0
        powers, operators, plugs = [], set(), set()
        points = as_list(st.get("chargingPoints"))

        for idx, cp in enumerate(points, start=1):
            if not isinstance(cp, dict):
                continue
            code = text(cp.get("status"))
            charger = dict(base)
            charger.update({
                "charger_index": idx,
                "charger_name": text(cp.get("name")),
                "operator": text(cp.get("operator")),
                "position": text(cp.get("position")),
                "operating_hours": text(cp.get("operatingHours")
                                        or cp.get("operationHours")),
                "charger_status_code": code,
                "charger_status": STATUS_LABEL.get(code, code),
            })
            if charger["operator"]:
                operators.add(charger["operator"])

            for plug in as_list(cp.get("plugTypes")):
                if not isinstance(plug, dict):
                    continue
                cur = text(plug.get("current")).upper()
                kw = num(plug.get("powerRating"))
                row_plug = dict(charger)
                row_plug.update({
                    "plug_type": text(plug.get("plugType")),
                    "current": cur,
                    "power_rating_kw": kw,
                    "price": num(plug.get("price")),
                    "price_unit": text(plug.get("priceType")),
                })
                if row_plug["plug_type"]:
                    plugs.add(row_plug["plug_type"])
                if kw is not None:
                    powers.append(kw)

                evs = as_list(plug.get("evIds"))
                if not evs:
                    # a plug type with no connector id still describes capacity
                    conn_rows.append(dict(row_plug, connector_id="",
                                          status_code=""))
                    continue

                for ev in evs:
                    if not isinstance(ev, dict):
                        continue
                    raw = text(ev.get("status"))
                    cid = text(ev.get("evCpId"))
                    if cid:
                        if cid in seen:
                            duplicates += 1
                        seen.add(cid)
                    conn_rows.append(dict(row_plug, connector_id=cid,
                                          status_code=raw))
                    tot += 1
                    is_free = raw == AVAILABLE
                    free += is_free
                    blank += raw == ""
                    if cur == "DC":
                        dc += 1
                        dc_free += is_free
                    else:
                        ac += 1
                        ac_free += is_free

        if tot:
            stn_rows.append({
                "captured_at": sampled_at,
                "location_id": base["location_id"],
                "station_name": base["station_name"],
                "address": base["address"],
                "postal_code": base["postal_code"],
                "latitude": base["latitude"],
                "longitude": base["longitude"],
                "chargers": len(points),
                "connectors": tot,
                "available": free,
                "in_use": tot - free,
                "blank_status": blank,
                "utilisation_pct": round(100 * (tot - free) / tot, 1),
                "ac_connectors": ac, "ac_available": ac_free,
                "dc_connectors": dc, "dc_available": dc_free,
                "max_power_kw": max(powers) if powers else None,
                "operators": ", ".join(sorted(operators)),
                "plug_types": ", ".join(sorted(plugs)),
            })

    return conn_rows, stn_rows, feed_updated, duplicates


def parse_carparks(payload, sampled_at):
    rows = []
    for item in as_list((payload or {}).get("items")):
        if not isinstance(item, dict):
            continue
        feed_ts = text(item.get("timestamp"))
        for cp in as_list(item.get("carpark_data")):
            if not isinstance(cp, dict):
                continue
            number = text(cp.get("carpark_number"))
            updated = text(cp.get("update_datetime"))
            for info in as_list(cp.get("carpark_info")):
                if not isinstance(info, dict):
                    continue
                code = text(info.get("lot_type"))
                total = num(info.get("total_lots"))
                avail = num(info.get("lots_available"))
                occupied = occ_pct = None
                # total_lots of 0 means capacity not reported, not a carpark
                # with no lots, so the derived columns stay empty
                if total and total > 0 and avail is not None:
                    occupied = total - avail
                    occ_pct = round(100 * occupied / total, 1)
                rows.append({
                    "captured_at": sampled_at,
                    "feed_timestamp": feed_ts,
                    "carpark_number": number,
                    "lot_type_code": code,
                    "lot_type": LOT_TYPES.get(code, code),
                    "total_lots": total,
                    "lots_available": avail,
                    "occupied_lots": occupied,
                    "occupancy_pct": occ_pct,
                    "update_datetime": updated,
                })
    return rows


# --------------------------------------------------------------------------
# integrity checks
# --------------------------------------------------------------------------

def check_sample(conn_rows, stn_rows, cp_rows, duplicates, sampled_at):
    """Each check records ok or warn with a short reason, into quality.csv, so
    a bad hour can be found later instead of quietly skewing an average."""
    out = []

    def rec(name, ok, detail=""):
        out.append({"sampled_at": sampled_at, "check": name,
                    "outcome": "ok" if ok else "warn", "detail": detail})

    rec("ev_feed_not_empty", bool(conn_rows), f"{len(conn_rows)} connector rows")
    rec("carpark_feed_not_empty", bool(cp_rows), f"{len(cp_rows)} lot rows")
    rec("connector_ids_unique", duplicates == 0,
        f"{duplicates} repeated ids" if duplicates else "no repeats")

    missing = sum(1 for r in stn_rows
                  if r["latitude"] is None or r["longitude"] is None)
    rec("stations_have_coordinates", missing == 0,
        f"{missing} stations without coordinates")

    lo_lat, hi_lat, lo_lon, hi_lon = SG_BOUNDS
    off = sum(1 for r in stn_rows
              if r["latitude"] is not None and r["longitude"] is not None
              and not (lo_lat <= r["latitude"] <= hi_lat
                       and lo_lon <= r["longitude"] <= hi_lon))
    rec("coordinates_inside_singapore", off == 0,
        f"{off} stations outside the Singapore bounding box")

    blank = sum(r["blank_status"] for r in stn_rows)
    total = sum(r["connectors"] for r in stn_rows)
    share = (100 * blank / total) if total else 0
    rec("blank_status_share_normal", share <= 10,
        f"{blank} of {total} connectors blank ({share:.1f}%)")

    bad = sum(1 for r in cp_rows
              if r["total_lots"] and r["lots_available"] is not None
              and r["total_lots"] > 0 and r["lots_available"] > r["total_lots"])
    rec("available_not_above_total", bad == 0,
        f"{bad} carparks report more free lots than they have")

    neg = sum(1 for r in cp_rows
              if (r["total_lots"] or 0) < 0 or (r["lots_available"] or 0) < 0)
    rec("no_negative_lots", neg == 0, f"{neg} negative values")

    dupes = len(cp_rows) - len({(r["carpark_number"], r["lot_type_code"])
                                for r in cp_rows})
    rec("carpark_rows_unique", dupes == 0,
        f"{dupes} duplicate carpark and lot-type pairs")

    # compare against the previous sample: a sudden swing in the connector
    # count means the feed changed shape, not that the network did
    prev = read_csv_rows(DIR_UTIL / "network.csv")
    if prev:
        last = prev[-1]
        before = num(last.get("connectors")) or 0
        swing = abs(total - before) / before * 100 if before else 0
        rec("connector_count_stable", swing <= 10,
            f"{before:.0f} to {total} connectors ({swing:.1f}% change)")
        rec("sample_is_new", text(last.get("sampled_at")) != sampled_at,
            f"previous sample {last.get('sampled_at')}")
    return out


# --------------------------------------------------------------------------
# the workbook
# --------------------------------------------------------------------------

def _load(folder, pattern):
    import pandas as pd
    frames = []
    for f in sorted(folder.glob(pattern)):
        try:
            if f.stat().st_size:
                frames.append(pd.read_csv(f, low_memory=False))
        except Exception as exc:                               # noqa: BLE001
            print(f"  skipped {f.name}: {exc}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def rebuild_workbook():
    """One file, rewritten in place. The latest sample at full grain, history
    aggregated so every sheet stays inside Excel's row limit."""
    try:
        import pandas as pd
    except ImportError:
        print("  pandas not installed, skipping the workbook")
        return None

    net = _load(DIR_UTIL, "network.csv")
    qual = _load(DIR_UTIL, "quality.csv")
    f_conn = _load(DIR_FACT, "connectors_*.csv.gz")
    f_stn = _load(DIR_FACT, "stations_*.csv.gz")
    f_cpk = _load(DIR_FACT, "carparks_*.csv.gz")
    d_conn = _load(DIR_DIM, "connectors_*.csv.gz")
    d_stn = _load(DIR_DIM, "stations_*.csv.gz")
    d_cpk = _load(DIR_DIM, "carparks_*.csv.gz")

    if net.empty and f_stn.empty:
        print("  nothing collected yet, no workbook written")
        return None

    def newest(frame, col="captured_at"):
        if frame.empty or col not in frame.columns:
            return frame
        return frame[frame[col] == frame[col].max()].drop(columns=[col])

    sheets = {}
    if not net.empty:
        sheets["Network history"] = net.sort_values("sampled_at")

    # latest connectors, with the descriptive columns joined back on
    if not f_conn.empty:
        latest = f_conn["sampled_at"].max()
        snap = f_conn[f_conn["sampled_at"] == latest].copy()
        dim = newest(d_conn)
        if not dim.empty and "connector_id" in dim.columns:
            dim = dim.drop_duplicates("connector_id")
            snap = snap.merge(dim, on="connector_id", how="left")
        # pandas reads "1" as 1.0 from CSV, so normalise before labelling
        code = (snap["status_code"].astype("string")
                .str.replace(r"\.0$", "", regex=True).fillna(""))
        snap["status_code"] = code
        snap["connector_status"] = code.map(STATUS_LABEL).fillna(code)
        sheets["Latest connectors"] = snap

    if not f_stn.empty:
        latest = f_stn["sampled_at"].max()
        snap = f_stn[f_stn["sampled_at"] == latest].copy()
        dim = newest(d_stn)
        if not dim.empty and "location_id" in dim.columns:
            snap = snap.merge(dim.drop_duplicates("location_id"),
                              on="location_id", how="left")
        sheets["Latest stations"] = snap

        s = f_stn.copy()
        s["date"] = s["sampled_at"].astype(str).str.slice(0, 10)
        sheets["Station daily"] = (
            s.groupby(["date", "location_id"], dropna=False)
             .agg(samples=("connectors", "size"),
                  connectors=("connectors", "median"),
                  mean_in_use=("in_use", "mean"),
                  mean_utilisation_pct=("utilisation_pct", "mean"),
                  peak_utilisation_pct=("utilisation_pct", "max"),
                  dc_connectors=("dc_connectors", "median"))
             .round(2).reset_index())

        # the shape the busy-versus-broken rule reads
        h = f_stn.copy()
        h["hour"] = h["sampled_at"].astype(str).str.slice(11, 13)
        sheets["Hourly profile"] = (
            h.groupby("hour", dropna=False)
             .agg(samples=("utilisation_pct", "size"),
                  stations=("location_id", "nunique"),
                  mean_utilisation_pct=("utilisation_pct", "mean"),
                  mean_in_use=("in_use", "mean"))
             .round(2).reset_index())

    if not f_cpk.empty:
        latest = f_cpk["sampled_at"].max()
        snap = f_cpk[f_cpk["sampled_at"] == latest].copy()
        dim = newest(d_cpk)
        if not dim.empty and {"carpark_number", "lot_type_code"} <= set(dim.columns):
            snap = snap.merge(
                dim.drop_duplicates(["carpark_number", "lot_type_code"]),
                on=["carpark_number", "lot_type_code"], how="left")
        sheets["Latest carparks"] = snap

        car = f_cpk[f_cpk["lot_type_code"] == "C"].copy()
        if not car.empty:
            car["date"] = car["sampled_at"].astype(str).str.slice(0, 10)
            sheets["Carpark daily"] = (
                car.groupby(["date", "carpark_number"], dropna=False)
                   .agg(samples=("total_lots", "size"),
                        total_lots=("total_lots", "median"),
                        mean_available=("lots_available", "mean"),
                        mean_occupancy_pct=("occupancy_pct", "mean"))
                   .round(2).reset_index())

    if not qual.empty:
        sheets["Data quality"] = qual.sort_values("sampled_at", ascending=False)

    sheets["Notes"] = pd.DataFrame({"note": [
        "Rebuilt in place by --workbook. Same filename every time.",
        "Latest sheets hold the most recent sample at full grain, with the "
        "descriptive columns joined on from the daily dimension snapshot.",
        "Full history lives in data/utilisation as CSV, which has no row limit. "
        "Station daily and Carpark daily are averaged per day so the workbook "
        "stays inside Excel's 1,048,576-row cap.",
        "utilisation_pct counts any connector not reporting status 1 as in use. "
        "blank_status records how many reported nothing, so the stricter "
        "reading can be recovered without re-collecting.",
        "Hourly profile is what the busy-versus-broken rule reads: a connector "
        "unavailable through the 02:00 to 05:00 window is faulty, not popular.",
        "Data quality has one row per check per sample. Filter outcome = warn.",
        "The context layers (planning area, hawker, URA, ACRA) are not fetched "
        "here. The daily pipeline adds them.",
    ]})

    DIR_OUT.mkdir(parents=True, exist_ok=True)
    tmp = WORKBOOK.with_name(f".{WORKBOOK.stem}.writing.xlsx")
    try:
        with pd.ExcelWriter(tmp, engine="openpyxl") as xl:
            for name, frame in sheets.items():
                frame.to_excel(xl, sheet_name=name[:31], index=False)
                ws = xl.sheets[name[:31]]
                ws.freeze_panes = "A2"
                for i, col in enumerate(frame.columns, start=1):
                    longest = pd.to_numeric(
                        frame[col].astype(str).str.len().max(), errors="coerce")
                    width = max(len(str(col)),
                                int(longest) if pd.notna(longest) else 0)
                    ws.column_dimensions[
                        ws.cell(row=1, column=i).column_letter
                    ].width = min(max(width + 2, 10), 42)
    except Exception as exc:                                   # noqa: BLE001
        tmp.unlink(missing_ok=True)
        print(f"  workbook not written: {exc}")
        return None

    try:
        os.replace(tmp, WORKBOOK)
        print(f"  wrote {WORKBOOK.relative_to(HERE)}  ({len(sheets)} sheets)")
        return WORKBOOK
    except PermissionError:
        alt = WORKBOOK.with_name(
            f"{WORKBOOK.stem}_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
        os.replace(tmp, alt)
        print(f"  !! {WORKBOOK.name} is open in Excel, wrote {alt.name} instead")
        return alt


def prune_raw(days):
    """Raw snapshots are kept for audit, not forever."""
    if days <= 0:
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    for folder in (DIR_RAW_EV, DIR_RAW_CP):
        if not folder.exists():
            continue
        for f in folder.glob("*.json.gz"):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


def show_status():
    if not DIR_UTIL.exists():
        print("nothing collected yet")
        return
    net = read_csv_rows(DIR_UTIL / "network.csv")
    if net:
        print(f"network.csv: {len(net)} samples, "
              f"{net[0]['sampled_at']} to {net[-1]['sampled_at']}")
        hours = {r["sampled_at"][11:13] for r in net}
        overnight = sorted(h for h in hours if h in ("02", "03", "04", "05"))
        print("overnight hours seen: " + (", ".join(overnight) if overnight else
              "NONE. The fault rule needs samples between 02:00 and 05:00"))
    for folder, label in ((DIR_FACT, "fact"), (DIR_DIM, "dim")):
        for f in sorted(folder.glob("*.csv.gz")) if folder.exists() else []:
            print(f"{label}/{f.name}: {f.stat().st_size / 1e6:.1f} MB")
    q = read_csv_rows(DIR_UTIL / "quality.csv")
    warns = [r for r in q if r["outcome"] != "ok"]
    print(f"quality.csv: {len(q)} checks, {len(warns)} warnings")
    for r in warns[-5:]:
        print(f"  {r['sampled_at']}  {r['check']}: {r['detail']}")
    raw = len(list(DIR_RAW_EV.glob("*.gz"))) if DIR_RAW_EV.exists() else 0
    print(f"raw EV snapshots kept: {raw}")


# --------------------------------------------------------------------------

def fetch_ev():
    if not LTA_KEY:
        print("  No LTA key. Set LTA_KEY in the environment, or put it in "
              "lta_key.txt beside this script.")
        return None
    r = get(EVC_BATCH_URL,
            headers={"AccountKey": LTA_KEY, "accept": "application/json"})
    if r is None:
        return None
    try:
        records = r.json().get("value") or []
    except ValueError:
        print("  EVCBatch did not return JSON")
        return None
    link = next((rec.get("Link") or rec.get("link") for rec in records
                 if isinstance(rec, dict)
                 and (rec.get("Link") or rec.get("link"))), None)
    if not link:
        print("  no download link in the EVCBatch response")
        return None
    blob = get(link, timeout=120)
    if blob is None:
        return None
    try:
        return blob.json()
    except ValueError:
        print("  snapshot link did not return JSON")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workbook", action="store_true",
                    help="rebuild output/utilisation/utilisation_log.xlsx")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="rebuild the workbook from what is already stored, "
                         "without taking a new sample")
    ap.add_argument("--status", action="store_true",
                    help="report what has been collected and stop")
    ap.add_argument("--no-raw", action="store_true",
                    help="do not keep the raw JSON snapshots")
    ap.add_argument("--keep-days", type=int, default=45,
                    help="delete raw snapshots older than this (0 keeps all)")
    args = ap.parse_args()

    if args.status:
        show_status()
        return

    if args.rebuild_only:
        rebuild_workbook()
        return

    for d in (DIR_RAW_EV, DIR_RAW_CP, DIR_UTIL, DIR_FACT, DIR_DIM, DIR_OUT):
        d.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    sampled_at = now.isoformat(timespec="seconds")
    stamp = now.strftime("%Y%m%d_%H%M%S")
    month = now.strftime("%Y%m")
    today = now.strftime("%Y%m%d")

    snapshot = fetch_ev()
    payload = None
    r = get(CARPARK_API)
    if r is not None:
        try:
            payload = r.json()
        except ValueError:
            print("  carpark API did not return JSON")

    # One feed being down should not lose the other. Nothing at all is a
    # failure, and the run exits non-zero so a scheduler can flag it.
    if snapshot is None and payload is None:
        sys.exit(f"{sampled_at}  both feeds unavailable, nothing written")

    if not args.no_raw:
        if snapshot is not None:
            save_raw(DIR_RAW_EV, f"ev_charging_raw_{stamp}", snapshot)
        if payload is not None:
            save_raw(DIR_RAW_CP, f"carpark_availability_{stamp}", payload)

    conn_rows, stn_rows, feed_updated, dupes = (
        parse_ev(snapshot, sampled_at) if snapshot is not None
        else ([], [], "", 0))
    cp_rows = parse_carparks(payload, sampled_at) if payload is not None else []

    if snapshot is not None and not conn_rows:
        print("  EV snapshot parsed to zero connectors, check the raw file")

    tot = sum(r["connectors"] for r in stn_rows)
    free = sum(r["available"] for r in stn_rows)
    ac = sum(r["ac_connectors"] for r in stn_rows)
    acf = sum(r["ac_available"] for r in stn_rows)
    dc = sum(r["dc_connectors"] for r in stn_rows)
    dcf = sum(r["dc_available"] for r in stn_rows)

    car = [r for r in cp_rows
           if r["lot_type_code"] == "C" and (r["total_lots"] or 0) > 0]
    lots = sum(r["total_lots"] for r in car)
    lots_free = sum(r["lots_available"] or 0 for r in car)

    network = {
        "sampled_at": sampled_at, "feed_updated": feed_updated,
        "stations": len(stn_rows),
        "chargers": sum(r["chargers"] for r in stn_rows),
        "connectors": tot, "available": free, "in_use": tot - free,
        "blank_status": sum(r["blank_status"] for r in stn_rows),
        "utilisation_pct": round(100 * (tot - free) / tot, 1) if tot else None,
        "ac_connectors": ac, "ac_in_use": ac - acf,
        "ac_utilisation_pct": round(100 * (ac - acf) / ac, 1) if ac else None,
        "dc_connectors": dc, "dc_in_use": dc - dcf,
        "dc_utilisation_pct": round(100 * (dc - dcf) / dc, 1) if dc else None,
        "carparks_reporting": len(car), "car_lots": lots,
        "car_lots_available": lots_free,
        "car_lots_occupied": lots - lots_free,
        "car_occupancy_pct": (round(100 * (lots - lots_free) / lots, 1)
                              if lots else None),
        "ev_feed": "ok" if snapshot is not None else "unavailable",
        "carpark_feed": "ok" if payload is not None else "unavailable",
    }

    quality = check_sample(conn_rows, stn_rows, cp_rows, dupes, sampled_at)

    # hourly facts
    append_rows(DIR_FACT / f"connectors_{month}.csv.gz", FACT_CONNECTOR_FIELDS,
                [{"sampled_at": sampled_at, "connector_id": r["connector_id"],
                  "status_code": r["status_code"]}
                 for r in conn_rows if r["connector_id"]])
    append_rows(DIR_FACT / f"stations_{month}.csv.gz", FACT_STATION_FIELDS,
                [dict(r, sampled_at=sampled_at) for r in stn_rows])
    append_rows(DIR_FACT / f"carparks_{month}.csv.gz", FACT_CARPARK_FIELDS,
                [dict(r, sampled_at=sampled_at) for r in cp_rows])
    append_rows(DIR_UTIL / "network.csv", NETWORK_FIELDS, [network])
    append_rows(DIR_UTIL / "quality.csv", QUALITY_FIELDS, quality)

    # daily dimensions: written on the first run of each day
    dim_conn = DIR_DIM / f"connectors_{today}.csv.gz"
    if conn_rows and not dim_conn.exists():
        append_rows(dim_conn, DIM_CONNECTOR_FIELDS, conn_rows)
        append_rows(DIR_DIM / f"stations_{today}.csv.gz", DIM_STATION_FIELDS,
                    stn_rows)
        print(f"  captured today's dimension snapshot ({len(conn_rows):,} "
              "connectors with full attributes)")
    dim_cpk = DIR_DIM / f"carparks_{today}.csv.gz"
    if cp_rows and not dim_cpk.exists():
        append_rows(dim_cpk, DIM_CARPARK_FIELDS, cp_rows)

    warns = [q for q in quality if q["outcome"] != "ok"]
    print(f"{sampled_at}  {len(stn_rows):,} stations, {tot:,} connectors, "
          f"{network['utilisation_pct']}% in use "
          f"(AC {network['ac_utilisation_pct']}%, "
          f"DC {network['dc_utilisation_pct']}%); "
          f"{len(car):,} carparks, {lots:,} car lots, "
          f"{network['car_occupancy_pct']}% occupied")
    for w in warns:
        print(f"  warn  {w['check']}: {w['detail']}")

    if args.keep_days:
        gone = prune_raw(args.keep_days)
        if gone:
            print(f"  pruned {gone} raw snapshots older than {args.keep_days} days")

    if args.workbook:
        rebuild_workbook()


if __name__ == "__main__":
    main()
