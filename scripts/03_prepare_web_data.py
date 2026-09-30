"""
03_prepare_web_data.py   (was: prepare_web_data.py)

Makes the small CSV the web app loads, so the browser never downloads the
400-600 MB model files.

Input   data/processed/building_model/philly_building_points.gpkg   (script 02)
Output  docs/data/building_weights_web.csv
        docs/data/tract_model_qa.csv

Columns in building_weights_web.csv (one row per residential or vacant building)
    tract_geoid  11-digit tract
    lon, lat     point inside the footprint
    units        modeled housing units
    occ          occupied units (households)       } these are the weights the
    own, rnt     owner / renter occupied units     } app uses to split ACS
    hh_pop       household population             } counts inside a polygon
    gq_pop       group-quarters population         }
    vacant       1 if VPI or OPA marks it vacant

Run from the repo root:
    python scripts/03_prepare_web_data.py
"""

import geopandas as gpd
import pandas as pd

from common import BUILDING_POINTS_GPKG, MODEL_DIR, WEB_DATA, publish


def num(s):
    return pd.to_numeric(s, errors="coerce").fillna(0).clip(lower=0)


def main():
    if not BUILDING_POINTS_GPKG.exists():
        raise FileNotFoundError(f"Run scripts/02_build_building_model.py first.\nMissing: {BUILDING_POINTS_GPKG}")
    print(f"Reading {BUILDING_POINTS_GPKG.name} ...")
    gdf = gpd.read_file(BUILDING_POINTS_GPKG)
    if gdf.crs is None:
        raise ValueError("The GeoPackage has no CRS.")
    gdf = gdf.to_crs(4326)
    if not gdf.geometry.geom_type.eq("Point").all():
        gdf.geometry = gdf.geometry.representative_point()

    out = pd.DataFrame({
        "tract_geoid": gdf["TRACT_GEOID"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(11),
        "lon": gdf.geometry.x.round(6),
        "lat": gdf.geometry.y.round(6),
        "units": num(gdf["res_units_model"]).round(0),
        "occ": num(gdf["occupied_units"]).round(3),
        "own": num(gdf["owner_units_occ"]).round(3),
        "rnt": num(gdf["renter_units_occ"]).round(3),
        "hh_pop": num(gdf["est_hh_pop"]).round(3),
        "gq_pop": num(gdf["est_gq_pop"]).round(3),
        "vacant": (num(gdf["vacant_any"]) > 0).astype("int8"),
    })
    keep = (out[["units", "occ", "hh_pop", "gq_pop"]].sum(axis=1) > 0) | (out["vacant"] == 1)
    out = out[keep & out["lon"].between(-76, -74) & out["lat"].between(39, 41)
              & out["tract_geoid"].str.fullmatch(r"\d{11}")]

    dest = WEB_DATA / "building_weights_web.csv"
    out.to_csv(dest, index=False)
    print(f"Saved docs/data/{dest.name}: {len(out):,} buildings, {dest.stat().st_size / 1024**2:,.1f} MB")
    print(f"  household population {out['hh_pop'].sum():,.0f}   households {out['occ'].sum():,.0f}"
          f"   vacant buildings {int(out['vacant'].sum()):,}")
    if dest.stat().st_size > 95 * 1024**2:
        print("  WARNING: GitHub rejects files over 100 MB. Round coordinates to 5 decimals or split by district.")

    qa = MODEL_DIR / "tract_model_qa.csv"
    if qa.exists():
        publish(qa)


if __name__ == "__main__":
    main()
