"""
Singapore EV chargers x carpark availability x data.gov.sg context layers.

One run collects every feed and produces both joins:

    the INNER join, one row per EV station matched to its carpark, which
    answers "what is at the chargers we have"; and

    the OUTER join, every carpark and every station, which answers "where is
    there parking and no charger" and is the Step 3 candidate pool.

Both were separate scripts. Merging them means the six data.gov.sg context
datasets are fetched once per run rather than twice, which is what triggered
the rate limit when both were scheduled.

    python sg_ev_carpark_pipeline.py

Flags:
    --offline      replay the newest saved LTA/carpark JSON in data/raw
    --keep-temp    keep the data.gov.sg downloads in data/temp instead of
                   deleting them at the end
    --no-archive   overwrite the combined workbooks without archiving the
                   previous copy
    --no-outer     skip the outer-join workbooks, inner join only
    --light        skip the ACRA download (the slowest of the six)
    --no-install   never pip install missing packages, just fail

The DataMall key is read from the LTA_KEY environment variable, or from a
file called lta_key.txt beside this script. Neither is committed. There is no
key in this file and none should ever be added.

Folder layout, all relative to this script:

    data/raw/ev/      ev_charging_raw_<stamp>.json.gz       kept
    data/raw/carpark/ carpark_availability_<stamp>.json.gz  kept
    data/reference/   hdb_carpark_information.csv           cached fallback
    data/temp/        the six data.gov.sg downloads          deleted after the run
    output/ev_charging/       ev_charging_points.xlsx
    output/carpark/           carpark_availability.xlsx
    output/context/           datagov_context.xlsx
    output/combined/          ev_carpark_combined_150m.xlsx   updated in place
                              ev_carpark_combined_300m.xlsx   updated in place
    output/archive/           previous copies of the combined workbooks
    output/outer/             carpark_ev_outer_150m.xlsx
                              carpark_ev_outer_300m.xlsx

Every workbook keeps a stable filename, so each run refreshes the same file
rather than leaving a new one behind. The previous copy of the combined pair
is moved to output/archive first, unless --no-archive. Two sheets accumulate rather than being replaced - Run history (one
row per run) and Town history (one row per run per planning area) - so
repeated runs build a time series of carpark availability and charger counts.
"""

import argparse
import gzip
import importlib.util
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

REQUIRED = {"pandas": "pandas", "requests": "requests", "openpyxl": "openpyxl"}


def ensure_deps():
    """openpyxl is the one people forget - pandas only imports it at write
    time, so without this the script dies after the slow network calls."""
    missing = [pkg for mod, pkg in REQUIRED.items()
               if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    print(f"missing packages: {', '.join(missing)} - installing ...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet",
                               *missing])
    except (subprocess.CalledProcessError, OSError):
        sys.exit("could not install automatically. Run:\n"
                 f"    {sys.executable} -m pip install {' '.join(missing)}")
    importlib.invalidate_caches()


if "--no-install" not in sys.argv:
    ensure_deps()

import pandas as pd          # noqa: E402
import requests              # noqa: E402

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent

DIR_RAW = HERE / "data" / "raw"
DIR_RAW_EV = DIR_RAW / "ev"
DIR_RAW_CP = DIR_RAW / "carpark"
DIR_REF = HERE / "data" / "reference"
DIR_TMP = HERE / "data" / "temp"
DIR_EV = HERE / "output" / "ev_charging"
DIR_CP = HERE / "output" / "carpark"
DIR_CTX = HERE / "output" / "context"
DIR_COMB = HERE / "output" / "combined"
DIR_ARCH = HERE / "output" / "archive"
DIR_OUTER = HERE / "output" / "outer"
ALL_DIRS = [DIR_RAW, DIR_RAW_EV, DIR_RAW_CP, DIR_REF, DIR_TMP, DIR_EV, DIR_CP, DIR_CTX, DIR_COMB,
            DIR_ARCH, DIR_OUTER]

# Never hard-code the key. It is read from the environment, or from a local
# lta_key.txt that .gitignore keeps out of the repository.
def _read_key():
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

POLL_DOWNLOAD = ("https://api-open.data.gov.sg/v1/public/api/datasets/"
                 "{ds}/poll-download")
DATASTORE = "https://data.gov.sg/api/action/datastore_search"

# The six data.gov.sg datasets. kind drives how the download is parsed;
# the fetcher tries poll-download first for all of them and falls back to
# datastore_search for the tabular ones.
CONTEXT_DATASETS = [
    {"key": "hdb_carparks", "id": "d_23f946fa557947f93a8043bbef41dd09",
     "title": "HDB Carpark Information", "agency": "HDB", "kind": "table",
     "use": "carpark addresses and SVY21 coordinates - required for the join"},
    {"key": "planning_areas", "id": "d_4765db0e87b9c86336792efe8a1f7a66",
     "title": "Master Plan 2019 Planning Area Boundary (No Sea)", "agency": "URA",
     "kind": "geojson",
     "use": "planning area polygons - assigns every site to a town by "
            "point-in-polygon instead of by street name"},
    {"key": "hawker_geo", "id": "d_4a086da0a5553be1d89383cd90d07ecd",
     "title": "Hawker Centres (GEOJSON)", "agency": "NEA", "kind": "geojson",
     "use": "foot traffic proxy - hawker centre locations and stall counts"},
    {"key": "hawker_list", "id": "d_68a42f09f350881996d83f9cd73ab02f",
     "title": "List of Government Markets Hawker Centres", "agency": "NEA",
     "kind": "table",
     "use": "stall counts to enrich the hawker centre geometry"},
    {"key": "ura_lots", "id": "d_d959102fa76d58f2de276bfbb7e8f68e",
     "title": "URA Parking Lot (GEOJSON)", "agency": "URA", "kind": "geojson",
     "use": "non-HDB parking supply, so the denominator is not HDB-only"},
    {"key": "acra", "id": "d_8575e84912df3c28995b8e6e0e05205a",
     "title": "ACRA Information on Corporate Entities ('A')", "agency": "ACRA",
     "kind": "table", "heavy": True,
     "use": "business density by postal sector - a second foot traffic proxy"},
]

LOT_TYPES = {"C": "Car", "H": "Heavy vehicle",
             "S": "Motorcycle with side car", "Y": "Motorcycle"}

CHARGER_STATUS = {"1": "Available", "0": "Unavailable", "100": "Out of service"}
CONNECTOR_STATUS = dict(CHARGER_STATUS)

# Match tiers: (max distance in metres, min address similarity). Distance
# always binds; similarity corroborates. An unbounded "high similarity wins"
# rule was tried and dropped - it produced island-wide false positives.
THRESHOLDS = {
    "150m": [(50, 0.00), (100, 0.35), (150, 0.35)],
    "300m": [(50, 0.00), (100, 0.35), (150, 0.35), (300, 0.25)],
}
CANDIDATE_RADIUS_M = 350      # pre-filter before scoring EV/carpark pairs
CONTEXT_RADIUS_M = 500        # radius for hawker / URA lot counts
ACRA_ROW_CAP = 200_000        # only bites if poll-download fails and we page

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def write_json_gz(path, payload):
    """Temp file then move, so a killed run cannot leave a snapshot that looks
    valid but is half written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    return path


def read_json(path):
    """Reads .json and .json.gz, so snapshots from either version still load."""
    if str(path).endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def newest_raw(folder, stem):
    """Snapshots written by an older version sat directly in data/raw, so look
    there too rather than telling the user there is nothing to replay."""
    found = sorted(folder.glob(f"{stem}.json.gz")) + sorted(folder.glob(f"{stem}.json"))
    if not found:
        found = (sorted(DIR_RAW.glob(f"{stem}.json.gz"))
                 + sorted(DIR_RAW.glob(f"{stem}.json")))
    return found


def as_list(x):
    """Both LTA feeds are inconsistent about single value vs list."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def svy21_to_wgs84(easting, northing):
    """EPSG:3414 -> EPSG:4326. Inverse transverse Mercator, WGS84 ellipsoid."""
    a, f = 6378137.0, 1 / 298.257223563
    b = a * (1 - f)
    e2 = (a * a - b * b) / (a * a)
    e4, e6 = e2 * e2, e2 * e2 * e2

    lat0, lon0 = math.radians(1.366666), math.radians(103.833333)
    n0, e0, k = 38744.572, 28001.642, 1.0

    A0 = 1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256
    A1 = 3.0 / 8 * (e2 + e4 / 4 + 15 * e6 / 128)
    A2 = 15.0 / 256 * (e4 + 3 * e6 / 4)
    A3 = 35 * e6 / 3072

    def meridian(lat):
        return a * (A0 * lat - A1 * math.sin(2 * lat)
                    + A2 * math.sin(4 * lat) - A3 * math.sin(6 * lat))

    m = meridian(lat0) + (northing - n0) / k
    mu = m / (a * A0)
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    lat = (mu
           + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
           + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
           + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
           + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))

    s, c, t = math.sin(lat), math.cos(lat), math.tan(lat)
    rho = a * (1 - e2) / (1 - e2 * s * s) ** 1.5
    v = a / math.sqrt(1 - e2 * s * s)
    psi = v / rho
    x = (easting - e0) / (k * v)
    x2 = x * x

    lat_out = lat - (k * v * t / rho) * (
        x2 / 2
        - x2 * x2 / 24 * (-4 * psi ** 2 + 9 * psi * (1 - t * t) + 12 * t * t)
        + x2 ** 3 / 720 * (8 * psi ** 4 * (11 - 24 * t * t)
                           - 12 * psi ** 3 * (21 - 71 * t * t)
                           + 15 * psi ** 2 * (15 - 98 * t * t + 15 * t ** 4)
                           + 180 * psi * (5 * t * t - 3 * t ** 4)
                           + 360 * t ** 4)
    ) * k
    lon_out = lon0 + (1 / c) * (
        x
        - x2 * x / 6 * (psi + 2 * t * t)
        + x2 ** 2 * x / 120 * (-4 * psi ** 3 * (1 - 6 * t * t)
                               + psi ** 2 * (9 - 68 * t * t)
                               + 72 * psi * t * t + 24 * t ** 4)
    )
    return math.degrees(lat_out), math.degrees(lon_out)


_ABBREV = {
    "ST": "STREET", "STR": "STREET", "AVE": "AVENUE", "AV": "AVENUE",
    "RD": "ROAD", "CRES": "CRESCENT", "CTRL": "CENTRAL", "CTR": "CENTRE",
    "CENTER": "CENTRE", "DR": "DRIVE", "LOR": "LORONG", "JLN": "JALAN",
    "BLOCK": "BLK", "BLKS": "BLK", "NTH": "NORTH", "STH": "SOUTH",
    "UPP": "UPPER", "TG": "TANJONG", "BT": "BUKIT", "PK": "PARK",
    "PL": "PLACE", "TER": "TERRACE", "CL": "CLOSE", "GDNS": "GARDENS",
    "MKT": "MARKET", "SQ": "SQUARE",
}
_NOISE = {"MSCP", "CAR", "PARK", "CARPARK", "MULTI", "STOREY", "BASEMENT",
          "SURFACE", "SINGAPORE", "S", "THE", "AT", "OF", "AND"}


def canon_address(text):
    """Token set for Jaccard scoring. ST45 -> ST 45 -> STREET 45."""
    if not isinstance(text, str) or not text.strip():
        return set()
    s = text.upper()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=\d)", " ", s)
    s = re.sub(r"(?<=\d)(?=[A-Z])", " ", s)
    out = []
    for tok in s.split():
        tok = _ABBREV.get(tok, tok)
        if tok in _NOISE:
            continue
        out.append(tok)
    return set(out)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def tier_for(dist_m, sim, tiers):
    for max_d, min_s in tiers:
        if dist_m <= max_d and sim >= min_s:
            return f"<={max_d}m" + (f" sim>={min_s:.2f}" if min_s else "")
    return None


def postal_sector(code):
    """First two digits of a Singapore postcode. 038983 -> 03."""
    if code is None:
        return None
    digits = re.sub(r"\D", "", str(code))
    if len(digits) < 5:
        return None
    return digits.zfill(6)[:2]

# --------------------------------------------------------------------------
# data.gov.sg
# --------------------------------------------------------------------------


def http_get(url, tries=5, base_wait=2, **kw):
    """data.gov.sg throttles bursts. Retry 429/5xx and transport errors with
    exponential backoff and return the last response either way."""
    kw.setdefault("timeout", 120)
    last = None
    for n in range(tries):
        try:
            r = requests.get(url, **kw)
            last = r
            if r.status_code < 400:
                return r
            if r.status_code in (408, 425, 429, 500, 502, 503, 504):
                wait = float(r.headers.get("Retry-After") or base_wait * 2 ** n)
                print(f"      HTTP {r.status_code}, waiting {wait:.0f}s "
                      f"(try {n + 1}/{tries})")
                time.sleep(min(wait, 60))
                continue
            return r
        except requests.RequestException as exc:
            last = exc
            print(f"      {type(exc).__name__}, retrying in "
                  f"{base_wait * 2 ** n}s (try {n + 1}/{tries})")
            time.sleep(base_wait * 2 ** n)
    if isinstance(last, requests.Response):
        return last
    raise last if last else RuntimeError("no response")


def dg_poll_download(dataset_id, dest_dir, attempts=6, wait=3):
    """poll-download hands back a signed link once the export is ready.
    Returns the downloaded path, or None."""
    url = POLL_DOWNLOAD.format(ds=dataset_id)
    for n in range(attempts):
        try:
            r = http_get(url, timeout=60)
            if r.status_code >= 400:
                return None, f"poll-download HTTP {r.status_code}"
            body = r.json()
        except Exception as exc:                              # noqa: BLE001
            return None, f"poll-download failed: {exc}"

        if body.get("code") != 0:
            msg = str(body.get("errMsg") or body)[:160]
            if any(w in msg.lower() for w in ("process", "pending", "try again")):
                time.sleep(wait)
                continue
            return None, f"poll-download refused: {msg}"

        link = (body.get("data") or {}).get("url")
        if not link:
            time.sleep(wait)
            continue

        blob = http_get(link, timeout=300)
        if blob.status_code >= 400:
            return None, f"download HTTP {blob.status_code}"
        content = blob.content

        # some exports arrive zipped
        if content[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                inner = next((n for n in z.namelist()
                              if n.lower().endswith((".geojson", ".json", ".csv",
                                                     ".kml"))), None)
                if inner is None:
                    return None, "zip held nothing readable"
                content = z.read(inner)
                suffix = Path(inner).suffix
        else:
            head = content[:200].lstrip()
            suffix = ".json" if head[:1] in (b"{", b"[") else ".csv"

        path = dest_dir / f"{dataset_id}{suffix}"
        path.write_bytes(content)
        return path, None

    return None, "poll-download never produced a link"


def dg_datastore(dataset_id, row_cap=None):
    """Paged datastore_search. Returns a DataFrame with every field."""
    rows, offset, limit, why = [], 0, 10_000, None
    while True:
        try:
            r = http_get(DATASTORE, timeout=120, params={
                "resource_id": dataset_id, "limit": limit, "offset": offset})
            if r.status_code >= 400:
                why = f"datastore_search HTTP {r.status_code}"
                break
            body = r.json()
        except Exception as exc:                              # noqa: BLE001
            why = f"datastore_search failed: {exc}"
            break

        if not body.get("success"):
            why = f"datastore_search refused: {str(body)[:160]}"
            break

        batch = (body.get("result") or {}).get("records") or []
        rows.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        if row_cap and len(rows) >= row_cap:
            print(f"      stopped at the {row_cap:,} row cap")
            break

    df = pd.DataFrame(rows)
    return df.drop(columns=[c for c in ("_id",) if c in df.columns]), why


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", str(text))
    text = (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&nbsp;", " ").replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


def parse_description_table(html):
    """data.gov.sg GEOJSON exports hide the real attributes in an HTML table
    inside properties.Description. Pull the field/value pairs back out.

    Parsed row by row on purpose: a single regex over the whole table lets a
    non-greedy match run past the "Attributes" header cell and glue it onto
    the first field name.
    """
    if not isinstance(html, str) or "<" not in html:
        return {}
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, flags=re.S | re.I)
        if len(cells) != 2:
            continue
        key, val = strip_html(cells[0]), strip_html(cells[1])
        if key and key.lower() != "attributes" and key not in out:
            out[key] = val
    return out


def _flat_coords(geom):
    """Every (lon, lat) in a geometry, ignoring any Z."""
    def walk(node):
        if isinstance(node, (int, float)):
            return
        if node and isinstance(node[0], (int, float)):
            yield (float(node[0]), float(node[1]))
            return
        for child in node or []:
            yield from walk(child)
    return list(walk((geom or {}).get("coordinates")))


def _rings(geom):
    """Outer rings (and holes) as lists of (lon, lat), for point-in-polygon."""
    gtype = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates") or []
    polys = []
    if gtype == "Polygon":
        polys = [coords]
    elif gtype == "MultiPolygon":
        polys = coords
    out = []
    for poly in polys:
        out.append([[(float(p[0]), float(p[1])) for p in ring] for ring in poly])
    return out


def parse_geojson(path):
    """Feature table with every attribute, plus centroid and polygon rings."""
    data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    features = data.get("features") if isinstance(data, dict) else data
    rows = []
    for feat in features or []:
        props = dict(feat.get("properties") or {})
        merged = parse_description_table(props.pop("Description", ""))
        for k, v in props.items():
            merged.setdefault(k, v)

        geom = feat.get("geometry") or {}
        pts = _flat_coords(geom)
        if pts:
            merged["_lon"] = sum(p[0] for p in pts) / len(pts)
            merged["_lat"] = sum(p[1] for p in pts) / len(pts)
        else:
            merged["_lon"] = merged["_lat"] = None
        merged["_geom_type"] = geom.get("type")
        merged["_rings"] = _rings(geom)
        rows.append(merged)
    return pd.DataFrame(rows)


DIR_CACHE_NAME = "context"


def cache_path(key):
    return DIR_REF / DIR_CACHE_NAME / f"{key}.csv.gz"


def save_cache(key, frame):
    try:
        path = cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        out = frame.copy()
        for col in out.columns:
            if out[col].map(lambda v: isinstance(v, (list, dict, tuple))).any():
                out[col] = out[col].map(json.dumps)
        out.to_csv(path, index=False, compression="gzip")
    except Exception:                                          # noqa: BLE001
        pass


def load_cache(key):
    path = cache_path(key)
    if not path.exists():
        return None, None
    try:
        frame = pd.read_csv(path, low_memory=False)
        if "_rings" in frame.columns:
            frame["_rings"] = frame["_rings"].map(
                lambda v: json.loads(v) if isinstance(v, str) and v.startswith("[")
                else [])
        when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
        return frame, when
    except Exception:                                          # noqa: BLE001
        return None, None


def fetch_context(spec, light=False, use_cache=True, refresh=False):
    """poll-download first, datastore_search second, the local cache third.

    These datasets barely change, and data.gov.sg throttles bursts, so a run
    that gets rate-limited falls back to the last good copy rather than
    silently dropping the columns.
    """
    print(f"  {spec['title']} ({spec['agency']}) ...")
    if light and spec.get("heavy"):
        print("      skipped (--light)")
        return None, "skipped (--light)"

    reasons = []
    if not refresh:
        path, why = dg_poll_download(spec["id"], DIR_TMP)
        if why:
            reasons.append(why)
        if path is not None:
            try:
                if spec["kind"] == "geojson" or path.suffix in (".geojson", ".json"):
                    df = parse_geojson(path)
                else:
                    df = pd.read_csv(path, dtype=str, low_memory=False)
                if not df.empty:
                    print(f"      {len(df):,} rows, {len(df.columns)} fields")
                    if use_cache:
                        save_cache(spec["key"], df)
                    return df, "loaded"
                reasons.append("download parsed to zero rows")
            except Exception as exc:                           # noqa: BLE001
                reasons.append(f"parse failed: {exc}")

        if spec["kind"] == "table":
            df, why = dg_datastore(
                spec["id"], row_cap=ACRA_ROW_CAP if spec.get("heavy") else None)
            if why:
                reasons.append(why)
            if not df.empty:
                print(f"      {len(df):,} rows, {len(df.columns)} fields "
                      "(datastore_search)")
                if use_cache:
                    save_cache(spec["key"], df)
                return df, "loaded (datastore_search)"

    if use_cache:
        df, when = load_cache(spec["key"])
        if df is not None and not df.empty:
            print(f"      live fetch failed - using cache from {when} "
                  f"({len(df):,} rows)")
            return df, f"cached {when}"

    note = "; ".join(reasons)[:250] or "unavailable"
    print(f"      unavailable: {note}")
    return None, note


# --------------------------------------------------------------------------
# geometry lookups
# --------------------------------------------------------------------------


def _in_ring(lon, lat, ring):
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            xin = (x2 - x1) * (lat - y1) / (y2 - y1 + 1e-18) + x1
            if lon < xin:
                inside = not inside
    return inside


class AreaIndex:
    """Point-in-polygon over the Master Plan planning areas."""

    def __init__(self, frame, name_col, extra_cols):
        self.name_col = name_col
        self.extra_cols = extra_cols
        self.areas = []
        for _, row in frame.iterrows():
            for poly in row.get("_rings") or []:
                if not poly or not poly[0]:
                    continue
                xs = [p[0] for p in poly[0]]
                ys = [p[1] for p in poly[0]]
                self.areas.append({
                    "row": row,
                    "bbox": (min(xs), min(ys), max(xs), max(ys)),
                    "poly": poly,
                })

    def lookup(self, lon, lat):
        if lon is None or lat is None or pd.isna(lon) or pd.isna(lat):
            return None
        for area in self.areas:
            x0, y0, x1, y1 = area["bbox"]
            if not (x0 <= lon <= x1 and y0 <= lat <= y1):
                continue
            rings = area["poly"]
            if _in_ring(lon, lat, rings[0]) and not any(
                    _in_ring(lon, lat, hole) for hole in rings[1:]):
                return area["row"]
        return None


class PointIndex:
    """Grid index for 'how many of these are within N metres'."""

    CELL = 0.005      # about 550 m

    def __init__(self, lons, lats, payload=None):
        self.pts = [(lo, la) for lo, la in zip(lons, lats)
                    if lo is not None and la is not None
                    and not pd.isna(lo) and not pd.isna(la)]
        self.payload = payload if payload is not None else [None] * len(self.pts)
        self.grid = {}
        for i, (lo, la) in enumerate(self.pts):
            self.grid.setdefault((int(la / self.CELL), int(lo / self.CELL)),
                                 []).append(i)

    def near(self, lat, lon, radius_m):
        if lat is None or lon is None or pd.isna(lat) or pd.isna(lon):
            return []
        span = int(radius_m / (self.CELL * 111_000)) + 1
        gy, gx = int(lat / self.CELL), int(lon / self.CELL)
        hits = []
        for dy in range(-span, span + 1):
            for dx in range(-span, span + 1):
                for i in self.grid.get((gy + dy, gx + dx), ()):
                    lo, la = self.pts[i]
                    d = haversine_m(lat, lon, la, lo)
                    if d <= radius_m:
                        hits.append((d, i))
        hits.sort()
        return hits


# --------------------------------------------------------------------------
# LTA EV charging points
# --------------------------------------------------------------------------


def fetch_ev_snapshot():
    if not LTA_KEY:
        sys.exit("No LTA key. Set the LTA_KEY environment variable, or put the "
                 "key in lta_key.txt beside this script.")
    r = requests.get(EVC_BATCH_URL,
                     headers={"AccountKey": LTA_KEY, "accept": "application/json"},
                     timeout=30)
    if r.status_code == 401:
        sys.exit("401 - LTA key rejected. Check LTA_KEY is the AccountKey from your email.")
    if r.status_code != 200:
        sys.exit(f"LTA HTTP {r.status_code}: {r.text[:300]}")

    link = next((rec.get("Link") or rec.get("link")
                 for rec in (r.json().get("value") or [])
                 if rec.get("Link") or rec.get("link")), None)
    if not link:
        sys.exit(f"No download link in EVCBatch response: {r.text[:300]}")

    print("  got download link, pulling snapshot ...")
    f = requests.get(link, timeout=120)
    f.raise_for_status()
    return f.json()


def flatten_ev(snapshot):
    """Connector-grain table from the EVCBatch snapshot.

    Real shape of the feed (confirmed against a 2026-09-10 pull):

        {"LastUpdatedTime": "...",
         "evLocationsData": [
            {"address", "name", "longtitude", "latitude", "postalCode",
             "chargingPoints": [
                {"status", "operatingHours", "operator", "position", "name",
                 "plugTypes": [
                    {"plugType", "price", "current", "powerRating", "priceType",
                     "evIds": [{"evCpId", "status"}]}]}]}]}

    Note the nesting: evIds hang off plugTypes, not off the charging point, and
    a point can carry more than one plug type. The feed also misspells
    longitude as "longtitude". postalCode is unique per station (2,755 distinct
    on 2026-09-10), so it doubles as the station key.
    """
    if isinstance(snapshot, dict):
        stations = (snapshot.get("evLocationsData")
                    or snapshot.get("value")
                    or snapshot.get("Value"))
        feed_updated = snapshot.get("LastUpdatedTime")
    else:
        stations, feed_updated = snapshot, None

    rows = []
    for st in as_list(stations):
        base = {
            "feed_updated": feed_updated,
            "location_id": st.get("postalCode"),
            "station_name": st.get("name"),
            "address": st.get("address"),
            "postal_code": st.get("postalCode"),
            "latitude": st.get("latitude"),
            # the feed misspells it "longtitude"
            "longitude": st.get("longtitude", st.get("longitude")),
        }

        points = as_list(st.get("chargingPoints"))
        if not points:
            rows.append(base)
            continue

        for n, cp in enumerate(points, start=1):
            charger = dict(base)
            charger.update({
                "charger_index": n,
                "charger_name": cp.get("name"),
                "operator": cp.get("operator"),
                "position": cp.get("position"),
                "operating_hours": cp.get("operatingHours") or cp.get("operationHours"),
                "charger_status_code": cp.get("status"),
                "charger_status": CHARGER_STATUS.get(str(cp.get("status")), cp.get("status")),
            })

            plugs = as_list(cp.get("plugTypes"))
            if not plugs:
                rows.append(charger)
                continue

            for plug in plugs:
                pr = dict(charger)
                pr.update({
                    "plug_type": plug.get("plugType"),
                    "current": plug.get("current"),
                    "power_rating_kw": plug.get("powerRating"),
                    "price": plug.get("price"),
                    "price_unit": plug.get("priceType"),
                })

                # evIds sit under the plug type, not the charging point
                connectors = as_list(plug.get("evIds")) or as_list(cp.get("evIds"))
                if not connectors:
                    rows.append(pr)
                    continue

                for ev in connectors:
                    row = dict(pr)
                    row["connector_id"] = ev.get("evCpId")
                    row["connector_status_code"] = ev.get("status")
                    row["connector_status"] = CONNECTOR_STATUS.get(
                        str(ev.get("status")), ev.get("status"))
                    rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df, df

    for col in ("latitude", "longitude", "power_rating_kw", "price"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # keep as text: postal codes have leading zeros, status codes are labels
    for col in ("location_id", "postal_code", "charger_status_code",
                "connector_status_code"):
        if col in df.columns:
            df[col] = df[col].astype("string")

    order = ["feed_updated", "location_id", "station_name", "address", "postal_code",
             "latitude", "longitude", "charger_index", "charger_name", "operator",
             "position", "operating_hours", "charger_status_code", "charger_status",
             "plug_type", "current", "power_rating_kw", "price", "price_unit",
             "connector_id", "connector_status_code", "connector_status"]
    df = df[[c for c in order if c in df.columns]]

    avail = df["connector_status_code"].astype(str).eq("1")
    stations = (df.assign(_avail=avail,
                          _ac=df["current"].eq("AC"),
                          _dc=df["current"].eq("DC"))
                  .groupby(["location_id", "station_name", "address", "postal_code",
                            "latitude", "longitude"], dropna=False)
                  .agg(chargers=("charger_index", "nunique"),
                       connectors=("connector_id", "count"),
                       available_now=("_avail", "sum"),
                       ac_connectors=("_ac", "sum"),
                       dc_connectors=("_dc", "sum"),
                       max_power_kw=("power_rating_kw", "max"),
                       operators=("operator", lambda s: ", ".join(sorted(set(s.dropna())))),
                       plug_types=("plug_type", lambda s: ", ".join(sorted(set(s.dropna())))),
                       median_price=("price", "median"))
                  .reset_index()
                  .sort_values("connectors", ascending=False))
    return df, stations


# --------------------------------------------------------------------------
# carpark availability (data.gov.sg realtime API)
# --------------------------------------------------------------------------


def fetch_carpark_payload():
    r = requests.get(CARPARK_API, timeout=30)
    if r.status_code != 200:
        sys.exit(f"data.gov.sg HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def flatten_carpark(payload):
    rows = []
    for item in as_list(payload.get("items")):
        feed_time = item.get("timestamp")
        for cp in as_list(item.get("carpark_data")):
            for info in as_list(cp.get("carpark_info")):
                code = info.get("lot_type")
                rows.append({
                    "feed_timestamp": feed_time,
                    "carpark_number": cp.get("carpark_number"),
                    "lot_type_code": code,
                    "lot_type": LOT_TYPES.get(code, code),
                    "total_lots": pd.to_numeric(info.get("total_lots"), errors="coerce"),
                    "lots_available": pd.to_numeric(info.get("lots_available"), errors="coerce"),
                    "update_datetime": cp.get("update_datetime"),
                })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # total_lots of 0 means capacity not reported, not a carpark with no lots
    known = df["total_lots"] > 0
    df["occupied_lots"] = (df["total_lots"] - df["lots_available"]).where(known)
    df["occupancy_pct"] = (df["occupied_lots"] / df["total_lots"] * 100).where(known).round(1)
    return df


# --------------------------------------------------------------------------
# the EV x carpark join
# --------------------------------------------------------------------------


def match_stations_to_carparks(stations, carparks, tiers):
    """Inner join on geography plus address agreement.

    Every EV station is scored against every carpark inside CANDIDATE_RADIUS_M
    using haversine distance and token-level address similarity. Best pair
    wins, one carpark to one station.
    """
    ev = stations.dropna(subset=["latitude", "longitude"]).copy()
    cp = carparks.dropna(subset=["latitude", "longitude"]).copy()
    if ev.empty or cp.empty:
        return pd.DataFrame()

    ev["_tokens"] = ev["address"].map(canon_address)
    cp["_tokens"] = cp["address"].map(canon_address)

    # grid index, ~0.005 deg cells (about 550 m) so a 350 m radius needs 3x3
    cell = 0.005
    grid = {}
    for idx, row in cp.iterrows():
        key = (int(row["latitude"] / cell), int(row["longitude"] / cell))
        grid.setdefault(key, []).append(idx)

    pairs = []
    for ev_idx, e in ev.iterrows():
        gy, gx = int(e["latitude"] / cell), int(e["longitude"] / cell)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for cp_idx in grid.get((gy + dy, gx + dx), ()):
                    c = cp.loc[cp_idx]
                    d = haversine_m(e["latitude"], e["longitude"],
                                    c["latitude"], c["longitude"])
                    if d > CANDIDATE_RADIUS_M:
                        continue
                    sim = jaccard(e["_tokens"], c["_tokens"])
                    tier = tier_for(d, sim, tiers)
                    if tier:
                        pairs.append((ev_idx, cp_idx, d, sim, tier))

    if not pairs:
        return pd.DataFrame()

    # greedy one-to-one: closest first, address similarity breaks ties
    pairs.sort(key=lambda p: (p[2], -p[3]))
    used_ev, used_cp, chosen = set(), set(), []
    for ev_idx, cp_idx, d, sim, tier in pairs:
        if ev_idx in used_ev or cp_idx in used_cp:
            continue
        used_ev.add(ev_idx)
        used_cp.add(cp_idx)
        chosen.append((ev_idx, cp_idx, d, sim, tier))

    ev_part = (ev.drop(columns=["_tokens"])
                 .loc[[c[0] for c in chosen]]
                 .add_prefix("ev_").reset_index(drop=True))
    cp_part = (cp.drop(columns=["_tokens"])
                 .loc[[c[1] for c in chosen]]
                 .add_prefix("cp_").reset_index(drop=True))
    meta = pd.DataFrame({
        "match_distance_m": [round(c[2], 1) for c in chosen],
        "address_similarity": [round(c[3], 3) for c in chosen],
        "match_tier": [c[4] for c in chosen],
    })
    out = pd.concat([meta, ev_part, cp_part], axis=1)
    return out.sort_values("match_distance_m").reset_index(drop=True)

# --------------------------------------------------------------------------
# HDB carpark information (dataset 1 of the six)
# --------------------------------------------------------------------------

REF_CARPARK = "hdb_carpark_information.csv"


def prepare_carpark_info(raw):
    """Normalise HDB Carpark Information and make sure it has lat/lon.
    Falls back to the cached copy in data/reference when the fetch failed."""
    cached = DIR_REF / REF_CARPARK

    if raw is None or raw.empty:
        if cached.exists():
            print(f"  using the cached {REF_CARPARK}")
            raw = pd.read_csv(cached, dtype=str)
        else:
            print("  no carpark address data - the combined workbooks will be skipped")
            return None
    else:
        raw.to_csv(cached, index=False)      # refresh the fallback

    info = raw.copy()
    info.columns = [str(c).strip().lower() for c in info.columns]

    if not {"latitude", "longitude"} <= set(info.columns):
        if {"x_coord", "y_coord"} <= set(info.columns):
            print("  converting SVY21 x/y to lat/lon ...")
            xs = pd.to_numeric(info["x_coord"], errors="coerce")
            ys = pd.to_numeric(info["y_coord"], errors="coerce")
            pairs = [svy21_to_wgs84(x, y) if pd.notna(x) and pd.notna(y)
                     else (None, None) for x, y in zip(xs, ys)]
            info["latitude"] = [p[0] for p in pairs]
            info["longitude"] = [p[1] for p in pairs]
        else:
            info["latitude"] = pd.NA
            info["longitude"] = pd.NA
    else:
        info["latitude"] = pd.to_numeric(info["latitude"], errors="coerce")
        info["longitude"] = pd.to_numeric(info["longitude"], errors="coerce")

    if "car_park_no" not in info.columns:
        print("  carpark file has no car_park_no column - cannot join")
        return None

    keep = [c for c in ("car_park_no", "address", "car_park_type",
                        "type_of_parking_system", "short_term_parking",
                        "free_parking", "night_parking", "car_park_decks",
                        "gantry_height", "car_park_basement",
                        "latitude", "longitude") if c in info.columns]
    return info[keep].rename(columns={"car_park_no": "carpark_number"})


# --------------------------------------------------------------------------
# context layers -> per-site columns
# --------------------------------------------------------------------------


def pick_col(frame, *needles, exclude=()):
    """First column whose name contains every needle. Column names in these
    exports are not stable, so match on substrings rather than hard-code."""
    if frame is None or frame.empty:
        return None
    for col in frame.columns:
        name = str(col).upper()
        if all(n.upper() in name for n in needles) and not any(
                x.upper() in name for x in exclude):
            return col
    return None


def first_text(frame, row, *cols):
    for c in cols:
        if c and c in frame.columns:
            v = row.get(c)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def build_context(raw):
    """Shape the raw data.gov.sg frames into things the joins can match on:
    a text address, a postal code, coordinates."""
    ctx = {"areas": None, "hawker": None, "hawker_index": None,
           "ura": None, "ura_places": None, "ura_index": None,
           "acra": None, "acra_postcode": None, "acra_sector": None,
           "hawker_stalls_col": "_stalls"}

    # ---- planning areas -------------------------------------------------
    pa = raw.get("planning_areas")
    if pa is not None and not pa.empty:
        name_col = pick_col(pa, "PLN_AREA_N") or pick_col(pa, "NAME")
        if name_col:
            ctx["areas"] = AreaIndex(pa, name_col, {})
            ctx["area_cols"] = (name_col, pick_col(pa, "PLN_AREA_C"),
                                pick_col(pa, "REGION_N"))
            print(f"  planning areas: {len(pa)} polygons")

    # ---- hawker centres, with stall counts from the markets list --------
    hg = raw.get("hawker_geo")
    hl = raw.get("hawker_list")
    if hg is not None and not hg.empty:
        hg = hg.copy()
        n_col = pick_col(hg, "NAME", exclude=("STREET", "BUILDING", "BLOCK"))
        blk = pick_col(hg, "ADDRESSBLOCK") or pick_col(hg, "BLOCK")
        street = pick_col(hg, "ADDRESSSTREET") or pick_col(hg, "STREET")
        pcode = pick_col(hg, "ADDRESSPOSTAL") or pick_col(hg, "POSTAL")
        food = pick_col(hg, "FOOD", "STALL")
        market = pick_col(hg, "MARKET", "STALL")

        hg["_name"] = [first_text(hg, r, n_col) for _, r in hg.iterrows()]
        hg["_addr"] = [" ".join(x for x in (first_text(hg, r, blk),
                                            first_text(hg, r, street),
                                            first_text(hg, r, n_col)) if x)
                       for _, r in hg.iterrows()]
        hg["_postal"] = hg[pcode].map(lambda v: re.sub(r"\D", "", str(v)).zfill(6)
                                      if pd.notna(v) else None) if pcode else None
        for col, out in ((food, "_food_stalls"), (market, "_market_stalls")):
            hg[out] = pd.to_numeric(hg[col], errors="coerce") if col else 0
        hg["_stalls"] = hg["_food_stalls"].fillna(0) + hg["_market_stalls"].fillna(0)

        if hl is not None and not hl.empty:
            l_name = pick_col(hl, "NAME") or hl.columns[0]
            l_cooked = pick_col(hl, "COOKED")
            l_produce = pick_col(hl, "PRODUCE") or pick_col(hl, "MARKET", "STALL")
            l_total = pick_col(hl, "NUMBER", "STALL") or pick_col(hl, "STALLS")
            lk = hl.copy()
            for c in (l_cooked, l_produce, l_total):
                if c:
                    lk[c] = pd.to_numeric(lk[c], errors="coerce")
            l_addr = pick_col(hl, "ADDRESS")
            cols = [c for c in (l_cooked, l_produce, l_total) if c]
            lk["_txt"] = (lk[l_name].astype(str) + " "
                          + (lk[l_addr].astype(str) if l_addr else ""))
            lk["_pc"] = lk["_txt"].str.extract(r"(\d{6})")[0]
            if cols:
                lk = lk.groupby("_txt", as_index=False).agg(
                    {**{c: "sum" for c in cols}, "_pc": "first"})

            # exact name equality caught only a small minority of centres, so
            # match on postal code first and token overlap second
            lk_tokens = [canon_address(t) for t in lk["_txt"]]
            by_pc = {pc: i for i, pc in enumerate(lk["_pc"]) if isinstance(pc, str)}
            picks, hits = [], 0
            for _, row in hg.iterrows():
                pc = row.get("_postal")
                j = by_pc.get(pc) if isinstance(pc, str) else None
                if j is None:
                    tok = canon_address(f"{row.get('_name', '')} {row.get('_addr', '')}")
                    best, score = None, 0.34
                    for k, other in enumerate(lk_tokens):
                        sc = jaccard(tok, other)
                        if sc > score:
                            best, score = k, sc
                    j = best
                picks.append(j)
                hits += j is not None

            attached = pd.DataFrame(index=range(len(hg)), columns=cols, dtype=object)
            rows = [i for i, j in enumerate(picks) if j is not None]
            if rows and cols:
                attached.iloc[rows] = lk.iloc[[picks[i] for i in rows]][cols] \
                    .to_numpy(dtype=object)
            for c in cols:
                hg[c] = pd.to_numeric(attached[c], errors="coerce")
            if l_total and l_total in hg.columns:
                hg["_stalls"] = hg["_stalls"].where(
                    hg["_stalls"] > 0, hg[l_total].fillna(0))
            print(f"  hawker: stall counts matched for {hits} of {len(hg)} centres")

        ctx["hawker"] = hg
        ctx["hawker_index"] = PointIndex(hg["_lon"], hg["_lat"])

    # ---- URA parking, rolled up from individual lots to parking places --
    ura = raw.get("ura_lots")
    if ura is not None and not ura.empty:
        ura = ura.copy()
        pp = pick_col(ura, "PP_CODE")
        name = pick_col(ura, "PARKING_PL") or pick_col(ura, "NAME")
        lot = pick_col(ura, "LOT_NO")
        ura["_lon"] = pd.to_numeric(ura["_lon"], errors="coerce")
        ura["_lat"] = pd.to_numeric(ura["_lat"], errors="coerce")

        key = pp or name
        if key:
            grouped = (ura.groupby(key, dropna=False)
                       .agg(_lon=("_lon", "mean"), _lat=("_lat", "mean"),
                            _lots=(key, "size"))
                       .reset_index().rename(columns={key: "_pp_code"}))
            # carry every original attribute of the parking place across
            first = (ura.drop(columns=["_rings"], errors="ignore")
                     .drop_duplicates(subset=[key], keep="first")
                     .rename(columns={key: "_pp_code"}))
            first = first.drop(columns=[c for c in ("_lon", "_lat")
                                        if c in first.columns])
            grouped = grouped.merge(first, on="_pp_code", how="left")
            grouped["_name"] = (grouped[name] if name and name in grouped.columns
                                else grouped["_pp_code"])
            # LOT_NO is a lot identifier in this dataset, so the lot count is
            # the number of features rolled into the parking place
            if lot:
                num = pd.to_numeric(ura[lot], errors="coerce")
                if num.notna().mean() > 0.9 and num.max() > 1000:
                    pass          # looks like an id, keep the feature count
            grouped["_addr"] = grouped["_name"].astype(str)
            grouped["_postal"] = None
            ctx["ura_places"] = grouped
            ctx["ura_index"] = PointIndex(grouped["_lon"], grouped["_lat"])
            print(f"  URA parking: {len(ura):,} lots rolled into "
                  f"{len(grouped):,} parking places")
        ctx["ura"] = ura

    # ---- ACRA, keyed on the full postal code ---------------------------
    acra = raw.get("acra")
    if acra is not None and not acra.empty:
        acra = acra.copy()
        pc = pick_col(acra, "POSTAL")
        status = pick_col(acra, "ENTITY", "STATUS") or pick_col(acra, "STATUS")
        ssic = pick_col(acra, "PRIMARY", "SSIC", exclude=("CODE",))
        name = pick_col(acra, "ENTITY", "NAME")
        if pc:
            acra["_postal"] = acra[pc].map(
                lambda v: re.sub(r"\D", "", str(v)).zfill(6)
                if pd.notna(v) and re.sub(r"\D", "", str(v)) else None)
            acra["_live"] = (acra[status].astype(str).str.upper().str.contains("LIVE")
                             if status else False)
            acra["_name"] = acra[name] if name else ""
            acra["_ssic"] = acra[ssic] if ssic else ""
            acra = acra[acra["_postal"].notna()]

            by_pc = (acra.groupby("_postal", as_index=False)
                     .agg(acra_entities_at_postcode=("_postal", "size"),
                          acra_live_at_postcode=("_live", "sum"),
                          acra_top_activity=("_ssic", lambda v: v.dropna().mode().iloc[0]
                                             if len(v.dropna()) else None),
                          acra_sample_entities=("_name", lambda v: "; ".join(
                              v.dropna().astype(str).head(3)))))
            by_pc = by_pc.rename(columns={"_postal": "postal_code_key"})
            ctx["acra"] = acra
            ctx["acra_postcode"] = by_pc

            sec = acra.assign(postal_sector=acra["_postal"].str[:2])
            ctx["acra_sector"] = (sec.groupby("postal_sector", as_index=False)
                                  .agg(acra_entities=("postal_sector", "size"),
                                       acra_live_entities=("_live", "sum")))
            print(f"  ACRA: {len(acra):,} entities over "
                  f"{len(by_pc):,} postal codes")

    return ctx


# --------------------------------------------------------------------------
# attaching the context layers by address
# --------------------------------------------------------------------------


def attach_layer(sites, lat_col, lon_col, postal_col, addr_col,
                 layer, index, prefix, tiers):
    """Inner-join a context layer onto the sites, carrying EVERY field of the
    matched record across with a prefix.

    Postal code is an exact address match and wins outright. Failing that,
    the same rule the EV/carpark join uses: within the tier distance and with
    enough address-token agreement. Anything looser is a coincidence of
    proximity, not a match.
    """
    out = sites.reset_index(drop=True).copy()
    n = len(out)

    if layer is None or layer.empty:
        out[f"{prefix}_match_by"] = None
        out[f"{prefix}_match_distance_m"] = None
        out[f"{prefix}_address_similarity"] = None
        return out, pd.DataFrame()

    layer = layer.reset_index(drop=True)
    by_postal = {}
    if "_postal" in layer.columns:
        for i, pc in enumerate(layer["_postal"]):
            if isinstance(pc, str) and len(pc) == 6:
                by_postal.setdefault(pc, i)

    addr_series = layer["_addr"] if "_addr" in layer.columns else pd.Series([""] * len(layer))
    layer_tokens = [canon_address(a) for a in addr_series]

    if postal_col and postal_col in out.columns:
        site_pc = out[postal_col].map(
            lambda v: re.sub(r"\D", "", str(v)).zfill(6) if pd.notna(v) else None)
    else:
        site_pc = pd.Series([None] * n)
    site_tokens = ([canon_address(a) for a in out[addr_col]]
                   if addr_col in out.columns else [set()] * n)

    lats = out[lat_col].tolist() if lat_col in out.columns else [None] * n
    lons = out[lon_col].tolist() if lon_col in out.columns else [None] * n

    hits = [None] * n
    how_col, dist_col, sim_col = [None] * n, [None] * n, [None] * n

    for i in range(n):
        pc = site_pc.iloc[i]
        if pc and pc in by_postal:
            j = by_postal[pc]
            hits[i], how_col[i] = j, "postal code"
            sim_col[i] = round(jaccard(site_tokens[i], layer_tokens[j]), 3)
            try:
                dist_col[i] = round(haversine_m(lats[i], lons[i],
                                                layer["_lat"].iloc[j],
                                                layer["_lon"].iloc[j]), 1)
            except Exception:                                  # noqa: BLE001
                pass
        elif index is not None:
            for d, j in index.near(lats[i], lons[i], CANDIDATE_RADIUS_M):
                sc = jaccard(site_tokens[i], layer_tokens[j])
                if tier_for(d, sc, tiers):
                    hits[i], how_col[i] = j, "address + distance"
                    dist_col[i], sim_col[i] = round(d, 1), round(sc, 3)
                    break

    # every column of the layer, prefixed, blank where nothing matched
    right = layer.drop(columns=[c for c in ("_rings", "_key", "_geom_type",
                                            "Name", "_addr")
                                if c in layer.columns])
    right = right.rename(columns={
        "_lon": "longitude", "_lat": "latitude", "_postal": "postal_code",
        "_name": "name", "_stalls": "total_stalls", "_food_stalls": "food_stalls",
        "_market_stalls": "market_stalls", "_lots": "lots", "_pp_code": "pp_code",
    }).add_prefix(f"{prefix}_")
    attached = pd.DataFrame(index=range(n), columns=right.columns, dtype=object)
    matched_rows = [i for i, h in enumerate(hits) if h is not None]
    if matched_rows:
        attached.iloc[matched_rows] = right.iloc[
            [hits[i] for i in matched_rows]].to_numpy(dtype=object)

    out = pd.concat([out, attached], axis=1)
    out[f"{prefix}_match_by"] = how_col
    out[f"{prefix}_match_distance_m"] = dist_col
    out[f"{prefix}_address_similarity"] = sim_col

    inner = out.iloc[matched_rows].reset_index(drop=True) if matched_rows \
        else pd.DataFrame()
    return out, inner


def assign_areas(frame, lat_col, lon_col, ctx):
    """planning_area / code / region by point-in-polygon."""
    out = frame.copy()
    areas = ctx.get("areas")
    if areas is None:
        out["planning_area"] = None
        out["planning_area_code"] = None
        out["region"] = None
        return out

    nc, cc, rc = ctx["area_cols"]
    names, codes, regions = [], [], []
    for la, lo in zip(out[lat_col], out[lon_col]):
        row = areas.lookup(lo, la)
        names.append(row.get(nc) if row is not None else None)
        codes.append(row.get(cc) if row is not None and cc else None)
        regions.append(row.get(rc) if row is not None and rc else None)
    out["planning_area"] = names
    out["planning_area_code"] = codes
    out["region"] = regions
    return out


def enrich_radius(sites, lat_col, lon_col, postal_col, ctx):
    """Threshold-independent context: what sits within CONTEXT_RADIUS_M, and
    the ACRA postcode join. Computed once - it is the slow part."""
    sites = sites.reset_index(drop=True).copy()
    n = len(sites)

    if ctx.get("acra_postcode") is not None and postal_col in sites.columns:
        sites["postal_code_key"] = sites[postal_col].map(
            lambda v: re.sub(r"\D", "", str(v)).zfill(6) if pd.notna(v) else None)
        sites = sites.merge(ctx["acra_postcode"], on="postal_code_key", how="left")
        sites["acra_match_by"] = sites["acra_entities_at_postcode"].notna().map(
            {True: "postal code", False: None})
        sites["postal_sector"] = sites["postal_code_key"].str[:2]

    hawk_n, hawk_stalls = [0] * n, [0.0] * n
    ura_places_n, ura_lots_n = [0] * n, [0.0] * n
    hidx, hawker = ctx.get("hawker_index"), ctx.get("hawker")
    uidx, uplaces = ctx.get("ura_index"), ctx.get("ura_places")
    for i, (la, lo) in enumerate(zip(sites[lat_col], sites[lon_col])):
        if hidx is not None:
            hits = hidx.near(la, lo, CONTEXT_RADIUS_M)
            hawk_n[i] = len(hits)
            if hits:
                hawk_stalls[i] = float(hawker.iloc[[h[1] for h in hits]]["_stalls"]
                                       .fillna(0).sum())
        if uidx is not None:
            hits = uidx.near(la, lo, CONTEXT_RADIUS_M)
            ura_places_n[i] = len(hits)
            if hits:
                ura_lots_n[i] = float(uplaces.iloc[[h[1] for h in hits]]["_lots"]
                                      .fillna(0).sum())
    sites[f"hawker_centres_within_{CONTEXT_RADIUS_M}m"] = hawk_n
    sites[f"hawker_stalls_within_{CONTEXT_RADIUS_M}m"] = hawk_stalls
    sites[f"ura_parking_places_within_{CONTEXT_RADIUS_M}m"] = ura_places_n
    sites[f"ura_parking_lots_within_{CONTEXT_RADIUS_M}m"] = ura_lots_n
    return sites


def attach_context(sites, lat_col, lon_col, postal_col, addr_col, ctx, tiers):
    """Address-matched context layers, at this workbook's distance tolerance.
    Run per threshold so the 150m workbook is not carrying 300m matches."""
    inners = {}
    sites, inners["hawker"] = attach_layer(
        sites, lat_col, lon_col, postal_col, addr_col,
        ctx.get("hawker"), ctx.get("hawker_index"), "hawker", tiers)
    sites, inners["ura"] = attach_layer(
        sites, lat_col, lon_col, None, addr_col,
        ctx.get("ura_places"), ctx.get("ura_index"), "ura", tiers)

    if ctx.get("acra") is not None and "postal_code_key" in sites.columns:
        keys = set(sites.loc[sites.get("acra_entities_at_postcode").notna(),
                             "postal_code_key"].dropna())
        inners["acra"] = (ctx["acra"][ctx["acra"]["_postal"].isin(keys)].copy()
                          if keys else pd.DataFrame())
    return sites, inners


def town_metrics(ev_stations, cp_sites, matched, ctx, collected_at):
    """One row per planning area - the grain Step 1 of the proposal works in.

    Every quantity is counted once against the town its own coordinates fall
    in. Deriving these from the matched pairs instead would double-count a
    hawker centre near ten stations and would ignore every HDB carpark that
    has no charger, which is exactly the pool Step 3 is looking for.
    """
    if "planning_area" not in ev_stations.columns:
        return pd.DataFrame()

    ev = (ev_stations.groupby("planning_area", dropna=False)
          .agg(region=("region", "first"),
               ev_stations=("location_id", "nunique"),
               connectors=("connectors", "sum"),
               ac_connectors=("ac_connectors", "sum"),
               dc_connectors=("dc_connectors", "sum"),
               available_connectors=("available_now", "sum"))
          .reset_index())

    cp = (cp_sites.groupby("planning_area", dropna=False)
          .agg(carparks=("carpark_number", "nunique"),
               car_lots=("total_lots", "sum"),
               available_car_lots=("lots_available", "sum"))
          .reset_index())

    town = ev.merge(cp, on="planning_area", how="outer")

    # carparks that already have a charger, from the inner join
    if not matched.empty and "cp_planning_area" in matched.columns:
        cov = (matched.groupby("cp_planning_area", dropna=False)
               .agg(carparks_with_chargers=("cp_carpark_number", "nunique"))
               .reset_index().rename(columns={"cp_planning_area": "planning_area"}))
        town = town.merge(cov, on="planning_area", how="left")
    else:
        town["carparks_with_chargers"] = pd.NA

    # context layers, counted once each against their own planning area
    hawker = ctx.get("hawker")
    if hawker is not None and "planning_area" in hawker.columns:
        h = (hawker.groupby("planning_area", dropna=False)
             .agg(hawker_centres=("planning_area", "size"),
                  hawker_stalls=(ctx["hawker_stalls_col"], "sum"))
             .reset_index())
        town = town.merge(h, on="planning_area", how="left")

    ura = ctx.get("ura_places")
    if ura is not None and "planning_area" in ura.columns:
        u = (ura.groupby("planning_area", dropna=False)
             .agg(ura_parking_places=("planning_area", "size"),
                  ura_parking_lots=("_lots", "sum"))
             .reset_index())
        town = town.merge(u, on="planning_area", how="left")

    # ACRA: each postal sector counted once, against the town most of our
    # located sites in that sector fall in
    if ctx.get("acra_sector") is not None and "postal_sector" in ev_stations.columns:
        located = pd.concat([
            ev_stations[["postal_sector", "planning_area"]]
            if "postal_sector" in ev_stations.columns else pd.DataFrame(),
        ], ignore_index=True).dropna()
        if not located.empty:
            modal = (located.groupby(["postal_sector", "planning_area"])
                     .size().reset_index(name="n")
                     .sort_values("n", ascending=False)
                     .drop_duplicates("postal_sector")[["postal_sector",
                                                        "planning_area"]])
            a = (ctx["acra_sector"].merge(modal, on="postal_sector", how="inner")
                 .groupby("planning_area", as_index=False)
                 .agg(acra_entities=("acra_entities", "sum"),
                      acra_live_entities=("acra_live_entities", "sum")))
            town = town.merge(a, on="planning_area", how="left")

    num = town.select_dtypes(include="number").columns
    town[num] = town[num].fillna(0)

    tot_lots = town["car_lots"].sum()
    tot_avail = town["available_car_lots"].sum()
    tot_conn = town["connectors"].sum()

    # the proposal's metrics (ii) (iii) (v)
    town["share_of_sg_car_lots"] = (town["car_lots"] / tot_lots).round(5)
    town["share_of_sg_available_lots"] = (
        town["available_car_lots"] / tot_avail).round(5)
    town["avail_town_over_sg_total_lots"] = (
        town["available_car_lots"] / tot_lots).round(5)
    town["share_of_sg_connectors"] = (town["connectors"] / tot_conn).round(5)
    def ratio(num, den, scale=1.0):
        return (pd.to_numeric(town[num], errors="coerce")
                / pd.to_numeric(town[den], errors="coerce").replace(0, float("nan"))
                * scale).round(1)

    town["connectors_per_1000_car_lots"] = ratio("connectors", "car_lots", 1000)
    town["car_lots_per_connector"] = ratio("car_lots", "connectors")
    town["carpark_charger_coverage_pct"] = ratio(
        "carparks_with_chargers", "carparks", 100)

    town.insert(0, "collected_at", collected_at)
    return town.sort_values("connectors_per_1000_car_lots",
                            na_position="last").reset_index(drop=True)



# --------------------------------------------------------------------------
# the outer join: total parking supply against EV charging
# --------------------------------------------------------------------------
#
# The inner join above answers "what is at the chargers we have". This answers
# the opposite question: where is there parking and no charger. Merged into
# this script so the six context datasets are fetched once per run instead of
# twice, which is what was triggering the data.gov.sg rate limit.

def build_total_parking(payload, info):
    """Every carpark HDB lists, with live lots by type where reported.

    Starting from the availability feed would silently drop the ~240 carparks
    that do not report, and those are parking supply too.
    """
    live = flatten_carpark(payload)

    wide = (live.pivot_table(index="carpark_number", columns="lot_type_code",
                             values=["total_lots", "lots_available"],
                             aggfunc="sum")
            if not live.empty else pd.DataFrame())
    # every carpark either source knows about - 8 carparks report live lots
    # (1,604 car lots) without appearing in HDB Carpark Information, and
    # dropping them would understate total supply
    every = sorted(set(info["carpark_number"]) | set(live["carpark_number"]))
    flat = pd.DataFrame(index=pd.Index(every, name="carpark_number"))
    names = {"C": "car", "Y": "motorcycle", "H": "heavy_vehicle",
             "S": "motorcycle_sidecar"}
    for code, label in names.items():
        for src, suffix in (("total_lots", "lots"), ("lots_available", "lots_available")):
            col = (src, code)
            flat[f"{label}_{suffix}"] = (wide[col] if col in wide.columns
                                         else pd.NA)
    flat = flat.reset_index()

    out = info.merge(flat, on="carpark_number", how="outer")
    out["in_hdb_carpark_information"] = out["carpark_number"].isin(
        set(info["carpark_number"]))

    lot_cols = [c for c in out.columns if c.endswith("_lots")]
    avail_cols = [c for c in out.columns if c.endswith("_lots_available")]
    out["total_lots_all_types"] = out[lot_cols].sum(axis=1, min_count=1)
    out["total_available_all_types"] = out[avail_cols].sum(axis=1, min_count=1)
    out["reports_availability"] = out["car_lots"].notna()

    # car lots are the ICE/EV-relevant denominator; keep the familiar names
    out = out.rename(columns={"car_lots": "total_lots",
                              "car_lots_available": "lots_available"})
    known = out["total_lots"] > 0
    out["occupied_lots"] = (out["total_lots"] - out["lots_available"]).where(known)
    out["occupancy_pct"] = (out["occupied_lots"] / out["total_lots"] * 100) \
        .where(known).round(1)
    return out


def outer_join(ev_stations, carparks, tiers):
    """Matched pairs plus everything that matched nothing on either side."""
    matched = match_stations_to_carparks(ev_stations, carparks, tiers)

    if matched.empty:
        matched_ev, matched_cp = set(), set()
    else:
        matched_ev = set(matched["ev_location_id"].dropna())
        matched_cp = set(matched["cp_carpark_number"].dropna())

    cp_only = carparks[~carparks["carpark_number"].isin(matched_cp)] \
        .add_prefix("cp_").reset_index(drop=True)
    ev_only = ev_stations[~ev_stations["location_id"].isin(matched_ev)] \
        .add_prefix("ev_").reset_index(drop=True)

    matched = matched.assign(match_status="matched") if not matched.empty \
        else matched
    cp_only = cp_only.assign(match_status="carpark_only")
    ev_only = ev_only.assign(match_status="ev_only")

    frames = [f for f in (matched, cp_only, ev_only) if not f.empty]
    everything = pd.concat(frames, ignore_index=True, sort=False)

    # one town column whichever side the row came from
    for base in ("planning_area", "planning_area_code", "region"):
        cols = [c for c in (f"ev_{base}", f"cp_{base}") if c in everything.columns]
        if cols:
            merged = everything[cols[0]]
            for c in cols[1:]:
                merged = merged.where(merged.notna(), everything[c])
            everything[base] = merged

    everything["connectors"] = pd.to_numeric(
        everything.get("ev_connectors"), errors="coerce").fillna(0).astype(int)
    everything["car_lots"] = pd.to_numeric(
        everything.get("cp_total_lots"), errors="coerce")
    everything["available_car_lots"] = pd.to_numeric(
        everything.get("cp_lots_available"), errors="coerce")
    everything["has_charger"] = everything["connectors"] > 0
    everything["car_lots_per_connector"] = (
        everything["car_lots"]
        / everything["connectors"].replace(0, float("nan"))).round(1)

    order = ["match_status", "has_charger", "planning_area", "region",
             "connectors", "car_lots", "available_car_lots",
             "car_lots_per_connector"]
    rest = [c for c in everything.columns if c not in order]
    return everything[[c for c in order if c in everything.columns] + rest]


def coverage_by_town(everything, collected_at):
    """The Step 3 table: parking supply against charger provision, per town."""
    d = everything.copy()
    d["is_carpark"] = d["match_status"].isin(("matched", "carpark_only"))
    d["car_lots"] = d["car_lots"].fillna(0)

    g = (d[d["is_carpark"]].groupby("planning_area", dropna=False)
         .agg(carparks=("match_status", "size"),
              carparks_with_chargers=("has_charger", "sum"),
              car_lots=("car_lots", "sum"),
              available_car_lots=("available_car_lots", "sum"))
         .reset_index())

    ice = (d[d["is_carpark"] & ~d["has_charger"]]
           .groupby("planning_area", dropna=False)
           .agg(carparks_without_chargers=("match_status", "size"),
                car_lots_without_chargers=("car_lots", "sum"))
           .reset_index())
    g = g.merge(ice, on="planning_area", how="left")

    ev = (d.groupby("planning_area", dropna=False)
          .agg(ev_stations=("match_status",
                            lambda s: (s != "carpark_only").sum()),
               connectors=("connectors", "sum"))
          .reset_index())
    g = g.merge(ev, on="planning_area", how="left")

    offsite = (d[d["match_status"] == "ev_only"]
               .groupby("planning_area", dropna=False)
               .agg(ev_stations_off_hdb=("match_status", "size"),
                    connectors_off_hdb=("connectors", "sum"))
               .reset_index())
    g = g.merge(offsite, on="planning_area", how="left")

    num = g.select_dtypes(include="number").columns
    g[num] = g[num].fillna(0)

    def ratio(a, b, scale=1.0):
        return (pd.to_numeric(g[a], errors="coerce")
                / pd.to_numeric(g[b], errors="coerce").replace(0, float("nan"))
                * scale).round(1)

    g["carpark_coverage_pct"] = ratio("carparks_with_chargers", "carparks", 100)
    g["uncovered_lots_pct"] = ratio("car_lots_without_chargers", "car_lots", 100)
    g["connectors_per_1000_car_lots"] = ratio("connectors", "car_lots", 1000)
    g["share_of_sg_uncovered_lots"] = (
        g["car_lots_without_chargers"] / g["car_lots_without_chargers"].sum()
    ).round(5)

    g.insert(0, "collected_at", collected_at)
    return g.sort_values("car_lots_without_chargers", ascending=False) \
            .reset_index(drop=True)


# --------------------------------------------------------------------------
# integrity checks
# --------------------------------------------------------------------------

SG_BOUNDS = (1.15, 1.48, 103.6, 104.1)     # lat min, lat max, lon min, lon max


def run_checks(connectors, ev_stations, cp_sites, total_parking, sources,
               collected_at):
    """Checks that run before anything is written, recorded as a sheet rather
    than printed and lost. A warning does not stop the run: a flagged number
    you can see beats a clean number you cannot trust."""
    rows = []

    def rec(name, ok, detail):
        rows.append({"collected_at": collected_at, "check": name,
                     "outcome": "ok" if ok else "warn", "detail": detail})

    n_conn = len(connectors)
    rec("ev_feed_populated", n_conn > 0, f"{n_conn:,} connectors")
    rec("connector_count_plausible", 5000 <= n_conn <= 40000,
        f"{n_conn:,} connectors, expected roughly 8,000 to 20,000")

    ids = connectors.get("connector_id")
    if ids is not None:
        named = ids.dropna()
        dupes = len(named) - named.nunique()
        rec("connector_ids_unique", dupes == 0, f"{dupes} repeated ids")

    lo_lat, hi_lat, lo_lon, hi_lon = SG_BOUNDS
    for frame, label in ((ev_stations, "stations"), (cp_sites, "carparks")):
        if frame.empty or "latitude" not in frame.columns:
            continue
        good = frame[["latitude", "longitude"]].dropna()
        off = int((~good["latitude"].between(lo_lat, hi_lat)
                   | ~good["longitude"].between(lo_lon, hi_lon)).sum())
        rec(f"{label}_inside_singapore", off == 0,
            f"{off} of {len(good):,} outside the bounding box")
        missing = len(frame) - len(good)
        rec(f"{label}_have_coordinates", missing == 0,
            f"{missing} without coordinates")

    if "carpark_number" in cp_sites.columns:
        d = len(cp_sites) - cp_sites["carpark_number"].nunique()
        rec("carpark_rows_unique", d == 0, f"{d} duplicate carpark numbers")

    if "total_lots" in total_parking.columns:
        lots = pd.to_numeric(total_parking["total_lots"], errors="coerce").sum()
        rec("car_lot_total_plausible", 500_000 <= lots <= 900_000,
            f"{lots:,.0f} car lots, expected roughly 700,000")

    if "planning_area" in ev_stations.columns:
        placed = int(ev_stations["planning_area"].notna().sum())
        share = placed / max(len(ev_stations), 1)
        rec("stations_assigned_to_a_town", share >= 0.98,
            f"{placed:,} of {len(ev_stations):,} ({share:.1%})")

    if not sources.empty and "status" in sources.columns:
        loaded = int(sources["status"].astype(str).str.startswith(
            ("loaded", "cached")).sum())
        rec("context_datasets_available", loaded == len(sources),
            f"{loaded} of {len(sources)} loaded or served from cache")

    return pd.DataFrame(rows)


def check_join(matched, everything, ev_stations, total_parking, tiers, label,
               collected_at):
    """Arithmetic that has to hold after a join. If it does not, the join lost
    or duplicated rows and every number downstream is wrong."""
    rows = []

    def rec(name, ok, detail):
        rows.append({"collected_at": collected_at, "check": f"{label} {name}",
                     "outcome": "ok" if ok else "warn", "detail": detail})

    if matched is not None and not matched.empty \
            and "match_distance_m" in matched.columns:
        cap = max(d for d, _ in tiers)
        worst = matched["match_distance_m"].max()
        rec("matches_within_threshold", worst <= cap,
            f"furthest match {worst:.1f}m against a {cap}m cap")
        for side, col in (("station", "ev_location_id"),
                          ("carpark", "cp_carpark_number")):
            if col in matched.columns:
                d = len(matched) - matched[col].nunique()
                rec(f"one_{side}_per_pair", d == 0, f"{d} reused {side}s")

    if everything is not None and not everything.empty:
        counts = everything["match_status"].value_counts()
        m = int(counts.get("matched", 0))
        cp_only = int(counts.get("carpark_only", 0))
        ev_only = int(counts.get("ev_only", 0))
        rec("carparks_conserved", m + cp_only == len(total_parking),
            f"{m} + {cp_only} against {len(total_parking)} carparks")
        rec("stations_conserved", m + ev_only == len(ev_stations),
            f"{m} + {ev_only} against {len(ev_stations)} stations")
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# trimming
# --------------------------------------------------------------------------

# Two layers of cleanup, both switched off by --wide.
#
#   clean_sheet()  runs on EVERY sheet and only removes things that provably
#                  carry nothing: all-empty columns, exact duplicates of
#                  another column, and GIS bookkeeping.
#   slim()         runs on the two base tables only, and additionally drops
#                  the sparse context-layer records and the known constants.
#                  Nothing is lost: those fields survive in full on the
#                  per-layer inner-join and reference sheets.

# Suffixes that mark a shapefile export's internal bookkeeping. Numeric, so a
# correlation widget will happily rank them, and meaningless.
GIS_NOISE = ("OBJECTID", "OBJECTID_1", "INC_CRC", "FMEL_UPD_D",
             "SHAPE.AREA", "SHAPE.LEN", "SHAPE_1.AREA", "SHAPE_1.LEN",
             "LANDXADDRESSPOINT", "LANDYADDRESSPOINT")

# Never removed as a duplicate, whatever else matches them. These are the keys
# a BI model or a join relies on.
PROTECTED = {
    "collected_at", "planning_area", "planning_area_code", "region",
    "ev_location_id", "ev_postal_code", "ev_address", "ev_station_name",
    "cp_carpark_number", "cp_address", "postal_code_key", "postal_sector",
    "match_distance_m", "address_similarity", "match_tier",
}

DROP_EXACT = {
    # duplicates - byte-identical to a column that is kept. ev_location_id
    # survives on purpose: it is the key a BI model relates the station and
    # connector tables on.
    "ev_postal_code_key",
    "ev_planning_area", "ev_planning_area_code", "ev_region",
    "cp_planning_area", "cp_planning_area_code", "cp_region",
    "ev_hawker_NAME", "ev_hawker_ADDRESSPOSTALCODE",
    "ev_hawker_NUMBER_OF_COOKED_FOOD_STALLS", "ev_ura_PARKING_PL",
    # constant by construction
    "cp_lot_type_code", "cp_lot_type", "cp_feed_timestamp",
    "ev_acra_match_by", "ev_hawker_market_stalls",
    # near-empty free text and dates
    "ev_hawker_PHOTOURL", "ev_hawker_DESCRIPTION", "ev_hawker_ADDRESS_MYENV",
    "ev_hawker_ADDRESSBUILDINGNAME", "ev_hawker_INFO_ON_CO_LOCATORS",
    "ev_hawker_AWARDED_DATE", "ev_hawker_IMPLEMENTATION_DATE",
    "ev_hawker_HUP_COMPLETION_DATE", "ev_hawker_EST_ORIGINAL_COMPLETION_DATE",
    # superseded by the GEOJSON stall counts
    "ev_hawker_no_of_cooked_food_stalls", "ev_hawker_no_of_stalls",
    # redundant once planning_area is on the row
    "ev_hawker_planning_area_code", "ev_hawker_region",
    "ev_hawker_longitude", "ev_hawker_latitude",
}

# The whole URA record on the base tables. URA Parking Lot covers URA-managed
# and private parking, largely central; every matched row is an HDB carpark,
# so they barely overlap - 1.2% of rows at 150m, 3.6% at 300m. The useful form
# of that dataset is the radius count, which is kept.
DROP_PREFIX = ("ev_ura_",)
KEEP_ANYWAY = ("ev_ura_parking_places_within_", "ev_ura_parking_lots_within_")

BASE_TABLES = ("Matched stations", "Matched connectors")


# ACRA writes the literal string "na" for missing, so a column that is "na"
# on every row is empty, not constant.
BLANKS = ("", "NAN", "NONE", "NAT", "NA", "N/A", "NULL", "-")


def _blank_mask(series):
    return series.astype(str).str.strip().str.upper().isin(BLANKS)


def _fingerprint(series):
    """Value signature for duplicate detection, blind to dtype."""
    text = series.astype(str).str.strip().str.upper()
    return hash(tuple(text.where(~text.isin(BLANKS), "").tolist()))


def _dup_rank(name, position):
    """Which of two identical columns to keep: never an internal one, then
    the shortest name, then the one that is not SHOUTING, then first seen."""
    name = str(name)
    return (name.startswith("_"), len(name), not name.islower(), position)


def clean_sheet(frame, sheet=""):
    """Remove only what provably carries nothing. Safe on any sheet."""
    dropped = []
    keep = list(frame.columns)

    empty = [c for c in keep if frame[c].isna().all() or _blank_mask(frame[c]).all()]
    for c in empty:
        dropped.append((sheet, c, "no value on any row"))
    keep = [c for c in keep if c not in empty]

    noise = [c for c in keep
             if isinstance(c, str) and any(c == n or c.endswith("_" + n)
                                           or c.endswith(n) for n in GIS_NOISE)]
    noise = [c for c in noise if c not in PROTECTED]
    for c in noise:
        dropped.append((sheet, c, "GIS bookkeeping from the shapefile export"))
    keep = [c for c in keep if c not in noise]

    if len(frame):
        groups = {}
        for pos, c in enumerate(keep):
            groups.setdefault(_fingerprint(frame[c]), []).append((c, pos))
        for members in groups.values():
            if len(members) < 2:
                continue
            members.sort(key=lambda m: _dup_rank(*m))
            winner = members[0][0]
            for c, _ in members[1:]:
                if c in PROTECTED:
                    continue
                dropped.append((sheet, c, f"identical to {winner}"))
                keep.remove(c)

    return frame[keep], dropped


def slim(frame, sheet=""):
    """The extra cut for the two base tables."""
    drop = [c for c in frame.columns
            if (c in DROP_EXACT
                or (isinstance(c, str) and c.startswith(DROP_PREFIX)
                    and not c.startswith(KEEP_ANYWAY)))]
    reasons = []
    for c in drop:
        if isinstance(c, str) and c.startswith("ev_ura_"):
            why = "URA record, filled on under 4% of rows"
        elif c in ("cp_lot_type_code", "cp_lot_type", "cp_feed_timestamp",
                   "ev_acra_match_by", "ev_hawker_market_stalls"):
            why = "one value on every row"
        elif c.startswith("ev_hawker_"):
            why = "hawker record, sparse or duplicated"
        else:
            why = "duplicate of a kept column"
        reasons.append((sheet, c, why))
    return frame.drop(columns=drop), reasons


def clean_all(sheets, wide=False):
    """Apply both layers across every sheet and return the audit trail."""
    if wide:
        return sheets, pd.DataFrame(columns=["sheet", "column", "reason"])
    out, log = {}, []
    for name, frame in sheets.items():
        if name in ("Notes", "Sources", "Summary"):
            out[name] = frame
            continue
        if name in BASE_TABLES:
            frame, why = slim(frame, name)
            log += why
        frame, why = clean_sheet(frame, name)
        log += why
        out[name] = frame
    return out, pd.DataFrame(log, columns=["sheet", "column", "reason"])


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

# plumbing columns that should never reach a sheet
INTERNAL = ("_rings", "_key", "_geom_type", "_addr", "_live", "_ssic",
            "_txt", "_pc", "_tokens")
RENAME_INTERNAL = {"_lon": "longitude", "_lat": "latitude",
                   "_postal": "postal_code", "_name": "name",
                   "_lots": "lots", "_pp_code": "pp_code",
                   "_stalls": "total_stalls", "_food_stalls": "food_stalls",
                   "_market_stalls": "market_stalls"}


def tidy(frame, collected_at):
    out = frame.copy()
    out = out.drop(columns=[c for c in INTERNAL if c in out.columns])
    out = out.rename(columns={k: v for k, v in RENAME_INTERNAL.items()
                              if k in out.columns
                              and v not in out.columns})
    out = out.drop(columns=[c for c in out.columns
                            if isinstance(c, str) and c.startswith("_")])
    for col in out.columns:
        if out[col].map(lambda v: isinstance(v, (list, dict, tuple))).any():
            out[col] = out[col].astype(str)
    if "collected_at" not in out.columns:
        out.insert(0, "collected_at", collected_at)
    return out


def locked_by_excel(path):
    """Excel keeps the file open and drops a ~$name lock file beside it.
    Writing over that raises PermissionError, so check before doing the work."""
    path = Path(path)
    if (path.parent / f"~${path.name}").exists():
        return True
    if not path.exists():
        return False
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def write_workbook(path, sheets, collected_at):
    """Written to a temp file first, then moved into place, so an interrupted
    run cannot leave a half-written workbook. If the target is open in Excel
    the run does not die - it writes a stamped copy alongside and says so."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.writing{path.suffix}")
    try:
        _write_sheets(tmp, sheets, collected_at)
    except Exception:
        tmp.unlink(missing_ok=True)      # never leave a half-written file behind
        raise

    try:
        os.replace(tmp, path)
        print(f"  wrote {path.relative_to(HERE)}")
        return path
    except PermissionError:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        alt = path.with_name(f"{path.stem}_{stamp}{path.suffix}")
        os.replace(tmp, alt)
        print(f"  !! {path.name} is open in Excel - wrote {alt.name} instead.")
        print(f"     Close it and rerun to update {path.name} in place.")
        return alt


def _write_sheets(path, sheets, collected_at):
    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        for name, frame in sheets.items():
            frame = tidy(frame, collected_at)
            sheet = name[:31]
            frame.to_excel(xl, sheet_name=sheet, index=False)
            ws = xl.sheets[sheet]
            ws.freeze_panes = "A2"
            for i, col in enumerate(frame.columns, start=1):
                longest = pd.to_numeric(frame[col].astype(str).str.len().max(),
                                        errors="coerce")
                width = max(len(str(col)), int(longest) if pd.notna(longest) else 0)
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = \
                    min(max(width + 2, 10), 45)


def read_sheet(path, sheet):
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_excel(path, sheet_name=sheet)
    except Exception:                                          # noqa: BLE001
        return pd.DataFrame()


def update_combined(path, sheets, history_rows, town_rows, collected_at,
                    archive=True):
    """Same filename every run. Current-state sheets are replaced; Run history
    and Town history are appended to, so repeated runs build a time series."""
    previous_runs = read_sheet(path, "Run history")
    previous_towns = read_sheet(path, "Town history")

    if path.exists() and archive:
        DIR_ARCH.mkdir(parents=True, exist_ok=True)
        was = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, DIR_ARCH / f"{path.stem}_{was}{path.suffix}")

    runs = pd.concat([previous_runs, history_rows], ignore_index=True)
    if "collected_at" in runs.columns:
        runs = runs.drop_duplicates(subset=["collected_at"], keep="last")
        runs = runs.sort_values("collected_at")

    towns = pd.concat([previous_towns, town_rows], ignore_index=True)
    if {"collected_at", "planning_area"} <= set(towns.columns):
        towns = towns.drop_duplicates(subset=["collected_at", "planning_area"],
                                      keep="last")
        towns = towns.sort_values(["collected_at", "planning_area"])

    sheets = dict(sheets)
    sheets["Run history"] = runs
    sheets["Town history"] = towns
    write_workbook(path, sheets, collected_at)
    return len(runs)


# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="re-parse the newest saved JSON instead of calling the APIs")
    ap.add_argument("--keep-temp", action="store_true",
                    help="keep the data.gov.sg downloads in data/temp")
    ap.add_argument("--no-archive", action="store_true",
                    help="overwrite the combined workbooks without archiving")
    ap.add_argument("--refresh-context", action="store_true",
                    help="ignore the context cache and force a live fetch")
    ap.add_argument("--no-cache", action="store_true",
                    help="do not read or write the context cache")
    ap.add_argument("--wide", action="store_true",
                    help="keep every column on every sheet, including empty, "
                         "duplicate and bookkeeping ones")
    ap.add_argument("--no-outer", action="store_true",
                    help="skip the outer-join workbooks (inner join only)")
    ap.add_argument("--light", action="store_true",
                    help="skip the ACRA download, the slowest of the six")
    ap.add_argument("--no-install", action="store_true",
                    help="do not pip install missing packages, just fail")
    args = ap.parse_args()

    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)

    collected_at = datetime.now().isoformat(timespec="seconds")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    locked = [DIR_COMB / f"ev_carpark_combined_{label}.xlsx"
              for label in THRESHOLDS
              if locked_by_excel(DIR_COMB / f"ev_carpark_combined_{label}.xlsx")]
    if locked:
        print("Excel has these open, so they cannot be updated in place:")
        for f in locked:
            print(f"  {f.relative_to(HERE)}")
        print("Close them now to update in place. Carrying on either way - "
              "a stamped copy is written for anything still locked at the end.\n")

    # ---- 1. the two live feeds ----------------------------------------
    if args.offline:
        ev_raw = newest_raw(DIR_RAW_EV, "ev_charging_raw_*")
        cp_raw = newest_raw(DIR_RAW_CP, "carpark_availability_*")
        if not ev_raw or not cp_raw:
            sys.exit("--offline needs both raw JSON files in data/raw "
                     "from an earlier run.")
        print(f"offline: reading {ev_raw[-1].name} and {cp_raw[-1].name}")
        snapshot = read_json(ev_raw[-1])
        payload = read_json(cp_raw[-1])
        got = re.search(r"_(\d{8})_(\d{6})", ev_raw[-1].name)
        if got:
            collected_at = datetime.strptime(got.group(1) + got.group(2),
                                             "%Y%m%d%H%M%S").isoformat(timespec="seconds")
            stamp = f"{got.group(1)}_{got.group(2)}"
    else:
        print("fetching EV charging points from LTA DataMall ...")
        snapshot = fetch_ev_snapshot()
        write_json_gz(DIR_RAW_EV / f"ev_charging_raw_{stamp}.json.gz", snapshot)
        print(f"  saved data/raw/ev/ev_charging_raw_{stamp}.json.gz")

        print("fetching carpark availability from data.gov.sg ...")
        payload = fetch_carpark_payload()
        write_json_gz(DIR_RAW_CP / f"carpark_availability_{stamp}.json.gz", payload)
        print(f"  saved data/raw/carpark/carpark_availability_{stamp}.json.gz")

    connectors, ev_stations = flatten_ev(snapshot)
    if connectors.empty:
        sys.exit("EV snapshot parsed but empty - check the raw JSON.")
    carparks = flatten_carpark(payload)
    if carparks.empty:
        sys.exit("Carpark feed returned no records.")

    # ---- 2. the six data.gov.sg datasets (temp) ------------------------
    print("fetching data.gov.sg context datasets ...")
    raw_ctx, sources = {}, []
    for spec in CONTEXT_DATASETS:
        # --offline only replays the two live feeds; the context datasets are
        # never saved as files, so they are fetched every run and fail soft.
        frame, status = fetch_context(spec, light=args.light,
                                      use_cache=not args.no_cache,
                                      refresh=args.refresh_context)
        raw_ctx[spec["key"]] = frame
        sources.append({
            "dataset_id": spec["id"], "title": spec["title"],
            "agency": spec["agency"], "format": spec["kind"],
            "rows": 0 if frame is None else len(frame),
            "fields": 0 if frame is None else len(frame.columns),
            "status": status,
            "used_for": spec["use"],
        })
        time.sleep(2)      # data.gov.sg throttles bursts
    sources = pd.DataFrame(sources)
    ok = int((~sources["status"].str.startswith(("unavailable", "skipped",
                                                 "poll-download",
                                                 "datastore"))).sum())
    print(f"  {ok} of {len(sources)} context datasets available")

    info = prepare_carpark_info(raw_ctx.get("hdb_carparks"))
    if info is not None:
        carparks = carparks.merge(info, on="carpark_number", how="left")
        print(f"  joined addresses onto {carparks['address'].notna().sum():,} "
              "carpark rows")
    carparks = carparks.sort_values(["carpark_number", "lot_type_code"])

    car_lots = carparks[carparks["lot_type_code"] == "C"].copy()
    cp_sites = (car_lots.groupby("carpark_number", as_index=False)
                .agg({c: "first" for c in car_lots.columns
                      if c != "carpark_number"}))

    # the outer join needs a different carpark table: every carpark either
    # source knows about, every lot type, whether or not it reports live
    total_parking = build_total_parking(payload, info)
    listed = int(total_parking["in_hdb_carpark_information"].sum())
    print(f"  total parking: {len(total_parking):,} carparks "
          f"({listed:,} in the HDB register, {len(total_parking) - listed} "
          f"live-feed only), {total_parking['total_lots'].sum():,.0f} car lots, "
          f"{total_parking['total_lots_all_types'].sum():,.0f} of all types")

    ctx = build_context(raw_ctx)

    # assign planning areas once, to every table that has coordinates
    if ctx.get("areas") is not None:
        for key in ("hawker", "ura_places"):
            frame = ctx.get(key)
            if frame is not None and not frame.empty:
                ctx[key] = assign_areas(frame, "_lat", "_lon", ctx)
        ev_stations = assign_areas(ev_stations, "latitude", "longitude", ctx)
        cp_sites = assign_areas(cp_sites, "latitude", "longitude", ctx)
        total_parking = assign_areas(total_parking, "latitude", "longitude", ctx)
        print(f"  planning area assigned to "
              f"{ev_stations['planning_area'].notna().sum():,} of "
              f"{len(ev_stations):,} EV stations and "
              f"{cp_sites['planning_area'].notna().sum():,} of "
              f"{len(cp_sites):,} carparks")
    else:
        ev_stations["planning_area"] = None
        ev_stations["planning_area_code"] = None
        ev_stations["region"] = None
        cp_sites["planning_area"] = None
        cp_sites["planning_area_code"] = None
        cp_sites["region"] = None
        total_parking["planning_area"] = None
        total_parking["planning_area_code"] = None
        total_parking["region"] = None

    # threshold-independent context (radius counts + the ACRA postcode join)
    ev_stations = enrich_radius(ev_stations, "latitude", "longitude",
                                "postal_code", ctx)

    # ---- 3. per-source workbooks ---------------------------------------
    by_operator = (connectors.assign(_dc=connectors["current"].eq("DC"))
                   .groupby("operator", dropna=False)
                   .agg(sites=("location_id", "nunique"),
                        chargers=("charger_index", "count"),
                        connectors=("connector_id", "count"),
                        dc_connectors=("_dc", "sum"),
                        max_power_kw=("power_rating_kw", "max"),
                        median_power_kw=("power_rating_kw", "median"),
                        median_price=("price", "median"))
                   .reset_index().sort_values("connectors", ascending=False))

    ev_sheets, _ = clean_all({"Connectors": connectors, "Stations": ev_stations,
                              "By operator": by_operator}, wide=args.wide)
    write_workbook(DIR_EV / "ev_charging_points.xlsx", ev_sheets, collected_at)

    cp_sheets, _ = clean_all({"Availability": carparks,
                              "Car lots by carpark": cp_sites}, wide=args.wide)
    write_workbook(DIR_CP / "carpark_availability.xlsx", cp_sheets, collected_at)

    ctx_sheets = {"Sources": sources}
    for key, label in (("planning_areas", "Planning areas"),
                       ("hawker_geo", "Hawker centres"),
                       ("hawker_list", "Hawker stall counts"),
                       ("ura_lots", "URA parking lots")):
        frame = raw_ctx.get(key)
        if frame is not None and not frame.empty:
            ctx_sheets[label] = frame
    if ctx.get("acra_sector") is not None:
        ctx_sheets["ACRA by postal sector"] = ctx["acra_sector"]
    if len(ctx_sheets) > 1:
        ctx_sheets, _ = clean_all(ctx_sheets, wide=args.wide)
        write_workbook(DIR_CTX / "datagov_context.xlsx", ctx_sheets, collected_at)

    # ---- 4. combined workbooks, updated in place -----------------------
    if info is None or cp_sites["latitude"].notna().sum() == 0:
        print("no carpark coordinates - skipping the combined workbooks")
        return

    quality = run_checks(connectors, ev_stations, cp_sites, total_parking,
                         sources, collected_at)
    for w in quality[quality["outcome"] != "ok"].itertuples():
        print(f"  warn  {w.check}: {w.detail}")

    for label, tiers in THRESHOLDS.items():
        ev_t, inner_joins = attach_context(ev_stations, "latitude", "longitude",
                                           "postal_code", "address", ctx, tiers)
        for key, frame in inner_joins.items():
            if frame is not None and not frame.empty:
                unit = "ACRA entities" if key == "acra" else "stations"
                print(f"  {label} {key}: {len(frame):,} {unit} joined by address")

        matched = match_stations_to_carparks(ev_t, cp_sites, tiers)
        if matched.empty:
            print(f"  {label}: no matches")
            continue

        # the enrichment columns arrive with the ev_ prefix; surface the
        # town on the matched row itself, it is what everything groups by
        for src, dest in (("ev_planning_area", "planning_area"),
                          ("ev_planning_area_code", "planning_area_code"),
                          ("ev_region", "region")):
            if src in matched.columns:
                matched[dest] = matched[src].where(
                    matched[src].notna(),
                    matched.get(src.replace("ev_", "cp_")))

        carry = (["match_distance_m", "address_similarity", "match_tier",
                  "planning_area", "planning_area_code", "region"]
                 + [c for c in matched.columns
                    if c.startswith(("cp_", "ev_hawker", "ev_ura", "ev_acra",
                                     "ev_postal_sector"))])
        carry = [c for c in dict.fromkeys(carry) if c in matched.columns]
        matched_connectors = connectors.add_prefix("ev_").merge(
            matched[["ev_location_id"] + carry], on="ev_location_id", how="inner")

        towns = town_metrics(ev_t, cp_sites, matched, ctx, collected_at)

        run_row = pd.DataFrame([{
            "collected_at": collected_at,
            "threshold": label,
            "ev_stations": len(ev_stations),
            "ev_connectors": len(connectors),
            "hdb_carparks_with_car_lots": len(cp_sites),
            "matched_pairs": len(matched),
            "matched_connectors": len(matched_connectors),
            "match_rate_stations": round(len(matched) / len(ev_stations), 4),
            "match_rate_carparks": round(len(matched) / len(cp_sites), 4),
            "median_match_distance_m": round(matched["match_distance_m"].median(), 1),
            "matched_car_lots": matched["cp_total_lots"].sum(),
            "matched_available_lots": matched["cp_lots_available"].sum(),
            "planning_areas_covered": int(towns["planning_area"].nunique())
            if not towns.empty else 0,
            "context_datasets_loaded": ok,
        }])

        summary = run_row.T.reset_index()
        summary.columns = ["metric", "value"]

        notes = pd.DataFrame({"note": [
            f"Inner join of LTA EV charging stations and HDB carpark availability, "
            f"{label} threshold.",
            f"Collected {collected_at}. Both live feeds pulled in the same run.",
            "Pairs kept only where: <=50m, or <=150m with address similarity >=0.35"
            + (", or <=300m with similarity >=0.25." if label == "300m" else "."),
            "Address similarity is token-level Jaccard after canonicalising "
            "abbreviations (ST->STREET, AVE->AVENUE, BLOCK->BLK) and dropping "
            "MSCP / CAR PARK noise.",
            "One-to-one: each EV station matches at most one carpark and vice versa.",
            "Carpark side is deduplicated to one row per carpark, car lots "
            "(lot_type_code C) only.",
            "planning_area comes from point-in-polygon against URA Master Plan 2019 "
            "boundaries, not from parsing the street name.",
            "ev_hawker_* and ev_ura_* carry every field of the ONE matched record, "
            "joined the same way as the carpark: exact postal code where both sides "
            "have one, otherwise inside the distance tier with enough address-token "
            "agreement. ev_*_match_by says which rule fired. Blank means no match at "
            "this threshold, not missing data.",
            f"ev_hawker_centres_within_{CONTEXT_RADIUS_M}m and the other *_within_* "
            f"columns are plain radius counts, kept as a fallback signal for stations "
            f"that match no single record.",
            "ev_acra_* joins on the full 6-digit postal code - ACRA has no "
            "coordinates, so postal code is the only honest key. That file covers "
            "company names starting with 'A' only, so read it as a relative density "
            "proxy, never as an entity count.",
            "The EV x hawker centres / EV x URA parking / ACRA at charger postcodes "
            "sheets are the inner joins on their own: matched rows only, all fields.",
            "Availability is a point-in-time reading. Run history and Town history "
            "accumulate across runs - use them for time-of-day analysis.",
            "ev_ / cp_ prefixes avoid the address/latitude/longitude collision "
            "between the two sources.",
        ]})

        checks = pd.concat(
            [quality, check_join(matched, None, ev_t, total_parking, tiers,
                                 label, collected_at)], ignore_index=True)

        sheets = {
            "Matched stations": matched,
            "Matched connectors": matched_connectors,
            "By planning area": towns,
            "Summary": summary,
            "Sources": sources,
            "Data quality": checks,
            "Notes": notes,
        }
        for key, lbl in (("hawker", "EV x hawker centres"),
                         ("ura", "EV x URA parking"),
                         ("acra", "ACRA at charger postcodes")):
            frame = inner_joins.get(key)
            if frame is not None and not frame.empty:
                sheets[lbl] = frame
        for key, lbl in (("hawker_geo", "Hawker centres"),
                         ("hawker_list", "Hawker stall counts"),
                         ("ura_lots", "URA parking lots"),
                         ("planning_areas", "Planning areas")):
            frame = raw_ctx.get(key)
            if frame is not None and not frame.empty:
                sheets[lbl] = frame
        if ctx.get("ura_places") is not None:
            sheets["URA parking places"] = ctx["ura_places"]
        if ctx.get("acra_postcode") is not None:
            sheets["ACRA by postcode"] = ctx["acra_postcode"]
        if ctx.get("acra_sector") is not None:
            sheets["ACRA by postal sector"] = ctx["acra_sector"]

        sheets, cleanup = clean_all(sheets, wide=args.wide)
        if not cleanup.empty:
            sheets["Cleanup log"] = cleanup
            print(f"  {label}: removed {len(cleanup)} empty, duplicate or "
                  f"bookkeeping columns across "
                  f"{cleanup['sheet'].nunique()} sheets (--wide keeps them)")

        path = DIR_COMB / f"ev_carpark_combined_{label}.xlsx"
        runs = update_combined(path, sheets, run_row, towns, collected_at,
                               archive=not args.no_archive)

        # ---- the outer join, same threshold, same context ---------------
        if not args.no_outer:
            everything = outer_join(ev_t, total_parking, tiers)
            cover = coverage_by_town(everything, collected_at)
            counts = everything["match_status"].value_counts()
            candidates = (everything[everything["match_status"] == "carpark_only"]
                          .sort_values("car_lots", ascending=False)
                          .reset_index(drop=True))

            uncovered = cover["car_lots_without_chargers"].sum()
            all_lots = cover["car_lots"].sum()
            outer_summary = pd.DataFrame({
                "metric": [
                    "Threshold", "Rows in the outer join",
                    "matched (carpark with a charger)",
                    "carpark_only (no charger - candidate pool)",
                    "ev_only (charger off the HDB estate)",
                    "HDB carparks total", "HDB car lots total",
                    "HDB lots of all types",
                    "Car lots at carparks with a charger",
                    "Car lots at carparks without one",
                    "Share of car lots with no charger",
                    "EV stations total", "Connectors total",
                    "Connectors on HDB carparks", "Planning areas covered"],
                "value": [
                    label, len(everything),
                    int(counts.get("matched", 0)),
                    int(counts.get("carpark_only", 0)),
                    int(counts.get("ev_only", 0)),
                    int(cover["carparks"].sum()), int(all_lots),
                    int(total_parking["total_lots_all_types"].sum()),
                    int(all_lots - uncovered), int(uncovered),
                    f"{uncovered / max(all_lots, 1):.1%}",
                    len(ev_stations), len(connectors),
                    int(cover["connectors"].sum() - cover["connectors_off_hdb"].sum()),
                    int(cover["planning_area"].notna().sum())],
            })

            outer_notes = pd.DataFrame({"note": [
                f"Outer join of total HDB parking supply and EV charging, {label}.",
                f"Collected {collected_at}.",
                "match_status: matched = carpark with a charger at its address; "
                "carpark_only = parking with no charger, so petrol and diesel "
                "today and the candidate pool for new ones; ev_only = a charger "
                "with no HDB carpark, typically a mall, office or private site.",
                "Total parking means every carpark either source lists, including "
                "the ones that never report live availability "
                "(reports_availability = FALSE) and the eight that appear only in "
                "the live feed, and every lot type. car_lots is car-only; "
                "total_lots_all_types adds motorcycle and heavy vehicle.",
                "Same matching rule as the inner join: 50 m unconditional, 150 m "
                "with address similarity 0.35 or better"
                + (", 300 m with 0.25 or better." if label == "300m" else "."),
                "One-to-one: a carpark takes at most one station and vice versa.",
                "ev_only rows are not unmet demand. They are sites the HDB feed "
                "does not list. Do not read them as a shortfall.",
                "Candidate sites is carpark_only sorted by car lots. That is a "
                "supply ranking, not a recommendation. Weight it with the EVPI "
                "before drawing conclusions.",
            ]})

            outer_checks = pd.concat(
                [quality, check_join(matched, everything, ev_t, total_parking,
                                     tiers, label, collected_at)],
                ignore_index=True)
            for w in outer_checks[outer_checks["outcome"] != "ok"].itertuples():
                if str(w.check).startswith(label):
                    print(f"    warn  {w.check}: {w.detail}")

            outer_sheets = {
                "All parking sites": everything,
                "Candidate sites": candidates,
                "By planning area": cover,
                "Coverage summary": outer_summary,
                "Sources": sources,
                "Data quality": outer_checks,
                "Notes": outer_notes,
            }
            outer_sheets, outer_cleanup = clean_all(outer_sheets, wide=args.wide)
            if not outer_cleanup.empty:
                outer_sheets["Cleanup log"] = outer_cleanup
            write_workbook(DIR_OUTER / f"carpark_ev_outer_{label}.xlsx",
                           outer_sheets, collected_at)
            print(f"    outer {label}: {int(counts.get('matched', 0)):,} matched, "
                  f"{int(counts.get('carpark_only', 0)):,} carpark only, "
                  f"{int(counts.get('ev_only', 0)):,} EV only | "
                  f"{uncovered:,.0f} car lots with no charger")
        print(f"  {label}: {len(matched):,} matched pairs, "
              f"{len(matched) / len(ev_stations):.1%} of EV stations, "
              f"median {matched['match_distance_m'].median():.1f}m, "
              f"{towns['planning_area'].nunique() if not towns.empty else 0} "
              f"planning areas "
              f"({runs} run{'s' if runs != 1 else ''} in history)")

    # ---- 5. clean up ----------------------------------------------------
    if args.keep_temp:
        print(f"  kept the downloads in {DIR_TMP.relative_to(HERE)}")
    else:
        for f in DIR_TMP.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass

    print(f"\n{len(connectors):,} connectors across {len(ev_stations):,} stations; "
          f"{len(cp_sites):,} HDB carparks with car lots")
    print(f"collected_at = {collected_at}")


if __name__ == "__main__":
    main()