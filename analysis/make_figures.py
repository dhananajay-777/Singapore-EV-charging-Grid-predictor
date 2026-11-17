"""
Builds the README figures from the collected data in data/utilisation.

    python analysis/make_figures.py

Writes three PNGs to docs/figures/. Needs pandas, numpy and matplotlib.
"""

import glob
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.path import Path as Polygon

ROOT = Path(__file__).resolve().parent.parent
UTIL = ROOT / "data" / "utilisation"
OUT = ROOT / "docs" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
EV = "#2a78d6"      # categorical slot 1
CAR = "#eb6834"     # categorical slot 2

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK_2,
    "xtick.color": INK_2, "ytick.color": INK_2, "text.color": INK,
    "font.size": 11, "axes.titlesize": 14, "axes.titleweight": "bold",
    "axes.titlelocation": "left", "axes.spines.top": False,
    "axes.spines.right": False, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.8, "axes.axisbelow": True,
})


def network():
    n = pd.read_csv(UTIL / "network.csv", parse_dates=["sampled_at"])
    return n.drop_duplicates("sampled_at").sort_values("sampled_at")


def fig_daily(n):
    d = (n.set_index("sampled_at")[["utilisation_pct", "car_occupancy_pct"]]
           .resample("D").mean().dropna())
    fig, ax = plt.subplots(figsize=(10, 4.2), dpi=150)
    ax.plot(d.index, d.utilisation_pct, color=EV, lw=2, label="EV connector utilisation")
    ax.plot(d.index, d.car_occupancy_pct, color=CAR, lw=2, label="HDB car-lot occupancy")
    last = d.index[-1]
    ax.annotate("EV utilisation", (last, d.utilisation_pct.iloc[-1]), xytext=(6, 0),
                textcoords="offset points", va="center", color=INK_2)
    ax.annotate("Car occupancy", (last, d.car_occupancy_pct.iloc[-1]), xytext=(6, 0),
                textcoords="offset points", va="center", color=INK_2)
    ax.set_ylim(0, 60)
    ax.set_ylabel("% (daily mean of samples)")
    ax.set_title("Network-wide demand, 11 Sep – 14 Oct 2026", pad=26)
    ax.text(0, 1.04, f"{len(n)} samples · 11,400+ EV connectors · 2,000+ HDB carparks",
            transform=ax.transAxes, color=INK_2, fontsize=10)
    ax.legend(loc="lower left", frameon=False, ncol=2)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(OUT / "daily_demand.png")
    plt.close(fig)


def fig_hourly(n):
    h = (n.assign(hour=n.sampled_at.dt.hour)
           .groupby("hour")[["utilisation_pct", "car_occupancy_pct"]].mean()
           .reindex(range(24)))
    fig, ax = plt.subplots(figsize=(10, 4.2), dpi=150)
    ax.plot(h.index, h.utilisation_pct, color=EV, lw=2, marker="o", ms=4,
            label="EV connector utilisation")
    ax.plot(h.index, h.car_occupancy_pct, color=CAR, lw=2, marker="o", ms=4,
            label="HDB car-lot occupancy")
    ax.set_xticks(range(0, 24, 2))
    ax.set_xlabel("Hour of day (SGT)")
    ax.set_ylabel("% (mean across samples)")
    ax.set_ylim(0, 60)
    ax.set_title("Demand by hour of day", pad=26)
    ax.text(0, 1.04, "Gaps are hours the scheduler never sampled",
            transform=ax.transAxes, color=INK_2, fontsize=10)
    ax.legend(loc="lower left", frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(OUT / "hourly_profile.png")
    plt.close(fig)


def _rings(node):
    if isinstance(node[0][0], (int, float)):
        return [node]
    out = []
    for child in node:
        out.extend(_rings(child))
    return out


def fig_areas(min_stations=5, top=10):
    st = pd.concat(pd.read_csv(f, parse_dates=["sampled_at"])
                   for f in glob.glob(str(UTIL / "fact" / "stations_*.csv.gz")))
    dim = (pd.concat(pd.read_csv(f) for f in glob.glob(str(UTIL / "dim" / "stations_*.csv.gz")))
             .drop_duplicates("location_id", keep="last"))
    pa = pd.read_csv(ROOT / "data" / "reference" / "context" / "planning_areas.csv.gz")
    polys = [(r.PLN_AREA_N, Polygon(np.array(ring)))
             for _, r in pa.iterrows() for ring in _rings(json.loads(r["_rings"]))]
    dim["area"] = [next((name for name, p in polys if p.contains_point((lon, lat))), None)
                   for lon, lat in zip(dim.longitude, dim.latitude)]

    st = st[st.connectors > 0].assign(day=st.sampled_at.dt.date)
    # charging urgency at station level: the mean of each day's peak utilisation
    urg = (st.groupby(["location_id", "day"]).utilisation_pct.max()
             .groupby("location_id").mean().rename("urgency").reset_index()
             .merge(dim[["location_id", "area"]], on="location_id"))
    g = (urg.groupby("area").agg(urgency=("urgency", "mean"), stations=("urgency", "size"))
            .query("stations >= @min_stations")
            .sort_values("urgency", ascending=False).head(top).iloc[::-1])

    fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
    ax.barh(g.index.str.title(), g.urgency, color=EV, height=0.62)
    for y, (val, k) in enumerate(zip(g.urgency, g.stations)):
        ax.text(val + 1, y, f"{val:.1f}  ({k} stations)", va="center", color=INK_2, fontsize=10)
    ax.set_xlim(0, 100)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("Charging urgency: mean of daily peak utilisation (%)")
    ax.set_title("Where public chargers run hottest: top 10 planning areas", pad=26)
    ax.text(0, 1.03, f"Station level, all 34 days · areas with {min_stations}+ stations",
            transform=ax.transAxes, color=INK_2, fontsize=10)
    fig.tight_layout()
    fig.savefig(OUT / "urgency_by_area.png")
    plt.close(fig)
    return g.iloc[::-1]


if __name__ == "__main__":
    n = network()
    fig_daily(n)
    fig_hourly(n)
    print(fig_areas().round(1))
    print("written to", OUT)
