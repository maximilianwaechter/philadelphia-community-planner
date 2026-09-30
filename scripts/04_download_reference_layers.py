"""
04_download_reference_layers.py

Downloads boundary layers from the City of Philadelphia's public ArcGIS
services and saves them as GeoJSON in docs/data/ for the web app:

    police_districts.geojson     Police districts (dist_numc)
    zip_codes.geojson            ZIP codes (code) + Temple priority flag
    political_wards.geojson      66 political wards (ward_num)
    political_divisions.geojson  ~1,700 voting divisions (division_num)

The app also falls back to loading these live from the City if the files are
missing, but saving them keeps the app fast and working if a service moves.

Run from the repo root:
    python scripts/04_download_reference_layers.py
"""

import json

import requests

from common import WEB_DATA

BASE = "https://services.arcgis.com/fLeGjb7u4uXqeF9q/arcgis/rest/services"
LAYERS = {
    "police_districts.geojson": ("Boundaries_District", "dist_numc"),
    "zip_codes.geojson": ("Zipcodes_Poly", "code"),
    "political_wards.geojson": ("Political_Wards", "ward_num"),
    "political_divisions.geojson": ("Political_Divisions", "division_num"),
}
TEMPLE_PRIORITY_ZIPS = {"19121", "19122", "19123", "19125", "19130", "19132", "19133", "19140"}
PAGE = 50   # some City layers cap requests at 50 records


def round_coords(c, nd=6):
    return round(c, nd) if isinstance(c, float) else [round_coords(x, nd) for x in c]


def fetch(service, field):
    features, offset = [], 0
    while True:
        r = requests.get(f"{BASE}/{service}/FeatureServer/0/query", timeout=120, params={
            "where": "1=1", "outFields": field, "outSR": 4326, "f": "geojson",
            "resultOffset": offset, "resultRecordCount": PAGE, "orderByFields": "objectid"})
        r.raise_for_status()
        batch = r.json().get("features", [])
        features += batch
        if len(batch) < PAGE:
            return features
        offset += PAGE


def main():
    for filename, (service, field) in LAYERS.items():
        print(f"Downloading {service} ...", end=" ")
        feats = fetch(service, field)
        for f in feats:
            f["geometry"]["coordinates"] = round_coords(f["geometry"]["coordinates"])
            if service == "Zipcodes_Poly":
                f["properties"]["temple_priority"] = str(f["properties"].get("code")) in TEMPLE_PRIORITY_ZIPS
        out = WEB_DATA / filename
        out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
        print(f"{len(feats):,} features -> docs/data/{filename} ({out.stat().st_size / 1024:,.0f} KB)")


if __name__ == "__main__":
    main()
