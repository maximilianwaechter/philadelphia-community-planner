"""
01_download_acs_tracts.py   (was: philly_acs_tracts.py)

Philadelphia census tracts + ACS 5-year estimates. This is the master data:
every building-level number later is scaled so each tract adds back up to
these values.

Outputs
    data/processed/acs/philly_tracts_acs2024.gpkg
    data/processed/acs/philly_tracts_acs2024.csv   (also copied to docs/data/)
    data/processed/acs/field_dictionary.csv

Set your Census API key in .env (never commit it):
    CENSUS_API_KEY=your_key_here

Run from the repo root:
    python scripts/01_download_acs_tracts.py
"""

import os

import geopandas as gpd
import pandas as pd
import requests

from common import ACS_DIR, ACS_YEAR, TRACT_CSV, TRACT_GPKG, publish

YEAR = ACS_YEAR
STATE = "42"
COUNTY = "101"
API_KEY = os.environ.get("CENSUS_API_KEY", "")
OUT_CRS = 2272
HEADERS = {"User-Agent": "Mozilla/5.0 (philly-acs-download)"}

# ---------------------------------------------------------------------------
# ACS variables: field name -> (ACS code, description)
# ---------------------------------------------------------------------------
VARIABLES = {
    # population and housing
    "pop_total": ("B01003_001E", "Total population"),
    "housing": ("B25001_001E", "Total housing units"),
    "hh_occ": ("B25003_001E", "Occupied housing units / households"),
    "owner_occ": ("B25003_002E", "Owner-occupied housing units"),
    "renter_occ": ("B25003_003E", "Renter-occupied housing units"),
    "hh_pop": ("B25008_001E", "Population in occupied housing units"),
    "own_hhpop": ("B25008_002E", "Population in owner-occupied housing units"),
    "rnt_hhpop": ("B25008_003E", "Population in renter-occupied housing units"),
    "avg_hh": ("B25010_001E", "Average household size"),
    "avg_own_hh": ("B25010_002E", "Average household size - owner occupied"),
    "avg_rnt_hh": ("B25010_003E", "Average household size - renter occupied"),
    # income and poverty
    "med_hh_inc": ("B19013_001E", "Median household income (dollars)"),
    "pov_univ": ("B17001_001E", "Population for whom poverty status is determined"),
    "pov_below": ("B17001_002E", "Population below poverty level"),
    # race and ethnicity
    "race_total": ("B03002_001E", "Total population - race/ethnicity universe"),
    "white_nh": ("B03002_003E", "White alone, not Hispanic or Latino"),
    "black_nh": ("B03002_004E", "Black or African American alone, not Hispanic or Latino"),
    "asian_nh": ("B03002_006E", "Asian alone, not Hispanic or Latino"),
    "hispanic": ("B03002_012E", "Hispanic or Latino - any race"),
    # commuting and vehicles
    "workers": ("B08301_001E", "Workers 16 years and over"),
    "drv_alone": ("B08301_003E", "Commute - drove alone"),
    "pub_trans": ("B08301_010E", "Commute - public transportation"),
    "bicycle": ("B08301_018E", "Commute - bicycle"),
    "walked": ("B08301_019E", "Commute - walked"),
    "wfh": ("B08301_021E", "Worked from home"),
    "veh_hh": ("B08201_001E", "Total households - vehicle availability universe"),
    "no_vehicle": ("B08201_002E", "Households with no vehicle available"),
    # school and education
    "sch_univ": ("B14001_001E", "Population 3 years and over - school enrollment universe"),
    "pop25plus": ("B15003_001E", "Population 25 years and over"),
    "hs_grad": ("B15003_017E", "Regular high school diploma"),
    "bachelors": ("B15003_022E", "Bachelor's degree"),
    "masters": ("B15003_023E", "Master's degree"),
    "prof_deg": ("B15003_024E", "Professional school degree"),
    "doctorate": ("B15003_025E", "Doctorate degree"),
    # employment
    "labor_frc": ("B23025_002E", "In labor force, 16 years and over"),
    "employed": ("B23025_004E", "Civilian labor force - employed"),
    "unemployed": ("B23025_005E", "Civilian labor force - unemployed"),
    # health, disability, language, internet universes
    "hins_univ": ("B27001_001E", "Civilian noninstitutionalized population - health insurance universe"),
    "dis_univ": ("C18108_001E", "Civilian noninstitutionalized population - disability universe"),
    "lang_univ": ("C16001_001E", "Population 5 years and over - language universe"),
    "inet_univ": ("B28002_001E", "Total households - internet access universe"),
    # housing cost
    "med_rent": ("B25064_001E", "Median gross rent"),
    "med_value": ("B25077_001E", "Median owner-occupied home value"),
    # age under 18
    "m_under5": ("B01001_003E", "Male under 5"), "m_5_9": ("B01001_004E", "Male 5 to 9"),
    "m_10_14": ("B01001_005E", "Male 10 to 14"), "m_15_17": ("B01001_006E", "Male 15 to 17"),
    "f_under5": ("B01001_027E", "Female under 5"), "f_5_9": ("B01001_028E", "Female 5 to 9"),
    "f_10_14": ("B01001_029E", "Female 10 to 14"), "f_15_17": ("B01001_030E", "Female 15 to 17"),
    # age 65+
    "m_65_66": ("B01001_020E", "Male 65 and 66"), "m_67_69": ("B01001_021E", "Male 67 to 69"),
    "m_70_74": ("B01001_022E", "Male 70 to 74"), "m_75_79": ("B01001_023E", "Male 75 to 79"),
    "m_80_84": ("B01001_024E", "Male 80 to 84"), "m_85plus": ("B01001_025E", "Male 85 and over"),
    "f_65_66": ("B01001_044E", "Female 65 and 66"), "f_67_69": ("B01001_045E", "Female 67 to 69"),
    "f_70_74": ("B01001_046E", "Female 70 to 74"), "f_75_79": ("B01001_047E", "Female 75 to 79"),
    "f_80_84": ("B01001_048E", "Female 80 to 84"), "f_85plus": ("B01001_049E", "Female 85 and over"),
}

UNDER18 = ["m_under5", "m_5_9", "m_10_14", "m_15_17", "f_under5", "f_5_9", "f_10_14", "f_15_17"]
AGE65 = ["m_65_66", "m_67_69", "m_70_74", "m_75_79", "m_80_84", "m_85plus",
         "f_65_66", "f_67_69", "f_70_74", "f_75_79", "f_80_84", "f_85plus"]

DERIVED_SUMS = {
    "k12_enr": (["B14001_004E", "B14001_005E", "B14001_006E", "B14001_007E"],
                "Enrolled in kindergarten through grade 12"),
    "under18": ([VARIABLES[f][0] for f in UNDER18], "Population under 18"),
    "age65plus": ([VARIABLES[f][0] for f in AGE65], "Population age 65 and over"),
}

TRACT_URLS = [
    f"https://www2.census.gov/geo/tiger/GENZ{YEAR}/shp/cb_{YEAR}_{STATE}_tract_500k.zip",
    f"https://www2.census.gov/geo/tiger/TIGER{YEAR}/TRACT/tl_{YEAR}_{STATE}_tract.zip",
]


def safe_ratio(num, den):
    return num / den.where(den != 0)


def fetch_acs(codes):
    url = f"https://api.census.gov/data/{YEAR}/acs/acs5"
    merged = None
    for i in range(0, len(codes), 45):
        chunk = codes[i:i + 45]
        params = [("get", ",".join(["NAME"] + chunk)), ("for", "tract:*"),
                  ("in", f"state:{STATE}"), ("in", f"county:{COUNTY}")]
        if API_KEY:
            params.append(("key", API_KEY))
        print(f"Downloading ACS variables {i + 1}-{min(i + 45, len(codes))} of {len(codes)}...")
        r = requests.get(url, params=params, headers=HEADERS, timeout=120)
        if not r.ok:
            raise RuntimeError(f"Census API error {r.status_code}: {r.text[:1000]}")
        rows = r.json()
        df = pd.DataFrame(rows[1:], columns=rows[0])
        df["TRACT_GEOID"] = df["state"] + df["county"] + df["tract"]
        df = df.drop(columns=["state", "county", "tract"])
        merged = df if merged is None else merged.merge(
            df.drop(columns="NAME"), on="TRACT_GEOID", how="outer", validate="one_to_one")
    for code in codes:
        merged[code] = pd.to_numeric(merged[code], errors="coerce")
        merged[code] = merged[code].where(merged[code] >= 0)   # Census uses negatives for "missing"
    return merged


def load_tracts():
    for url in TRACT_URLS:
        dest = ACS_DIR / url.rsplit("/", 1)[-1]
        if not dest.exists():
            print(f"Downloading tract boundaries: {dest.name}")
            r = requests.get(url, headers=HEADERS, timeout=300)
            if not r.ok:
                print(f"Could not download {url}; trying fallback.")
                continue
            dest.write_bytes(r.content)
        print(f"Using tract boundaries from {dest.name}")
        tracts = gpd.read_file(dest)
        philly = tracts.loc[tracts["COUNTYFP"] == COUNTY, ["GEOID", "geometry"]].copy()
        return philly.rename(columns={"GEOID": "TRACT_GEOID"})
    raise RuntimeError("Could not download tract boundaries.")


def main():
    if not API_KEY:
        print("No CENSUS_API_KEY in .env - running without a key (slower, rate-limited).")

    codes = [code for code, _ in VARIABLES.values()]
    for parts, _ in DERIVED_SUMS.values():
        codes.extend(parts)
    codes = list(dict.fromkeys(codes))
    acs = fetch_acs(codes)

    for field, (parts, _) in DERIVED_SUMS.items():
        acs[field] = acs[parts].sum(axis=1, min_count=len(parts))

    acs["vac_units"] = acs["B25001_001E"] - acs["B25003_001E"]
    acs["occ_rate"] = safe_ratio(acs["B25003_001E"], acs["B25001_001E"])
    acs["vac_rate"] = safe_ratio(acs["vac_units"], acs["B25001_001E"])
    acs["owner_pct"] = safe_ratio(acs["B25003_002E"], acs["B25003_001E"])
    acs["renter_pct"] = safe_ratio(acs["B25003_003E"], acs["B25003_001E"])
    acs["gq_pop"] = acs["B01003_001E"] - acs["B25008_001E"]      # dorms, nursing homes, prisons...
    acs["gq_pct"] = safe_ratio(acs["gq_pop"], acs["B01003_001E"])
    acs["pov_rate"] = safe_ratio(acs["B17001_002E"], acs["B17001_001E"])
    acs["no_veh_pct"] = safe_ratio(acs["B08201_002E"], acs["B08201_001E"])

    direct = {code for code, _ in VARIABLES.values()}
    helpers = {c for parts, _ in DERIVED_SUMS.values() for c in parts} - direct
    acs = acs.drop(columns=list(helpers), errors="ignore")
    acs = acs.rename(columns={code: field for field, (code, _) in VARIABLES.items()})

    gdf = load_tracts().merge(acs, on="TRACT_GEOID", how="left", validate="one_to_one").to_crs(OUT_CRS)

    print(f"\nPhiladelphia tracts: {len(gdf):,}")
    for label, col in [("Total population", "pop_total"), ("Household population", "hh_pop"),
                       ("Group-quarters population", "gq_pop"), ("Housing units", "housing"),
                       ("Occupied units", "hh_occ")]:
        print(f"{label}: {gdf[col].sum():,.0f}")

    gdf.to_file(TRACT_GPKG, layer=TRACT_GPKG.stem, driver="GPKG")
    gdf.drop(columns="geometry").to_csv(TRACT_CSV, index=False)

    dictionary = [(f, code, desc, "TRACT") for f, (code, desc) in VARIABLES.items()]
    dictionary += [(f, " + ".join(p), d, "TRACT_DERIVED") for f, (p, d) in DERIVED_SUMS.items()]
    dictionary += [
        ("vac_units", "housing - hh_occ", "Vacant housing units", "TRACT_DERIVED"),
        ("occ_rate", "hh_occ / housing", "Housing occupancy rate", "TRACT_DERIVED"),
        ("vac_rate", "vac_units / housing", "Housing vacancy rate", "TRACT_DERIVED"),
        ("owner_pct", "owner_occ / hh_occ", "Owner-occupied share", "TRACT_DERIVED"),
        ("renter_pct", "renter_occ / hh_occ", "Renter-occupied share", "TRACT_DERIVED"),
        ("gq_pop", "pop_total - hh_pop", "Population living outside ordinary housing units", "TRACT_DERIVED"),
        ("gq_pct", "gq_pop / pop_total", "Group-quarters population share", "TRACT_DERIVED"),
        ("pov_rate", "pov_below / pov_univ", "Poverty rate", "TRACT_DERIVED"),
        ("no_veh_pct", "no_vehicle / veh_hh", "Households without a vehicle share", "TRACT_DERIVED"),
    ]
    pd.DataFrame(dictionary, columns=["field", "acs_variable", "description", "source_geography"]) \
        .to_csv(ACS_DIR / "field_dictionary.csv", index=False)

    publish(TRACT_CSV)
    print(f"\nDone. Files written to {ACS_DIR.resolve()}")


if __name__ == "__main__":
    main()
