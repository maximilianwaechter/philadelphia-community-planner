# Philadelphia Community Area Planner

A map app for the community development office to compare possible outreach
areas in Philadelphia. Draw or pick a polygon and it reports modeled
demographics (population, age, race, income, housing, commuting, education)
built from ACS census data and placed onto real buildings.

Live site: GitHub Pages serves the `docs/` folder.

---

## How the numbers are made

```
ACS tracts (census)  ─┐
OPA properties       ─┼─►  building model  ─►  building_weights_web.csv  ─►  web app
Building footprints  ─┤    (script 02)          (script 03)                 (docs/)
Vacancy indicators   ─┘
```

1. **Census is the master data.** Every tract's households, owners, renters
   and people come from the ACS 5-year estimates.
2. **Footprints say where people live.** OPA records are matched to building
   footprints; OPA says how many units each building has, which look
   owner-occupied (homestead exemption) and which are vacant.
3. **Each tract is balanced back to the census.** Occupied owner and renter
   units are scaled so they add up to the tract's ACS numbers, vacant buildings
   get zero, and people = owner units × owner household size + renter units ×
   renter household size. Dorms and nursing homes get the group-quarters
   population.
4. **In the app**, a polygon's share of each tract is taken from the buildings
   inside it: people counts by population, household counts by occupied units,
   owner and renter counts by owner and renter units.

This is a model, not address-level census data. `docs/data/tract_model_qa.csv`
flags tracts where it is weakest.

---

## Folder layout

```
philadelphia-community-planner/
├── README.md
├── requirements.txt          Python packages
├── .env.example              copy to .env and fill in (never commit .env)
├── .gitignore
├── scripts/                  run in order, from the repo root
│   ├── common.py             shared folder paths
│   ├── 01_download_acs_tracts.py      (was philly_acs_tracts.py)
│   ├── 02_build_building_model.py     (was philly_building.py)
│   ├── 03_prepare_web_data.py         (was prepare_web_data.py)
│   ├── 04_download_reference_layers.py  police districts, ZIPs, wards, divisions
│   └── 05_outreach_heatmap.py           privacy-safe outreach heat map
├── data/                     NOT in git (big or private)
│   ├── raw/                  downloads you put here yourself
│   │   └── outreach/         member exports (private!)
│   └── processed/            everything the scripts create
│       ├── acs/
│       ├── building_model/
│       └── outreach/
└── docs/                     the website (GitHub Pages)
    ├── index.html
    └── data/                 small files the site loads
```

---

## First-time setup

```bash
git clone https://github.com/maximilianwaechter/philadelphia-community-planner.git
cd philadelphia-community-planner
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # Windows   (macOS/Linux: cp .env.example .env)
```

Edit `.env`:

- `CENSUS_API_KEY`: free at <https://api.census.gov/data/key_signup.html>
- `PLANNER_DATA_DIR`: optional. Point it at the S: drive folder if you keep the
  big files there. That folder needs `raw/` and `processed/` inside it.

## Get the raw data (into `data/raw/`)

All from [OpenDataPhilly](https://opendataphilly.org):

| File name to save as | Dataset |
|---|---|
| `OPA_PROPERTIES_PUBLIC.csv` | Property Assessments (full CSV, ~580,000 rows) |
| `LI_BUILDING_FOOTPRINTS.geojson` | Building Footprints |
| `Vacant_Indicators_Bldg.geojson` | Vacant Property Indicators – Buildings |

Optional:

- `group_quarters_buildings.csv` with one column, `BUILDING_ID` (the footprint
  BIN), listing dorms and nursing homes the OPA descriptions miss. Temple
  residence halls are the big one. Without this, student population near
  campus gets spread over row houses.
- `outreach/gateway_clients.csv`: the member export (`ID, Status, Address`).

## Run the pipeline

```bash
python scripts/01_download_acs_tracts.py        # ~1 min
python scripts/02_build_building_model.py       # 10-30 min
python scripts/03_prepare_web_data.py
python scripts/04_download_reference_layers.py
python scripts/05_outreach_heatmap.py           # only when the member list changes
```

Script 02 prints an **OPA / footprint diagnostics** block. Check it the first
time. The earlier model placed only ~85,000 units for ~680,000 households, and
these numbers show where records get lost:

- *OPA records read* should be about 580,000. Far fewer means a partial export.
- *Footprints with residential* should be in the hundreds of thousands.
- *Median people per occupied unit* at the end should be about 2.2.

Preview the site locally:

```bash
python -m http.server 8000 --directory docs
# open http://localhost:8000
```

---

## Updating GitHub (step by step)

These steps move the existing repo (index.html and `data/` at the top level)
to the new layout without losing history.

1. **Get a clean local copy and a branch**
   ```bash
   git clone https://github.com/maximilianwaechter/philadelphia-community-planner.git
   cd philadelphia-community-planner
   git checkout -b restructure
   ```

2. **Move the website into `docs/`** (`git mv` keeps each file's history)
   ```bash
   mkdir docs
   git mv index.html docs/index.html
   git mv data docs/data
   ```

3. **Copy in the new files** from this package, overwriting when asked:
   `README.md`, `.gitignore`, `.env.example`, `requirements.txt`, `scripts/`,
   `docs/index.html`, `docs/data/Polygons_of_interest.geojson`.

4. **Remove the old scripts and anything that shouldn't be public**
   ```bash
   git rm --cached philly_acs_tracts.py philly_building.py prepare_web_data.py   # if they were committed
   git rm --cached docs/data/opa_to_building_crosswalk.csv docs/data/unmatched_opa_points.csv   # if present; these go to Releases
   git status          # nothing under data/raw, data/processed or .env should be listed
   ```

   The old `philly_acs_tracts.py` had the Census API key typed into it. If that
   file was ever pushed, request a new key and put it in `.env` only.

5. **Rebuild the data and add the web files**
   ```bash
   python scripts/01_download_acs_tracts.py
   python scripts/02_build_building_model.py
   python scripts/03_prepare_web_data.py
   python scripts/04_download_reference_layers.py
   git add docs/data
   ```

6. **Commit and push**
   ```bash
   git add -A
   git commit -m "Restructure repo; census-balanced building model; planner updates"
   git push -u origin restructure
   ```
   Open the pull request on GitHub, check the file list, then merge into `main`.

7. **Point GitHub Pages at `docs/`**
   Repo **Settings → Pages → Build and deployment → Deploy from a branch →
   `main` / `/docs` → Save.** The site address stays the same.

8. **Put the big model files in a Release**
   Repo **Releases → Draft a new release**, tag it (e.g. `model-2026-10`), and
   attach `building_summary.csv`, `opa_to_building_crosswalk.csv` and the
   GeoPackages from `data/processed/building_model/`. The app links there.

### Routine updates later

```bash
git pull
# ...edit, or re-run scripts...
git add -A
git commit -m "Describe the change"
git push
```

---

## Outreach member data (privacy)

The member file has people's home addresses. It stays in `data/raw/outreach/`,
which git ignores. Script 05 publishes only:

- `docs/data/outreach_heat.csv`: member counts in ~250 m grid cells, with
  cells of fewer than 3 people removed
- `docs/data/outreach_by_zip.csv`: member counts per ZIP code

Anyone with the site link can see these. Confirm that level of detail is okay
with the program before publishing, or keep the repo private. GitHub Pages on
a private repo needs a paid GitHub plan.

---

## App features

- Draw, edit, cut, drag and delete planning polygons; **Undo / Redo** (buttons,
  map control, Ctrl+Z / Ctrl+Y). While drawing, Ctrl+Z removes the last corner.
- The map pans when the cursor reaches its edge while drawing, or while
  dragging a corner.
- Reference outlines: council districts, police districts, political wards,
  voting divisions, ZIP codes, and Temple's 8 priority ZIPs (19121, 19122,
  19123, 19125, 19130, 19132, 19133, 19140). Click any of them for demographics.
- **Style** any layer or polygon: color wheel, hex, brightness, and
  transparency sliders. Polygon styles save into the plan GeoJSON; layer
  styles are remembered in the browser and saved with the plan.
- Polygon labels use each polygon's own color.
- **Save changes** writes only new or edited polygons (plus a list of removed
  base polygons). **Open plan** puts those changes back on top of the base
  polygons.
- **Copy** duplicates a polygon so you can edit a variation.
- Police stations: hover for the district, click for address and phone.
- Outreach member heat map with a status filter (All, Active, Disengaged, and
  so on).
