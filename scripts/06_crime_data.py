"""
06_crime_data.py

Builds the Crime layer for the web app from Philadelphia Police Department
open data (OpenDataPhilly / City Carto SQL API):

    incidents_part1_part2   Part I and II crime incidents, 2006 to present
    shootings               Shooting victims, 2015 to present
    car_ped_stops           Vehicle and pedestrian stops ("investigations"), 2014 to present

Every record is
    * assigned to a month (only complete months are kept),
    * placed in today's police division, district and PSA (spatial join, so
      old records use current boundaries; records without coordinates fall
      back to the district/PSA written on the record),
    * snapped to the nearest street centerline segment within 150 ft.

Outputs (all in docs/data/):

    police_psa.geojson          Police Service Area boundaries (property "psa")
    police_divisions.geojson    The six patrol divisions, dissolved from districts
    crime/meta.json             months, offense list, coverage and QA counts
    crime/areas.json            monthly counts per offense: city, divisions, districts, PSAs
    crime/segments.json         street segments: seg_id, midpoint, length (ft)
    crime/seg_YYYY.json         monthly counts per offense per street segment, one file per year

Raw downloads are cached in data/processed/crime/raw/ so re-runs only fetch
the current and previous year again. Use --refresh to download everything.

Run from the repo root (takes a while the first time, ~3-4 million records):
    python scripts/06_crime_data.py
    python scripts/06_crime_data.py --start 2015      # fewer years, faster
"""

import argparse
import json
import sys
import time
from datetime import date
from io import StringIO
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

from common import PROCESSED, WEB_DATA

CARTO = "https://phl.carto.com/api/v2/sql"
ARCGIS = "https://services.arcgis.com/fLeGjb7u4uXqeF9q/arcgis/rest/services"
CRS_FT = "EPSG:2272"          # PA State Plane South, US feet
SNAP_FT = 150                  # a record farther than this from every street is left unsnapped
VIOLENT_UCR = {100, 200, 300, 400, 800}

# Current PPD patrol divisions (phillypolice.com districts list, Oct 2026).
DIVISIONS = {
    "Central": ["09", "22"],
    "East": ["24", "25", "26"],
    "Northeast": ["02", "07", "08", "15"],
    "Northwest": ["05", "14", "35", "39"],
    "South": ["01", "03", "17"],
    "Southwest": ["12", "16", "18", "19", "77"],
}
DIST_TO_DIV = {d: div for div, ds in DIVISIONS.items() for d in ds}

OUT = WEB_DATA / "crime"
CACHE = PROCESSED / "crime" / "raw"
OUT.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)

session = requests.Session()


# --------------------------------------------------------------------------- downloads
def carto(sql, fmt="csv", tries=4):
    """Run one query against the City's Carto SQL API (POST, so long queries are fine)."""
    for attempt in range(tries):
        try:
            r = session.post(CARTO, data={"q": sql, "format": fmt}, timeout=600)
            if r.status_code == 200:
                return r.text if fmt == "csv" else r.json()
            msg = r.text[:300]
        except requests.RequestException as err:
            msg = str(err)
        wait = 5 * (attempt + 1)
        print(f"    Carto request failed ({msg}); retrying in {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"Carto query failed after {tries} tries:\n{sql}")


def columns(table):
    j = carto(f"SELECT * FROM {table} LIMIT 0", fmt="json")
    return list((j.get("fields") or {}).keys())


def pick(cols, *names):
    for n in names:
        if n in cols:
            return n
    return None


def arcgis_geojson(service, out_fields="*", page=2000):
    feats, offset = [], 0
    while True:
        r = session.get(f"{ARCGIS}/{service}/FeatureServer/0/query", timeout=300, params={
            "where": "1=1", "outFields": out_fields, "outSR": 4326, "f": "geojson",
            "orderByFields": "objectid", "resultOffset": offset, "resultRecordCount": page})
        r.raise_for_status()
        j = r.json()
        batch = j.get("features", [])
        feats += batch
        offset += len(batch)
        more = j.get("exceededTransferLimit") or (j.get("properties") or {}).get("exceededTransferLimit")
        if not batch or not more:
            break
    if not feats:
        raise RuntimeError(f"{service} returned no features")
    return gpd.GeoDataFrame.from_features(feats, crs=4326)


def download_year(source, year, refresh):
    """One year of one source as a DataFrame with: date, offense, ucr, kind, lon, lat, dc_dist, psa_raw."""
    path = CACHE / f"{source['name']}_{year}.csv.gz"
    recent = year >= date.today().year - 1
    if path.exists() and not refresh and not recent:
        return pd.read_csv(path, dtype=str)
    sql = source["sql"](year)
    print(f"    downloading {source['name']} {year} ...", end=" ", flush=True)
    df = pd.read_csv(StringIO(carto(sql)), dtype=str)
    df.to_csv(path, index=False, compression="gzip")
    print(f"{len(df):,} rows")
    return df


def build_sources():
    """SQL for each table, using whichever column names the table actually has."""
    sources = []

    cols = columns("incidents_part1_part2")
    dcol = pick(cols, "dispatch_date", "dispatch_date_time")
    sources.append({
        "name": "incidents", "first": 2006,
        "sql": lambda y, d=dcol: (
            f"SELECT ST_X(the_geom) AS lon, ST_Y(the_geom) AS lat, {d}::date AS date, "
            f"ucr_general AS ucr, text_general_code AS offense, dc_dist, psa AS psa_raw "
            f"FROM incidents_part1_part2 WHERE {d} >= '{y}-01-01' AND {d} < '{y + 1}-01-01'"),
    })

    try:
        cols = columns("shootings")
        dcol = pick(cols, "date_", "date", "datetime_occur")
        fatal = pick(cols, "fatal")
        dist = pick(cols, "dist", "dc_dist", "district")
        if dcol:
            label = ("CASE WHEN " + fatal + "::text IN ('1','1.0','true','t','Y','Yes') "
                     "THEN 'Shooting victim - fatal' ELSE 'Shooting victim - nonfatal' END") if fatal else "'Shooting victim'"
            dist_sql = f"{dist}::text" if dist else "NULL"
            sources.append({
                "name": "shootings", "first": 2015,
                "sql": lambda y, d=dcol, lab=label, ds=dist_sql: (
                    f"SELECT ST_X(the_geom) AS lon, ST_Y(the_geom) AS lat, {d}::date AS date, "
                    f"{lab} AS offense, {ds} AS dc_dist, NULL AS psa_raw "
                    f"FROM shootings WHERE {d} >= '{y}-01-01' AND {d} < '{y + 1}-01-01'"),
            })
        else:
            print("  ! shootings table has no date column; skipping shootings")
    except RuntimeError as err:
        print(f"  ! shootings not available ({err}); skipping")

    try:
        cols = columns("car_ped_stops")
        dcol = pick(cols, "datetimeoccur", "datetime_occur", "date_value")
        tcol = pick(cols, "stoptype", "stop_type")
        dist = pick(cols, "districtoccur", "district", "dc_dist")
        psa = pick(cols, "psa")
        if dcol and tcol:
            sources.append({
                "name": "stops", "first": 2014,
                "sql": lambda y, d=dcol, t=tcol, ds=dist, p=psa: (
                    f"SELECT ST_X(the_geom) AS lon, ST_Y(the_geom) AS lat, {d}::date AS date, "
                    f"CASE WHEN lower({t}) LIKE 'veh%' THEN 'Vehicle stop' ELSE 'Pedestrian stop' END AS offense, "
                    f"{ds + '::text' if ds else 'NULL'} AS dc_dist, {p + '::text' if p else 'NULL'} AS psa_raw "
                    f"FROM car_ped_stops WHERE {d} >= '{y}-01-01' AND {d} < '{y + 1}-01-01'"),
            })
        else:
            print("  ! car_ped_stops columns not recognized; skipping stops")
    except RuntimeError as err:
        print(f"  ! car_ped_stops not available ({err}); skipping")
    return sources


# --------------------------------------------------------------------------- boundaries
def load_boundaries():
    dists = gpd.read_file(WEB_DATA / "police_districts.geojson").to_crs(CRS_FT)
    dists["geometry"] = dists.geometry.make_valid().buffer(0)        # the City file has small self-intersections
    dists["dist"] = dists["dist_numc"].astype(str).str.zfill(2)

    print("Downloading police service areas (Boundaries_PSA) ...")
    psa = arcgis_geojson("Boundaries_PSA")
    key = next((c for c in psa.columns if c.lower() in ("psa_num", "psa", "psanum", "psa_no")), None)
    if key is None:
        key = next(c for c in psa.columns if "psa" in c.lower())
    psa["psa"] = psa[key].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    psa["geometry"] = psa.geometry.make_valid().buffer(0)
    psa = psa[["psa", "geometry"]]
    psa.to_file(WEB_DATA / "police_psa.geojson", driver="GeoJSON", COORDINATE_PRECISION=6)
    print(f"  {len(psa)} PSAs -> docs/data/police_psa.geojson")

    divs = dists.assign(division=dists["dist"].map(DIST_TO_DIV)).dropna(subset=["division"])
    divs = divs.dissolve("division", as_index=False, grid_size=0.5)[["division", "geometry"]]
    divs["geometry"] = divs.geometry.buffer(2).buffer(-2)              # close slivers between districts
    divs["districts"] = divs["division"].map(lambda d: ", ".join(str(int(x)) for x in DIVISIONS[d]))
    divs.to_crs(4326).to_file(WEB_DATA / "police_divisions.geojson", driver="GeoJSON", COORDINATE_PRECISION=6)
    print(f"  {len(divs)} divisions -> docs/data/police_divisions.geojson")
    return dists[["dist", "geometry"]], psa.to_crs(CRS_FT)


def load_streets():
    path = PROCESSED / "crime" / "street_centerline.gpkg"
    if path.exists():
        return gpd.read_file(path)
    print("Downloading street centerlines (Street_Centerline) ...")
    st = arcgis_geojson("Street_Centerline", "objectid,seg_id")
    st = st[st.geometry.notna() & ~st.geometry.is_empty].copy()
    st["seg_id"] = st["seg_id"].astype("int64")
    st = st.to_crs(CRS_FT)[["seg_id", "geometry"]]
    st.to_file(path, driver="GPKG")
    print(f"  {len(st):,} segments")
    return st


# --------------------------------------------------------------------------- processing
def tidy_offense(s):
    s = " ".join(str(s).split())
    fixes = {"Homicide - Criminal ": "Homicide - Criminal"}
    s = fixes.get(s, s)
    return s[:1].upper() + s[1:].lower() if s.isupper() else s


def prepare(df, source):
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["kind"] = {"incidents": "crime", "shootings": "shooting", "stops": "stop"}[source]
    if source == "incidents":
        df["ucr"] = pd.to_numeric(df["ucr"], errors="coerce")
        df = df.dropna(subset=["ucr"])
        df["ucr"] = df["ucr"].astype(int)
        df["offense"] = df["offense"].fillna("Unknown").map(tidy_offense)
    else:
        df["ucr"] = 0
    df["dc_dist"] = df["dc_dist"].fillna("").astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(2)
    df["psa_raw"] = df["psa_raw"].fillna("").astype(str).str.strip()
    # Coordinates outside Philadelphia's box are geocoding failures.
    bad = ~df["lon"].between(-75.30, -74.95) | ~df["lat"].between(39.86, 40.14)
    df.loc[bad, ["lon", "lat"]] = np.nan
    return df[["date", "kind", "ucr", "offense", "lon", "lat", "dc_dist", "psa_raw"]]


def place(df, dists, psas, streets):
    """Spatial joins: district, PSA, division and nearest street segment."""
    has = df["lon"].notna()
    pts = gpd.GeoDataFrame(df[has], geometry=gpd.points_from_xy(df.loc[has, "lon"], df.loc[has, "lat"]), crs=4326).to_crs(CRS_FT)
    pts = pts[["geometry"]].copy()
    d = gpd.sjoin(pts, dists, how="left", predicate="within")
    d = d[~d.index.duplicated()]
    p = gpd.sjoin(pts, psas, how="left", predicate="within")
    p = p[~p.index.duplicated()]
    s = gpd.sjoin_nearest(pts, streets, how="left", max_distance=SNAP_FT)
    s = s[~s.index.duplicated()]
    df["dist"] = df["dc_dist"]
    df.loc[d.index, "dist"] = d["dist"].fillna(df.loc[d.index, "dc_dist"])
    df["psa"] = ""
    df.loc[p.index, "psa"] = p["psa"].fillna("")
    # Records without coordinates: try the PSA written on the record (district + PSA letter/number).
    psa_set = set(psas["psa"])
    nopsa = df["psa"] == ""
    guess = df.loc[nopsa, "dc_dist"] + df.loc[nopsa, "psa_raw"]
    df.loc[nopsa, "psa"] = guess.where(guess.isin(psa_set), "")
    df["division"] = df["dist"].map(DIST_TO_DIV).fillna("")
    df["seg_id"] = -1
    df.loc[s.index, "seg_id"] = s["seg_id"].fillna(-1).astype("int64")
    return df


def sparse(group_df, month_idx, off_idx):
    """[monthIndex, offenseIndex, count, ...] for one area."""
    g = group_df.groupby(["month", "offense_key"]).size()
    out = []
    for (m, o), n in g.items():
        out += [month_idx[m], off_idx[o], int(n)]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=int, default=2006, help="first year to include (default 2006)")
    ap.add_argument("--refresh", action="store_true", help="re-download every year, not just the latest two")
    args = ap.parse_args()

    dists, psas = load_boundaries()
    streets = load_streets()
    sources = build_sources()
    print("Sources:", ", ".join(s["name"] for s in sources))

    this_year = date.today().year
    frames, qa = [], {}
    for src in sources:
        for year in range(max(args.start, src["first"]), this_year + 1):
            raw = download_year(src, year, args.refresh)
            if raw.empty:
                continue
            df = place(prepare(raw, src["name"]), dists, psas, streets)
            q = qa.setdefault(str(year), {})
            q[src["name"]] = {
                "records": int(len(df)),
                "no_coordinates": int(df["lon"].isna().sum()),
                "not_on_a_street": int(((df["seg_id"] < 0) & df["lon"].notna()).sum()),
                "no_district": int((df["dist"].str.strip("0") == "").sum()),
            }
            df = df.drop(columns=["lon", "lat", "dc_dist", "psa_raw"])
            for c in ("kind", "offense", "dist", "psa", "division"):
                df[c] = df[c].astype("category")
            frames.append(df)
            print(f"  {src['name']} {year}: {len(df):,} records")

    data = pd.concat(frames, ignore_index=True)
    for c in ("kind", "offense", "dist", "psa", "division"):
        data[c] = data[c].astype(str)
    data["month"] = data["date"].dt.strftime("%Y-%m")

    # Keep complete months only: the latest month is dropped unless the data reaches its last day.
    last = data.loc[data["kind"] == "crime", "date"].max()
    last_complete = last if (last + pd.Timedelta(days=1)).month != last.month else (last.replace(day=1) - pd.Timedelta(days=1))
    data = data[data["date"] <= last_complete]
    months = pd.period_range(data["date"].min(), last_complete, freq="M").strftime("%Y-%m").tolist()
    month_idx = {m: i for i, m in enumerate(months)}

    # Offense list: crime by UCR code then name, then shootings, then stops.
    data["offense_key"] = data["kind"] + "|" + data["ucr"].astype(str) + "|" + data["offense"]
    offs = data.groupby("offense_key").agg(kind=("kind", "first"), ucr=("ucr", "first"), offense=("offense", "first"), n=("kind", "size")).reset_index()
    kind_order = {"crime": 0, "shooting": 1, "stop": 2}
    offs["o"] = offs["kind"].map(kind_order)
    offs = offs.sort_values(["o", "ucr", "n"], ascending=[True, True, False]).reset_index(drop=True)
    off_idx = {k: i for i, k in enumerate(offs["offense_key"])}
    offense_list = [{
        "label": r.offense, "kind": r.kind, "ucr": int(r.ucr),
        "cls": ("violent" if r.ucr in VIOLENT_UCR else "nonviolent") if r.kind == "crime" else r.kind,
        "records": int(r.n),
    } for r in offs.itertuples()]

    # Areas
    print("Aggregating areas ...")
    areas = {"city": sparse(data, month_idx, off_idx), "division": {}, "dist": {}, "psa": {}}
    for col, key in (("division", "division"), ("dist", "dist"), ("psa", "psa")):
        for val, g in data[data[col].astype(str).str.strip("0") != ""].groupby(col):
            areas[key][str(val)] = sparse(g, month_idx, off_idx)
    (OUT / "areas.json").write_text(json.dumps(areas, separators=(",", ":")))

    # Street segments
    print("Writing street segments ...")
    used = data.loc[data["seg_id"] >= 0, "seg_id"].unique()
    mids = streets.geometry.interpolate(0.5, normalized=True).to_crs(4326)
    segs = pd.DataFrame({"seg_id": streets["seg_id"].values, "ft": streets.geometry.length.round(0).astype(int).values,
                         "lon": mids.x.round(5).values, "lat": mids.y.round(5).values})
    segs = segs.drop_duplicates("seg_id").reset_index(drop=True)
    seg_idx = {s: i for i, s in enumerate(segs["seg_id"])}
    (OUT / "segments.json").write_text(json.dumps({
        "seg_id": segs["seg_id"].astype(int).tolist(), "ft": segs["ft"].tolist(),
        "lon": segs["lon"].tolist(), "lat": segs["lat"].tolist()}, separators=(",", ":")))

    on_seg = data[data["seg_id"] >= 0]
    years = sorted(on_seg["date"].dt.year.unique())
    for y in years:
        g = on_seg[on_seg["date"].dt.year == y].groupby(["seg_id", "month", "offense_key"]).size()
        rows = []
        for (s, m, o), n in g.items():
            rows += [seg_idx[s], month_idx[m], off_idx[o], int(n)]
        p = OUT / f"seg_{y}.json"
        p.write_text(json.dumps({"year": int(y), "rows": rows}, separators=(",", ":")))
        print(f"  seg_{y}.json  {len(rows) // 4:,} rows  ({p.stat().st_size / 1024**2:,.1f} MB)")

    coverage = {k: {"first": data.loc[data["kind"] == k, "month"].min(), "last": data.loc[data["kind"] == k, "month"].max()}
                for k in data["kind"].unique()}
    meta = {
        "generated": date.today().isoformat(),
        "months": months,
        "years": [int(y) for y in years],
        "offenses": offense_list,
        "coverage": coverage,
        "violent_ucr": sorted(VIOLENT_UCR),
        "snap_feet": SNAP_FT,
        "divisions": DIVISIONS,
        "qa": qa,
        "segments_with_records": int(len(used)),
        "sources": {
            "incidents": "https://opendataphilly.org/datasets/crime-incidents/",
            "shootings": "https://opendataphilly.org/datasets/shooting-victims/",
            "stops": "https://opendataphilly.org/datasets/vehicle-pedestrian-investigations/",
        },
    }
    (OUT / "meta.json").write_text(json.dumps(meta, separators=(",", ":")))
    print(f"Done: {len(data):,} records, {months[0]} to {months[-1]}, files in docs/data/crime/")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
