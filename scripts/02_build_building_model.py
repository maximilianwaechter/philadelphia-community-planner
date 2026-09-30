"""
02_build_building_model.py   (was: philly_building.py)

Building-level residential population model for Philadelphia.

    ACS tracts (script 01) ... master totals. Every tract adds back up to these.
    Building footprints ...... WHERE people can live (BIN = BUILDING_ID)
    OPA properties ........... HOW MANY units each building has, owner-occupied
                               signal (homestead exemption), use type
    Vacant Property Indicators which buildings get nobody

Steps
    1  Footprints            6  Buildings -> tracts
    2  OPA records + classes 7  Units per building (exact, livable-area proxy, minimum)
    3  OPA -> footprint      8  Occupied owner / renter units, balanced to ACS per tract
    4  One row per building  9  Household population (owner vs renter household size)
    5  Vacancy (VPI)        10  Group-quarters population (dorms, nursing homes...)
                            11  Building-level estimates of ACS counts
                            12  Outputs + tract QA

Inputs  (data/raw/, see README)
    OPA_PROPERTIES_PUBLIC.csv, LI_BUILDING_FOOTPRINTS.geojson,
    Vacant_Indicators_Bldg.geojson, optional group_quarters_buildings.csv
Needs   data/processed/acs/philly_tracts_acs2024.gpkg  (run script 01 first)
Outputs data/processed/building_model/

This is a MODEL, not address-level Census data.

Run from the repo root:
    python scripts/02_build_building_model.py
"""

import numpy as np
import pandas as pd
import geopandas as gpd

from common import (BUILDING_POINTS_GPKG, FOOTPRINTS_GEOJSON, GQ_BUILDINGS_CSV, MODEL_DIR,
                    OPA_CSV, TRACT_GPKG, VACANT_GEOJSON)

# =============================================================================
# SETTINGS
# =============================================================================
WORK_CRS = 2272                      # PA South State Plane, feet
NEAREST_BUILDING_MAX_FT = 75         # OPA points outside every footprint snap to one this close
VPI_VACANCY_THRESHOLD = 0.50
EXCLUDE_VPI_VACANT = True            # VPI buildings get zero households

# Starting guess that a unit WITHOUT a homestead exemption is owner-occupied.
# Only a seed: step 8 rescales every tract to ACS owner_occ / renter_occ.
P_OWNER_NO_HOMESTEAD = {"single": 0.35, "condo": 0.35, "multi": 0.05}
IPF_ITERATIONS = 50
EXPECTED_OPA_RECORDS = 500_000       # Philadelphia has ~580k OPA accounts; warn if far fewer

GQ_PATTERN = (r"DORM|NURSING|ASSISTED|CONVALESC|CONVENT|RECTORY|MONASTER|SHELTER|"
              r"GROUP HOME|PRISON|CORRECTION|JAIL|DETENTION|BARRACKS|RESIDENCE HALL")

RES_PATTERN = (r"SINGLE.?FAMILY|TWO.?FAMILY|THREE.?FAMILY|FOUR.?FAMILY|MULTI.?FAMILY|RESIDENT|"
               r"APARTMENT|\bAPT\b|CONDO|DUPLEX|TRIPLEX")
COM_PATTERN = r"COMMERCIAL|RETAIL|OFFICE|STORE|SHOPPING|HOTEL|MOTEL"
IND_PATTERN = r"INDUSTRIAL|WAREHOUSE|FACTORY|MANUFACTUR"
INST_PATTERN = r"UNIVERSITY|COLLEGE|SCHOOL|HOSPITAL|CHURCH|WORSHIP|INSTITUTION"

# ACS count fields and which building weight splits them.
#   pop    people (household + group quarters)
#   occ    occupied housing units / households
#   own    owner-occupied units      rnt  renter-occupied units
#   hhpop  household population
ALLOCATE = {
    "hh_pop": "hhpop",
    "hh_occ": "occ", "veh_hh": "occ", "no_vehicle": "occ", "inet_univ": "occ",
    "owner_occ": "own", "renter_occ": "rnt",
    **{f: "pop" for f in [
        "pop_total", "race_total", "white_nh", "black_nh", "asian_nh", "hispanic",
        "pov_univ", "pov_below", "under18", "age65plus", "workers", "drv_alone", "pub_trans",
        "bicycle", "walked", "wfh", "sch_univ", "k12_enr", "pop25plus", "hs_grad", "bachelors",
        "masters", "prof_deg", "doctorate", "labor_frc", "employed", "unemployed",
        "hins_univ", "dis_univ", "lang_univ"]},
}


# =============================================================================
# HELPERS
# =============================================================================
def normalize_columns(df):
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def first_existing(df, candidates, required=False):
    for name in candidates:
        if name.lower() in df.columns:
            return name.lower()
    if required:
        raise RuntimeError("Could not find any of these fields:\n" + "\n".join(candidates))
    return None


def numeric(series):
    return pd.to_numeric(series, errors="coerce")


def safe_ratio(num, den):
    return num / den.where(den != 0)


def text(df, cols):
    out = pd.Series("", index=df.index)
    for c in cols:
        if c in df.columns:
            out = out + " " + df[c].fillna("").astype(str).str.upper()
    return out.str.strip()


def concat_unique(series, max_values=50):
    vals = list(dict.fromkeys(str(v).strip() for v in series.dropna() if str(v).strip()))
    if not vals:
        return None
    if len(vals) > max_values:
        return " | ".join(vals[:max_values]) + f" | ... ({len(vals)} total)"
    return " | ".join(vals)


# =============================================================================
# 2. OPA POINTS + RECORD CLASSIFICATION
# =============================================================================
def opa_points_from_shape(opa):
    """The full OPA CSV from OpenDataPhilly stores location as
    'SRID=2272;POINT (x y)' in a column called shape."""
    raw = opa["shape"].astype("string").str.strip()
    srid = numeric(raw.str.extract(r"^SRID=(\d+);", expand=False))
    wkt = raw.str.replace(r"^SRID=\d+;", "", regex=True)
    good = wkt.str.upper().str.startswith("POINT", na=False) & ~wkt.str.upper().str.contains("EMPTY", na=False)
    print(f"OPA coordinates: shape column; records without a location: {(~good).sum():,}")
    opa, wkt = opa.loc[good].copy(), wkt[good]
    codes = srid[good].dropna().unique()
    src = int(codes[0]) if len(codes) else 2272
    if len(codes) > 1:
        print(f"  WARNING: several SRIDs in shape column {codes}; using EPSG:{src}")
    print(f"Detected OPA CRS: EPSG:{src}")
    geom = gpd.GeoSeries.from_wkt(wkt.to_numpy(dtype=object), crs=src)
    geom.index = opa.index
    return gpd.GeoDataFrame(opa, geometry=geom, crs=src).to_crs(WORK_CRS)


def create_opa_points(opa):
    lat_col = first_existing(opa, ["geocode_lat", "latitude", "lat", "y"])
    lon_col = first_existing(opa, ["geocode_lon", "longitude", "lon", "lng", "x"])
    if (not lat_col or not lon_col) and "shape" in opa.columns:
        return opa_points_from_shape(opa)
    if not lat_col or not lon_col:
        print("\nOPA columns:", sorted(opa.columns))
        raise RuntimeError("Could not identify OPA coordinate fields.")
    x, y = numeric(opa[lon_col]), numeric(opa[lat_col])
    good = x.notna() & y.notna()
    print(f"OPA coordinates: {lon_col}/{lat_col}; records without coordinates: {(~good).sum():,}")
    opa, x, y = opa.loc[good].copy(), x[good], y[good]
    src = 4326 if (x.abs().median() <= 180 and y.abs().median() <= 90) else 3857
    print(f"Detected OPA CRS: EPSG:{src}")
    return gpd.GeoDataFrame(opa, geometry=gpd.points_from_xy(x, y), crs=src).to_crs(WORK_CRS)


def classify_opa_records(opa):
    """Vectorized version of the original classify_opa_record(). Current category
    and new building description come first; legacy description only if blank."""
    primary = text(opa, ["category_code_description", "building_code_description_new"])
    legacy = text(opa, ["building_code_description"])
    txt = primary.where(primary.str.len() > 0, legacy)

    mixed = txt.str.contains("MIXED")
    opa["opa_vacant"] = primary.str.contains(r"\bVACANT\b").astype(int)
    opa["opa_residential"] = (txt.str.contains(RES_PATTERN) | mixed).astype(int)
    opa["opa_commercial"] = (txt.str.contains(COM_PATTERN) | mixed).astype(int)
    opa["opa_industrial"] = txt.str.contains(IND_PATTERN).astype(int)
    opa["opa_institutional"] = txt.str.contains(INST_PATTERN).astype(int)
    opa["opa_gq"] = text(opa, ["category_code_description", "building_code_description_new",
                              "building_code_description"]).str.contains(GQ_PATTERN).astype(int)

    # ---- units per record (same rules as infer_record_units) ----
    units_exact = pd.Series(np.nan, index=opa.index)
    units_min = pd.Series(1.0, index=opa.index)
    method = pd.Series("residential_unknown", index=opa.index)
    rules = [  # later rules only apply where no earlier rule matched
        ("CONDO", 1, 1, "condo_account_1"),
        (r"TWO.?FAMILY|2.?FAMILY|DUPLEX", 2, 2, "opa_two_family"),
        (r"THREE.?FAMILY|3.?FAMILY|TRIPLEX", 3, 3, "opa_three_family"),
        (r"FOUR.?FAMILY|4.?FAMILY", 4, 4, "opa_four_family"),
        (r"APARTMENTS?\s*>\s*4|APARTMENTS?.*4.*UNITS", np.nan, 5, "apartment_gt4_unknown"),
        (r"APARTMENT|MULTI.?FAMILY", np.nan, 2, "multifamily_unknown"),
        (r"SINGLE.?FAMILY", 1, 1, "single_family_1"),
    ]
    done = pd.Series(False, index=opa.index)
    for pattern, exact, minimum, name in rules:
        hit = primary.str.contains(pattern) & ~done
        units_exact[hit], units_min[hit], method[hit] = exact, minimum, name
        done |= hit
    nonres = opa["opa_residential"] == 0
    units_exact[nonres], units_min[nonres], method[nonres] = 0, 0, "non_residential"
    opa["units_exact_record"], opa["units_min_record"], opa["unit_record_method"] = units_exact, units_min, method
    return opa


# =============================================================================
# 3. OPA -> BUILDING FOOTPRINT
# =============================================================================
def match_opa_to_buildings(opa_points, footprints):
    fp = footprints[["BUILDING_ID", "fp_area_sqft", "geometry"]]
    exact = gpd.sjoin(opa_points, fp, how="left", predicate="within")
    exact["match_method"] = np.where(exact["BUILDING_ID"].notna(), "within", None)
    exact["match_distance_ft"] = np.where(exact["BUILDING_ID"].notna(), 0.0, np.nan)
    exact = exact.sort_values(["opa_row_id", "fp_area_sqft"], na_position="last") \
                 .drop_duplicates("opa_row_id", keep="first")
    matched = exact[exact["BUILDING_ID"].notna()]
    missing = opa_points[opa_points["opa_row_id"].isin(exact.loc[exact["BUILDING_ID"].isna(), "opa_row_id"])]
    if len(missing):
        near = gpd.sjoin_nearest(missing, fp, how="left", max_distance=NEAREST_BUILDING_MAX_FT,
                                 distance_col="match_distance_ft")
        near["match_method"] = np.where(near["BUILDING_ID"].notna(), "nearest", None)
        near = near.sort_values(["opa_row_id", "match_distance_ft", "fp_area_sqft"], na_position="last") \
                   .drop_duplicates("opa_row_id", keep="first")
        matched = pd.concat([matched, near], ignore_index=True)
    return gpd.GeoDataFrame(matched, geometry="geometry", crs=opa_points.crs)


def print_match_diagnostics(opa_points, opa_building, footprints):
    """The first model version put ~680k households into only ~85k units of
    capacity. These numbers show where residential records are being lost."""
    res = opa_points["opa_residential"] == 1
    print("\n--- OPA / footprint diagnostics ---")
    print(f"OPA records read:            {len(opa_points):,}")
    if len(opa_points) < EXPECTED_OPA_RECORDS:
        print(f"  WARNING: expected ~580,000 OPA accounts. The file may be a partial export "
              f"(ArcGIS/Carto download limits). Re-download the full CSV from OpenDataPhilly.")
    print(f"Residential OPA records:     {res.sum():,}")
    print("Unit methods:", opa_points.loc[res, "unit_record_method"].value_counts().to_dict())
    m = opa_building[opa_building["opa_residential"] == 1]
    print(f"Residential records matched: {len(m):,}  {m['match_method'].value_counts().to_dict()}")
    per_fp = m.groupby("BUILDING_ID").size()
    print(f"Footprints with residential: {len(per_fp):,} of {len(footprints):,}")
    print(f"Residential records per footprint: median {per_fp.median():.0f}, "
          f"95th pct {per_fp.quantile(.95):.0f}, max {per_fp.max():,}")
    big = per_fp[per_fp > 25]
    if len(big):
        print(f"  {len(big):,} footprints hold >25 residential records ({big.sum():,} records). "
              f"If these are not condo towers, footprints may be merged row blocks.")
    print("-----------------------------------\n")


# =============================================================================
# 4. ONE RECORD PER BUILDING
# =============================================================================
def aggregate_opa_buildings(matched):
    m = matched.copy()
    m["res"] = m["opa_residential"] == 1
    def col(name):
        return numeric(m[name]) if name in m.columns else pd.Series(np.nan, index=m.index)

    m["homestead"] = (col("homestead_exemption").fillna(0) > 0) & m["res"]
    m["unknown_units"] = m["res"] & m["units_exact_record"].isna()
    m["known_units"] = numeric(m["units_exact_record"]).where(m["res"], 0).fillna(0)
    m["min_units"] = numeric(m["units_min_record"]).where(m["res"], 0).fillna(0)
    m["condo"] = m["res"] & (m["unit_record_method"] == "condo_account_1")
    m["res_livable"] = col("total_livable_area").where(m["res"])
    m["mv"] = col("market_value")

    g = m.groupby("BUILDING_ID")
    out = pd.DataFrame({
        "opa_record_count": g.size(),
        "has_residential": g["opa_residential"].max(),
        "has_commercial": g["opa_commercial"].max(),
        "has_industrial": g["opa_industrial"].max(),
        "has_institutional": g["opa_institutional"].max(),
        "has_gq_use": g["opa_gq"].max(),
        "opa_vacant_flag": g["opa_vacant"].max(),
        "residential_opa_records": g["res"].sum(),
        "unknown_unit_records": g["unknown_units"].sum(),
        "res_units_known_sum": g["known_units"].sum(),
        "res_units_min": g["min_units"].sum(),
        "opa_res_livable_sqft": g["res_livable"].sum(min_count=1),
        "homestead_count": g["homestead"].sum(),
        "condo_records": g["condo"].sum(),
        "market_value_sum": g["mv"].sum(min_count=1),
    })
    out["res_units_exact"] = np.where(out["residential_opa_records"] == 0, 0,
                                      np.where(out["unknown_unit_records"] == 0, out["res_units_known_sum"], np.nan))
    if "parcel_number" in m.columns:
        out["opa_parcel_count"] = g["parcel_number"].nunique()
    if "year_built" in m.columns:
        out["year_built_model"] = g["year_built"].apply(lambda s: numeric(s).median())
    for col, src in [("opa_categories", "category_code_description"),
                     ("opa_building_types", "building_code_description_new")]:
        if src in m.columns:
            out[col] = g[src].apply(concat_unique)
    return out.reset_index()


def classify_building_use(b):
    res, com, ind, inst = (b[c].astype(bool) for c in
                           ["has_residential", "has_commercial", "has_industrial", "has_institutional"])
    return np.select(
        [res & (com | ind | inst), res, com, ind, inst, b["opa_vacant_flag"].astype(bool)],
        ["Mixed Use - Residential", "Residential", "Commercial", "Industrial", "Institutional", "OPA Vacant"],
        "Unknown")


# =============================================================================
# 8. OCCUPIED OWNER / RENTER UNITS, BALANCED TO ACS
# =============================================================================
def balance_occupied_units(b):
    """Two-way raking inside each tract.
       building totals: its available units * (ACS hh_occ / tract available units)
       tract totals:    owner units = ACS owner_occ, renter units = ACS renter_occ
    Vacant buildings start at zero and stay at zero."""
    tract = b["TRACT_GEOID"]
    hh_occ = numeric(b["hh_occ"]).fillna(0)
    units = numeric(b["res_units_model"]).fillna(0)
    b["allocation_fallback"] = ""

    avail = units.where(b["population_eligible"], 0.0)
    t_avail = avail.groupby(tract).transform("sum")
    # fallback A: tract has households, but every unit is flagged vacant -> ignore VPI there
    need = (t_avail <= 0) & (hh_occ > 0) & (units > 0)
    avail = avail.where(~need, units)
    b.loc[need, "allocation_fallback"] = "vacancy_flag_ignored"
    t_avail = avail.groupby(tract).transform("sum")
    # fallback B: still nothing -> any footprint in the tract, by floor area
    need = (t_avail <= 0) & (hh_occ > 0)
    avail = avail.where(~need, b["floor_area_sqft"] / 1000.0)
    b.loc[need, "allocation_fallback"] = "footprint_area"
    t_avail = avail.groupby(tract).transform("sum")

    b["occ_target"] = (avail * hh_occ / t_avail.where(t_avail > 0)).fillna(0)

    # seeds: homestead units are owners; other units get a starting owner probability
    h = np.minimum(b["homestead_count"].fillna(0), units)
    rest = (units - h).clip(lower=0)
    kind = np.where(b["condo_records"].fillna(0) > 0, "condo", np.where(units <= 1, "single", "multi"))
    p = pd.Series(kind, index=b.index).map(P_OWNER_NO_HOMESTEAD)
    own = (h + p * rest).where(avail > 0, 0.0)
    rnt = (units - own).clip(lower=0).where(avail > 0, 0.0)
    fb = b["allocation_fallback"] == "footprint_area"
    own[fb], rnt[fb] = 0.5, 0.5
    # a tenure with no seed anywhere in the tract is spread over available units
    for s in (own, rnt):
        empty = s.groupby(tract).transform("sum") <= 0
        s[empty] = avail[empty]

    own_t, rnt_t = numeric(b["owner_occ"]).fillna(0), numeric(b["renter_occ"]).fillna(0)
    target = b["occ_target"].to_numpy()
    own, rnt = own.to_numpy(float), rnt.to_numpy(float)
    for _ in range(IPF_ITERATIONS):
        so = pd.Series(own, index=b.index).groupby(tract).transform("sum").to_numpy()
        sr = pd.Series(rnt, index=b.index).groupby(tract).transform("sum").to_numpy()
        own = np.where(so > 0, own * own_t / np.where(so > 0, so, 1), 0)
        rnt = np.where(sr > 0, rnt * rnt_t / np.where(sr > 0, sr, 1), 0)
        tot = own + rnt
        f = np.where(tot > 0, target / np.where(tot > 0, tot, 1), 0)
        own, rnt = own * f, rnt * f
    b["owner_units_occ"], b["renter_units_occ"] = own, rnt
    b["occupied_units"] = own + rnt
    return b


# =============================================================================
# 9-10. PEOPLE
# =============================================================================
def assign_people(b, gq_ids):
    tract = b["TRACT_GEOID"]
    avg = numeric(b["avg_hh"])
    own_sz = numeric(b["avg_own_hh"]).fillna(avg).fillna(2.2)
    rnt_sz = numeric(b["avg_rnt_hh"]).fillna(avg).fillna(2.2)
    raw = b["owner_units_occ"] * own_sz + b["renter_units_occ"] * rnt_sz
    s = raw.groupby(tract).transform("sum")
    b["est_hh_pop"] = (raw * numeric(b["hh_pop"]).fillna(0) / s.where(s > 0)).fillna(0)

    # group quarters: dorm / nursing / institutional-residence buildings only
    b["gq_candidate"] = (b["has_gq_use"] == 1) | b["BUILDING_ID"].isin(gq_ids)
    w = b["floor_area_sqft"].where(b["gq_candidate"], 0.0)
    ws = w.groupby(tract).transform("sum")
    b["est_gq_pop"] = (w * numeric(b["gq_pop"]).fillna(0) / ws.where(ws > 0)).fillna(0)
    b["est_pop_total"] = b["est_hh_pop"] + b["est_gq_pop"]
    return b


# =============================================================================
# MAIN
# =============================================================================
def main():
    for path in [OPA_CSV, FOOTPRINTS_GEOJSON, VACANT_GEOJSON, TRACT_GPKG]:
        if not path.exists():
            raise FileNotFoundError(f"Missing input:\n{path}\n(see README - 'Get the raw data')")

    # ---- 1. footprints ----
    print("\nReading building footprints...")
    fp = normalize_columns(gpd.read_file(FOOTPRINTS_GEOJSON))
    if fp.crs is None:
        raise RuntimeError("Building footprint GeoJSON has no CRS.")
    fp = fp.to_crs(WORK_CRS)
    bin_col = first_existing(fp, ["bin"])
    fp["BUILDING_ID"] = fp[bin_col].astype("string").str.strip() if bin_col else pd.NA
    missing = fp["BUILDING_ID"].isna() | (fp["BUILDING_ID"] == "")
    fp.loc[missing, "BUILDING_ID"] = [f"FP_{i:08d}" for i in fp.index[missing]]
    dup = fp["BUILDING_ID"].duplicated(keep=False)
    if dup.any():   # merges on BUILDING_ID multiply rows if BINs repeat
        print(f"  {dup.sum():,} footprints share a BIN - giving them unique IDs")
        fp.loc[dup, "BUILDING_ID"] = fp.loc[dup, "BUILDING_ID"] + "_" + fp.index[dup].astype(str)
    fp["fp_area_sqft"] = fp.geometry.area
    fp = fp.rename(columns={k: v for k, v in {
        "address": "fp_address", "building_name": "fp_building_name", "square_ft": "fp_square_ft_source",
        "approx_hgt": "fp_approx_hgt", "max_hgt": "fp_max_hgt", "parcel_id_num": "fp_parcel_id"}.items()
        if k in fp.columns})
    height = numeric(fp["fp_max_hgt"]) if "fp_max_hgt" in fp.columns else pd.Series(np.nan, index=fp.index)
    fp["est_stories"] = (height / 11).round().clip(1, 80).fillna(1)
    fp["floor_area_sqft"] = fp["fp_area_sqft"] * fp["est_stories"]
    print(f"Building footprints: {len(fp):,}")

    # ---- 2. OPA ----
    print("\nReading OPA...")
    opa = normalize_columns(pd.read_csv(OPA_CSV, low_memory=False))
    opa["opa_row_id"] = np.arange(len(opa))
    opa_points = classify_opa_records(create_opa_points(opa))

    # ---- 3. OPA -> building ----
    print("\nMatching OPA records to building footprints...")
    opa_building = match_opa_to_buildings(opa_points, fp)
    unmatched = opa_points[~opa_points["opa_row_id"].isin(opa_building["opa_row_id"])]
    print(f"OPA matched: {len(opa_building):,}   unmatched: {len(unmatched):,}")
    print_match_diagnostics(opa_points, opa_building, fp)

    cross_cols = [c for c in ["opa_row_id", "parcel_number", "location", "unit", "BUILDING_ID", "match_method",
                              "match_distance_ft", "opa_residential", "opa_commercial", "opa_vacant",
                              "units_exact_record", "units_min_record", "unit_record_method"]
                  if c in opa_building.columns]
    opa_building[cross_cols].to_csv(MODEL_DIR / "opa_to_building_crosswalk.csv", index=False)
    unmatched.drop(columns="geometry").to_csv(MODEL_DIR / "unmatched_opa_points.csv", index=False)

    # ---- 4. one row per building ----
    print("Aggregating OPA accounts to buildings...")
    buildings = fp.merge(aggregate_opa_buildings(opa_building), on="BUILDING_ID", how="left")
    for c in ["opa_record_count", "has_residential", "has_commercial", "has_industrial", "has_institutional",
              "has_gq_use", "opa_vacant_flag", "residential_opa_records", "unknown_unit_records",
              "homestead_count", "condo_records"]:
        buildings[c] = buildings[c].fillna(0)
    buildings["building_use"] = classify_building_use(buildings)

    # ---- 5. vacancy ----
    print("\nReading Vacant Property Indicators...")
    vpi = normalize_columns(gpd.read_file(VACANT_GEOJSON))
    if vpi.crs is None:
        raise RuntimeError("VPI GeoJSON has no CRS.")
    vpi = vpi.to_crs(WORK_CRS)
    pts = gpd.GeoDataFrame(buildings[["BUILDING_ID"]], geometry=buildings.geometry.representative_point(),
                           crs=WORK_CRS)
    vcols = [c for c in ["build_rank", "geometry"] if c in vpi.columns]
    vj = gpd.sjoin(pts, vpi[vcols], how="left", predicate="within")
    vj["build_rank"] = numeric(vj["build_rank"]) if "build_rank" in vj else np.nan
    vs = vj.groupby("BUILDING_ID").agg(vpi_build_rank=("build_rank", "max"),
                                       vpi_match_count=("index_right", lambda x: x.notna().sum())).reset_index()
    buildings = buildings.merge(vs, on="BUILDING_ID", how="left")
    buildings["vpi_match_count"] = buildings["vpi_match_count"].fillna(0).astype(int)
    buildings["vpi_vacant"] = ((buildings["vpi_build_rank"] >= VPI_VACANCY_THRESHOLD)
                               | (buildings["vpi_match_count"] > 0)).astype(int)
    buildings["vacant_any"] = ((buildings["vpi_vacant"] == 1) | (buildings["opa_vacant_flag"] == 1)).astype(int)

    # ---- 6. tracts ----
    print("\nJoining buildings to census tracts...")
    tracts = normalize_columns(gpd.read_file(TRACT_GPKG))
    tid = first_existing(tracts, ["tract_geoid", "geoid"], required=True)
    tracts["TRACT_GEOID"] = tracts[tid].astype("string").str.replace(r"\.0$", "", regex=True).str.zfill(11)
    tracts = tracts.to_crs(WORK_CRS)
    tj = gpd.sjoin(pts, tracts[["TRACT_GEOID", "geometry"]], how="left", predicate="within") \
            .drop_duplicates("BUILDING_ID")
    miss = tj["TRACT_GEOID"].isna()
    if miss.any():   # points sitting on a tract edge or just outside the city line
        near = gpd.sjoin_nearest(tj.loc[miss, ["BUILDING_ID", "geometry"]],
                                 tracts[["TRACT_GEOID", "geometry"]], how="left").drop_duplicates("BUILDING_ID")
        tj.loc[miss, "TRACT_GEOID"] = near["TRACT_GEOID"]
        print(f"  {miss.sum():,} buildings snapped to the nearest tract")
    buildings = buildings.merge(tj[["BUILDING_ID", "TRACT_GEOID"]], on="BUILDING_ID", how="left")
    attrs = tracts.drop(columns=["geometry", tid], errors="ignore").drop_duplicates("TRACT_GEOID")
    buildings = buildings.merge(attrs, on="TRACT_GEOID", how="left")

    # ---- 7. units per building ----
    livable, exact = numeric(buildings["opa_res_livable_sqft"]), numeric(buildings["res_units_exact"])
    buildings["known_sqft_per_unit"] = np.where((exact > 0) & (livable > 0), livable / exact, np.nan)
    calib = buildings[buildings["known_sqft_per_unit"].between(200, 5000) & (buildings["has_residential"] == 1)]
    city_sqft = calib["known_sqft_per_unit"].median()
    if pd.isna(city_sqft):
        city_sqft = 1000
        print("WARNING: could not calculate citywide sqft/unit; using 1,000")
    buildings = buildings.merge(calib.groupby("TRACT_GEOID")["known_sqft_per_unit"].median()
                                .rename("tract_sqft_per_unit"), on="TRACT_GEOID", how="left")
    typical = buildings["tract_sqft_per_unit"].fillna(city_sqft)
    minimum = numeric(buildings["res_units_min"])
    proxy = np.maximum(np.maximum((livable / typical).round(), minimum.fillna(0)), 1)
    res = buildings["has_residential"] == 1
    buildings["res_units_model"] = np.select(
        [~res, exact.notna(), livable.gt(0), minimum.gt(0)], [0.0, exact, proxy, minimum], np.nan)
    buildings["unit_model_method"] = np.select(
        [~res, exact.notna(), livable.gt(0), minimum.gt(0)],
        ["non_residential", "OPA_exact_or_derived", "OPA_livable_area_proxy", "OPA_minimum_only"], "unresolved")

    buildings["population_eligible"] = res & (numeric(buildings["res_units_model"]) > 0)
    if EXCLUDE_VPI_VACANT:
        buildings["population_eligible"] &= buildings["vpi_vacant"] == 0

    # ---- 8-10. occupied units and people ----
    print("Balancing occupied owner / renter units to ACS...")
    buildings = balance_occupied_units(buildings)
    gq_ids = set()
    if GQ_BUILDINGS_CSV.exists():
        gq_ids = set(pd.read_csv(GQ_BUILDINGS_CSV, dtype=str)["BUILDING_ID"].str.strip())
        print(f"Group-quarters building list: {len(gq_ids):,} IDs")
    buildings = assign_people(buildings, gq_ids)

    # ---- 11. building-level estimates of every ACS count ----
    weight = {"pop": buildings["est_pop_total"], "hhpop": buildings["est_hh_pop"],
              "occ": buildings["occupied_units"], "own": buildings["owner_units_occ"],
              "rnt": buildings["renter_units_occ"]}
    for field, cls in ALLOCATE.items():
        if field not in buildings.columns:
            continue
        w = weight[cls]
        share = w / w.groupby(buildings["TRACT_GEOID"]).transform("sum").where(lambda s: s > 0)
        buildings[f"est_{field}"] = numeric(buildings[field]) * share.fillna(0)
    buildings["est_hh_pop"] = weight["hhpop"]      # keep the direct value, not a re-derived one

    # ---- 12. outputs ----
    print("\nWriting outputs...")
    points = buildings.copy()
    points["geometry"] = points.geometry.representative_point()
    ll = points.to_crs(4326)
    points["longitude"], points["latitude"] = ll.geometry.x, ll.geometry.y

    g = buildings.groupby("TRACT_GEOID", dropna=False)
    qa = pd.DataFrame({
        "physical_buildings": g.size(),
        "residential_buildings": g["has_residential"].sum(),
        "vpi_vacant_buildings": g["vpi_vacant"].sum(),
        "population_eligible_buildings": g["population_eligible"].sum(),
        "opa_units": g["res_units_model"].sum(),
        "modeled_occupied_units": g["occupied_units"].sum(),
        "modeled_owner_units": g["owner_units_occ"].sum(),
        "modeled_renter_units": g["renter_units_occ"].sum(),
        "modeled_household_population": g["est_hh_pop"].sum(),
        "modeled_gq_population": g["est_gq_pop"].sum(),
        "fallback_buildings": g["allocation_fallback"].apply(lambda s: int((s != "").sum())),
        "census_household_population": g["hh_pop"].first(),
        "census_occupied_units": g["hh_occ"].first(),
        "census_housing_units": g["housing"].first(),
        "census_gq_population": g["gq_pop"].first(),
        "acs_avg_hh": g["avg_hh"].first(),
    }).reset_index()
    qa["opa_units_vs_census_housing"] = safe_ratio(qa["opa_units"], qa["census_housing_units"])
    qa["people_per_occupied_unit"] = safe_ratio(qa["modeled_household_population"], qa["modeled_occupied_units"])
    qa["population_difference"] = qa["modeled_household_population"] - qa["census_household_population"]
    qa["gq_unplaced"] = qa["census_gq_population"] - qa["modeled_gq_population"]
    qa["flag"] = np.select(
        [qa["TRACT_GEOID"].isna(),
         (qa["census_household_population"] > 0) & (qa["modeled_household_population"] < 1),
         qa["opa_units_vs_census_housing"] < 0.7,
         qa["opa_units_vs_census_housing"] > 1.4,
         qa["fallback_buildings"] > 0,
         qa["gq_unplaced"] > 50],
        ["NO_TRACT", "POPULATION_LOST", "FEW_OPA_UNITS", "MANY_OPA_UNITS", "USED_FALLBACK", "GQ_NOT_PLACED"], "")

    buildings.to_file(MODEL_DIR / "philly_buildings_master.gpkg", layer="buildings_master", driver="GPKG")
    buildings[buildings["has_residential"] == 1].to_file(
        MODEL_DIR / "philly_buildings_residential.gpkg", layer="buildings_residential", driver="GPKG")
    buildings[buildings["vpi_vacant"] == 1].to_file(
        MODEL_DIR / "philly_buildings_vacant.gpkg", layer="buildings_vacant", driver="GPKG")
    points.to_file(BUILDING_POINTS_GPKG, layer="building_points", driver="GPKG")
    qa.to_csv(MODEL_DIR / "tract_model_qa.csv", index=False)
    buildings.drop(columns="geometry").to_csv(MODEL_DIR / "building_summary.csv", index=False)

    # ---- summary ----
    print("\n" + "=" * 60 + "\nBUILDING MODEL COMPLETE\n" + "=" * 60)
    print(f"Physical buildings:            {len(buildings):,}")
    print(f"Residential buildings:         {int(buildings['has_residential'].sum()):,}")
    print(f"Likely vacant (VPI):           {int(buildings['vpi_vacant'].sum()):,}")
    print(f"Modeled units:                 {buildings['res_units_model'].sum():,.0f}"
          f"   (ACS housing units {attrs['housing'].sum():,.0f})")
    print(f"Occupied units:                {buildings['occupied_units'].sum():,.0f}"
          f"   (ACS {attrs['hh_occ'].sum():,.0f})")
    print(f"Household population:          {buildings['est_hh_pop'].sum():,.0f}"
          f"   (ACS {attrs['hh_pop'].sum():,.0f})")
    print(f"Group-quarters placed:         {buildings['est_gq_pop'].sum():,.0f}"
          f"   (ACS {attrs['gq_pop'].sum():,.0f})")
    print("Unit methods:", buildings.loc[res, "unit_model_method"].value_counts().to_dict())
    print("Tract QA flags:", qa["flag"].value_counts().to_dict())
    print(f"Median people per occupied unit: {qa['people_per_occupied_unit'].median():.2f}  (should be ~2.2)")
    print(f"\nOutputs: {MODEL_DIR.resolve()}\nNext: python scripts/03_prepare_web_data.py")


if __name__ == "__main__":
    main()
