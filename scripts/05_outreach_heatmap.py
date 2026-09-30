"""
05_outreach_heatmap.py

Turns the outreach program member list into a privacy-safe heat map layer.

The member file holds people's home addresses, so it NEVER goes into the repo.
This script:
    1. reads data/raw/outreach/gateway_clients.csv  (ID, Status, Address)
    2. matches addresses to the City's OPA property file on this computer
       (no data leaves your machine); anything left over goes to the free
       U.S. Census batch geocoder (only the street address is sent)
    3. snaps every point to a ~250 m grid (the app counts members inside any
       polygon from these cells)
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

import geopandas as gpd

from common import OPA_CSV, OUTREACH_CSV, OUTREACH_DIR, WEB_DATA

GEOCODER = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
GRID_DEG = 0.0025            # ~250 m cells; bigger = blurrier map and rougher polygon counts
MIN_MEMBERS_PER_CELL = 1     # 1 = show every cell; raise it to hide cells with very few members
# Spellings that differ between how people write addresses and how OPA stores them
WORDS = {
    "STREET": "ST", "STR": "ST", "AVENUE": "AVE", "AV": "AVE", "ROAD": "RD", "BOULEVARD": "BLVD",
    "DRIVE": "DR", "LANE": "LN", "PLACE": "PL", "TERRACE": "TER", "COURT": "CT", "PARKWAY": "PKWY",
    "CIRCLE": "CIR", "SQUARE": "SQ", "HIGHWAY": "HWY", "PIKE": "PIKE", "WAY": "WAY",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "FIRST": "1ST", "SECOND": "2ND", "THIRD": "3RD", "FOURTH": "4TH", "FIFTH": "5TH", "SIXTH": "6TH",
    "SEVENTH": "7TH", "EIGHTH": "8TH", "NINTH": "9TH", "TENTH": "10TH",
}
SUFFIXES = {"ST", "AVE", "RD", "BLVD", "DR", "LN", "PL", "TER", "CT", "PKWY", "CIR", "SQ", "HWY", "PIKE", "WAY", "ALY", "WALK", "ROW"}
DIRECTIONS = {"N", "S", "E", "W"}
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


def address_keys(street):
    """'1915 w spencer avenue' -> ['1915 W SPENCER AVE', '1915 W SPENCER', '1915 SPENCER AVE', '1915 SPENCER']
    Most specific first; looser keys catch a missing suffix or direction."""
    words = re.sub(r"[^A-Z0-9 ]", " ", str(street).upper()).split()
    words = [WORDS.get(w, w) for w in words]
    if len(words) < 2 or not words[0][0].isdigit():
        return []
    num = re.sub(r"\D.*$", "", words[0])            # '1915A' -> '1915'
    rest = words[1:]
    dirn = rest[0] if rest and rest[0] in DIRECTIONS and len(rest) > 1 else ""
    if dirn:
        rest = rest[1:]
    suffix = rest[-1] if len(rest) > 1 and rest[-1] in SUFFIXES else ""
    name = " ".join(rest[:-1] if suffix else rest)
    keys = [f"{num} {dirn} {name} {suffix}", f"{num} {dirn} {name}", f"{num} {name} {suffix}", f"{num} {name}"]
    return list(dict.fromkeys(" ".join(k.split()) for k in keys))


def opa_lookup():
    """One point per address key from the City's OPA property file."""
    if not OPA_CSV.exists():
        print(f"OPA file not found ({OPA_CSV}); using only the Census geocoder.")
        return {}
    cols = ["house_number", "street_direction", "street_name", "street_designation", "shape"]
    opa = pd.read_csv(OPA_CSV, usecols=lambda c: c.lower() in cols, dtype=str, low_memory=False)
    opa.columns = [c.lower() for c in opa.columns]
    shape = opa["shape"].fillna("").str.replace(r"^SRID=\d+;", "", regex=True)
    opa = opa[shape.str.upper().str.startswith("POINT") & ~shape.str.upper().str.contains("EMPTY")].copy()
    pts = gpd.GeoSeries.from_wkt(shape[opa.index].to_numpy(dtype=object), crs=2272).to_crs(4326)
    opa["lon"], opa["lat"] = pts.x.to_numpy(), pts.y.to_numpy()
    f = lambda c: opa[c].fillna("").str.upper().str.strip()
    num = f("house_number").str.replace(r"\.0$", "", regex=True)
    d, n, s = f("street_direction"), f("street_name"), f("street_designation")
    lookup = {}
    for k in [num + " " + d + " " + n + " " + s, num + " " + d + " " + n, num + " " + n + " " + s, num + " " + n]:
        k = k.str.split().str.join(" ")
        for key, lon, lat in zip(k, opa["lon"], opa["lat"]):
            lookup.setdefault(key, (lon, lat))
    print(f"OPA address lookup: {len(lookup):,} address keys")
    return lookup


def match_opa(df, lookup):
    out = {}
    for i, street in df["street"].items():
        for k in address_keys(street):
            if k in lookup:
                out[i] = lookup[k]
                break
    res = pd.DataFrame.from_dict(out, orient="index", columns=["lon", "lat"])
    res["type"] = "OPA"
    return res


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

    opa_hits = match_opa(df[df["street"] != ""], opa_lookup())
    print(f"Matched to OPA addresses: {len(opa_hits):,}")
    rest = df[(df["street"] != "") & ~df.index.isin(opa_hits.index)]
    census_hits = geocode(rest) if len(rest) else pd.DataFrame(columns=["lon", "lat", "type"])
    print(f"Matched by Census geocoder: {len(census_hits):,} of {len(rest):,} left over")
    points = df.join(pd.concat([opa_hits, census_hits]), how="left")
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
    kept["cell"] = GRID_DEG
    cols = ["lat", "lon", "cell", "total"] + [c for c in ["active", "disengaged", "milestone", "inquiry", "other"]
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
