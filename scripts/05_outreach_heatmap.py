"""
05_outreach_heatmap.py

Turns the outreach program member list into a privacy-safe heat map layer.

The member file holds people's home addresses, so it NEVER goes into the repo.
This script:
    1. reads data/raw/outreach/gateway_clients.csv  (ID, Status, Address)
    2. geocodes addresses with the free U.S. Census batch geocoder
       (only the street address is sent - no names or IDs beyond a row number)
    3. snaps every point to a ~250 m grid and drops grid cells with fewer than
       MIN_MEMBERS_PER_CELL people
    4. writes docs/data/outreach_heat.csv  (grid cell center + counts by status)
       and docs/data/outreach_by_zip.csv   (member counts per ZIP code)

Only the aggregated files are published. The geocoded point file stays in
data/processed/outreach/ (git-ignored).

Run from the repo root:
    python scripts/05_outreach_heatmap.py
"""

import io
import re

import numpy as np
import pandas as pd
import requests

from common import OUTREACH_CSV, OUTREACH_DIR, WEB_DATA

GEOCODER = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
GRID_DEG = 0.0025            # ~250 m cells
MIN_MEMBERS_PER_CELL = 3     # cells with fewer people are dropped from the published file
STATUS_COLUMNS = {"Active": "active", "Disengaged": "disengaged",
                  "Milestone complete": "milestone", "Inquiry": "inquiry"}


def split_address(raw):
    """'4131 parrish street apt 2 philadelphia pa 19104' -> ('4131 parrish street', '19104')"""
    s = str(raw or "").strip()
    zips = re.findall(r"\b(19\d{3})\b", s)
    zipcode = zips[-1] if zips else ""
    s = re.sub(r"\b19\d{3}(-\d{4})?\b", " ", s)
    s = re.sub(r"\b(philadelphia|phila|phl)\b|,|\bpa\b|\bpenn(sylvania)?\b", " ", s, flags=re.I)
    s = re.sub(r"\b(apt|unit|fl|floor|rm|room|ste|suite|#)\s*\w+.*$", " ", s, flags=re.I)
    street = re.sub(r"\s+", " ", s).strip()
    if not re.match(r"^\d", street):   # zip-only or no house number: can't place it on a block
        street = ""
    return street, zipcode


def geocode(df):
    rows = df[df["street"] != ""]
    payload = pd.DataFrame({"id": rows.index, "street": rows["street"], "city": "Philadelphia",
                            "state": "PA", "zip": rows["zip"]})
    results = []
    for start in range(0, len(payload), 5000):
        chunk = payload.iloc[start:start + 5000]
        buf = io.StringIO()
        chunk.to_csv(buf, header=False, index=False)
        print(f"Geocoding {start + 1}-{start + len(chunk)} of {len(payload)} ...")
        r = requests.post(GEOCODER, timeout=600,
                          files={"addressFile": ("addresses.csv", buf.getvalue(), "text/csv")},
                          data={"benchmark": "Public_AR_Current"})
        r.raise_for_status()
        res = pd.read_csv(io.StringIO(r.text), header=None, dtype=str,
                          names=["id", "input", "match", "type", "matched", "coords", "tiger", "side"])
        results.append(res)
    res = pd.concat(results)
    res = res[res["match"] == "Match"].copy()
    xy = res["coords"].str.split(",", expand=True).astype(float)
    res["lon"], res["lat"] = xy[0], xy[1]
    res["id"] = res["id"].astype(int)
    return res.set_index("id")[["lon", "lat", "type"]]


def main():
    if not OUTREACH_CSV.exists():
        raise FileNotFoundError(f"Put the member export here (it stays out of git):\n{OUTREACH_CSV}")
    src = pd.read_csv(OUTREACH_CSV)
    df = pd.DataFrame({"status": src["Status"].fillna("Unknown")})
    df[["street", "zip"]] = src["Address"].apply(lambda a: pd.Series(split_address(a)))

    points = df.join(geocode(df), how="left")
    points.drop(columns="street").to_csv(OUTREACH_DIR / "outreach_geocoded_private.csv")

    placed = points.dropna(subset=["lon"]).copy()
    print(f"\nMembers: {len(df):,}   geocoded to an address: {len(placed):,}   "
          f"ZIP only / no address: {(df['street'] == '').sum():,}   "
          f"not matched: {((df['street'] != '') & points['lon'].isna()).sum():,}")

    # ---- grid heat map ----
    placed["gx"] = np.floor(placed["lon"] / GRID_DEG).astype(int)
    placed["gy"] = np.floor(placed["lat"] / GRID_DEG).astype(int)
    placed["col"] = placed["status"].map(STATUS_COLUMNS).fillna("other")
    grid = placed.pivot_table(index=["gx", "gy"], columns="col", values="status", aggfunc="size", fill_value=0)
    grid["total"] = grid.sum(axis=1)
    kept = grid[grid["total"] >= MIN_MEMBERS_PER_CELL].reset_index()
    kept["lon"] = ((kept["gx"] + 0.5) * GRID_DEG).round(5)
    kept["lat"] = ((kept["gy"] + 0.5) * GRID_DEG).round(5)
    cols = ["lat", "lon", "total"] + [c for c in ["active", "disengaged", "milestone", "inquiry", "other"]
                                      if c in kept.columns]
    kept[cols].to_csv(WEB_DATA / "outreach_heat.csv", index=False)
    print(f"Heat map: {len(kept):,} grid cells, {int(kept['total'].sum()):,} members "
          f"({int(grid['total'].sum() - kept['total'].sum()):,} in cells under {MIN_MEMBERS_PER_CELL} left out)")

    # ---- ZIP counts (includes members we could only place by ZIP) ----
    z = df[df["zip"] != ""].copy()
    z["col"] = z["status"].map(STATUS_COLUMNS).fillna("other")
    byzip = z.pivot_table(index="zip", columns="col", values="status", aggfunc="size", fill_value=0)
    byzip["total"] = byzip.sum(axis=1)
    byzip.reset_index().to_csv(WEB_DATA / "outreach_by_zip.csv", index=False)
    print(f"ZIP counts: {len(byzip):,} ZIP codes -> docs/data/outreach_by_zip.csv")


if __name__ == "__main__":
    main()
