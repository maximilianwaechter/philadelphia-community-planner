# Philadelphia Community Area Planner

Static Leaflet/Turf web application intended for GitHub Pages.

## Repository layout

```text
/
├── index.html
└── data/
    ├── City_Limits.geojson
    ├── Council_districts_2024.geojson
    ├── Schools_of_Interest.geojson
    ├── TU_Police_Stations.geojson
    ├── Polygons_of_interest.geojson
    ├── TU_police_boundary.geojson
    ├── TU_buildings.geojson
    ├── Police_Stations.geojson
    └── philly_building_points.geojson
```

The first eight GeoJSON files are project reference/base layers. `Polygons_of_interest.geojson` is loaded as the starting editable plan. The building-points file is the hidden analysis layer created from OPA + building footprints + vacancy + ACS preprocessing.

## What the app does

- Loads all base layers automatically from `data/`.
- Uses a light street basemap, with aerial imagery available from the map control.
- Lets users toggle reference layers.
- Lets users click Council Districts, City Limits, or the TU police boundary for an immediate demographic summary.
- Loads `Polygons_of_interest.geojson` as editable planning areas on first use.
- Lets users draw, rename, edit, remove, and import more planning polygons.
- Autosaves the current plan in that browser's localStorage.
- **Save plan** downloads a GeoJSON containing the current plan geometries and current summary values.
- **Open plan** reloads a previously saved plan.
- **Compare** loads a saved plan and recalculates both plans against the same current demographic model.

## Demographic methodology

The browser does not area-weight Census tracts. It sums modeled values stored on residential building points inside each selected polygon. Those points should be produced by the preprocessing workflow that:

1. joins OPA properties to physical building footprints;
2. flags likely vacancy;
3. estimates residential-unit capacity;
4. assigns buildings to Census tracts; and
5. distributes additive ACS tract counts to residential buildings using residential-unit weights.

The app therefore assumes demographic groups are distributed within a tract in the same proportions as modeled residential capacity. Median income, median rent, and median home value are displayed as household-weighted approximations.

## Publish with GitHub Pages

1. Create a GitHub repository.
2. Put `index.html` at the repository root.
3. Put the GeoJSON files in `data/` with the exact filenames above.
4. Commit and push the files.
5. In the repository, open **Settings → Pages**.
6. Choose **Deploy from a branch**, then select the branch containing the site and `/ (root)`.
7. Open the Pages URL GitHub provides.

Do not test the application by double-clicking `index.html` if the browser blocks local `fetch()` requests. Test through GitHub Pages or a local HTTP server.
