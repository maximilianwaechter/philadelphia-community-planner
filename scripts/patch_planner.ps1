# patch_planner.ps1
# Changes docs/index.html so the planner opens with:
#   - only TU Buildings, the TU Police Boundary and the planning polygons switched on
#   - the map starting on the Temple area (default Light gray basemap, no API key needed)
#   - crime, shootings and stops from January 2014 onward only
# Every edit must match exactly once or nothing is written. A backup is saved as index.html.bak.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File .\scripts\patch_planner.ps1

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$path = Join-Path (Join-Path $repo 'docs') 'index.html'
if (-not (Test-Path $path)) { throw "Can't find $path" }

$text = [IO.File]::ReadAllText($path)
$crlf = $text.Contains("`r`n")
$text = $text.Replace("`r`n", "`n")

$carto = "  'Light (easy to read)': L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', { maxZoom: 20, subdomains: 'abcd', attribution: '&copy; <a href=`"https://www.openstreetmap.org/copyright`">OpenStreetMap</a> contributors &copy; <a href=`"https://carto.com/attributions`">CARTO</a>' }),`n"
if ($text.Contains('const START_VISIBLE')) {
  # Patched by the earlier version: take the CARTO basemap back out, keep everything else.
  if (-not $text.Contains($carto)) { Write-Host 'index.html is already patched and has no CARTO basemap. Nothing to do.' -ForegroundColor Yellow; return }
  $text = $text.Replace($carto, '').Replace("basemaps['Light (easy to read)'].addTo(map);", "basemaps['Light gray'].addTo(map);")
  Copy-Item $path "$path.bak" -Force
  if ($crlf) { $text = $text.Replace("`n", "`r`n") }
  [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false))
  Write-Host 'Removed the CARTO basemap; Light gray is the default again. Previous file saved as docs\index.html.bak' -ForegroundColor Green
  return
}

function Swap([string]$old, [string]$new, [string]$what) {
  $old = $old.Replace("`r`n", "`n"); $new = $new.Replace("`r`n", "`n")
  $n = ([regex]::Matches($script:text, [regex]::Escape($old))).Count
  if ($n -ne 1) { throw "[$what] expected the original text once in index.html, found it $n time(s). Nothing was changed." }
  $script:text = $script:text.Replace($old, $new)
  Write-Host "  ok  $what"
}

Write-Host "Patching $path"

# 1. Layers on at start-up: TU buildings and TU police boundary (planning polygons are always drawn).
Swap @'
// ACS count fields and the building weight used to split them inside a polygon.
'@ @'
// Layers switched on when the app opens. Everything else starts off; turn it on in the Layers tab.
// Planning polygons (the Plan tab) are always drawn.
const START_VISIBLE = new Set(['tuBuildings', 'police']);
SLOTS.forEach(s => { s.visible = START_VISIBLE.has(s.key); });

// ACS count fields and the building weight used to split them inside a polygon.
'@ 'start-up layers'

# 3. Start the view on the TU police boundary with room around it.
Swap @'
  if (state.plan.areas.length) fitTo(state.plan.areas.map(a => a.layer));
  else if (state.layers.city && state.layers.city.leaflet) fitTo([state.layers.city.leaflet]);
'@ @'
  // Start on Temple: the TU police boundary with some room around it.
  const tuArea = state.layers.police && state.layers.police.leaflet;
  if (tuArea && tuArea.getBounds().isValid()) map.fitBounds(tuArea.getBounds().pad(0.35), { padding: [18, 18] });
  else if (state.plan.areas.length) fitTo(state.plan.areas.map(a => a.layer));
  else if (state.layers.city && state.layers.city.leaflet) fitTo([state.layers.city.leaflet]);
'@ 'start-up view'

# 4. Crime from 2014 on. Month numbers in areas.json and seg_YYYY.json are positions in
#    meta.months, so the dropped months are subtracted wherever those files are read.
Swap @'
const CRIME_DIR = 'data/crime/';
'@ @'
const CRIME_DIR = 'data/crime/';
const CRIME_START = '2014-01';     // months before this are dropped when the crime data loads
'@ 'crime start constant'

Swap @'
    crime.meta = meta; crime.months = meta.months; crime.offs = meta.offenses;
'@ @'
    // Keep CRIME_START onward only. Remember how many months were dropped (monthOffset).
    const keepFrom = meta.months.findIndex(m => m >= CRIME_START);
    if (keepFrom < 0) throw new Error(`no crime data from ${CRIME_START} on`);
    crime.monthOffset = keepFrom;
    const startYear = Number(CRIME_START.slice(0, 4));
    meta.months = meta.months.slice(keepFrom);
    meta.years = (meta.years || []).filter(y => Number(y) >= startYear);
    for (const c of Object.values(meta.coverage || {})) if (c && c.first < CRIME_START) c.first = CRIME_START;
    if (meta.qa) for (const y of Object.keys(meta.qa)) if (Number(y) < startYear) delete meta.qa[y];
    crime.meta = meta; crime.months = meta.months; crime.offs = meta.offenses;
'@ 'crime month filter'

Swap @'
  if (flat) for (let i = 0; i < flat.length; i += 3) mat[flat[i + 1] * nm + flat[i]] += flat[i + 2];
'@ @'
  if (flat) for (let i = 0; i < flat.length; i += 3) {
    const m = flat[i] - crime.monthOffset;
    if (m >= 0 && m < nm) mat[flat[i + 1] * nm + m] += flat[i + 2];
  }
'@ 'area counts offset'

Swap @'
    const m = rows[i + 1];
'@ @'
    const m = rows[i + 1] - crime.monthOffset;
'@ 'segment totals offset'

Swap @'
    for (let i = 0; i < rows.length; i += 4) if (flag[rows[i]]) mat[rows[i + 2] * nm + rows[i + 1]] += rows[i + 3];
'@ @'
    for (let i = 0; i < rows.length; i += 4) {
      const m = rows[i + 1] - crime.monthOffset;
      if (flag[rows[i]] && m >= 0 && m < nm) mat[rows[i + 2] * nm + m] += rows[i + 3];
    }
'@ 'drawn-area history offset'

Copy-Item $path "$path.bak" -Force
if ($crlf) { $text = $text.Replace("`n", "`r`n") }
[IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false))
Write-Host "Done. Original saved as docs\index.html.bak" -ForegroundColor Green
