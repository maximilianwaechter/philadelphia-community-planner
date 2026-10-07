<#
.SYNOPSIS
  Pulls the latest Philadelphia crime data from the City's public Carto SQL API
  into docs\data\ for the Community Area Planner.

.DESCRIPTION
  Pattern borrowed from nickhand/philly-gun-violence-dashboard:
    extract -> validate -> write to temp -> swap into place -> write metadata.
  A failed or empty pull never overwrites the last good file.

  Datasets (OpenDataPhilly, served from phl.carto.com):
    incidents_part1_part2  -> crime_incidents.csv  (violent flag = UCR 100/200/300/400/800)
    shootings              -> shootings.csv
    car_ped_stops          -> traffic_stops.csv

.EXAMPLE
  .\scripts\update-data.ps1                 # last 12 months, all datasets
  .\scripts\update-data.ps1 -Months 24
  .\scripts\update-data.ps1 -Datasets incidents,shootings
#>
[CmdletBinding()]
param(
    [int]$Months = 12,
    [ValidateSet('incidents', 'shootings', 'stops')]
    [string[]]$Datasets = @('incidents', 'shootings', 'stops'),
    [string]$OutDir = (Join-Path $PSScriptRoot '..\docs\data')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # makes Invoke-WebRequest much faster on Windows PowerShell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$CartoUrl = 'https://phl.carto.com/api/v2/sql'
$OutDir = [IO.Path]::GetFullPath($OutDir)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$end   = (Get-Date).Date.AddDays(1)
$start = $end.AddMonths(-$Months)

# One entry per dataset. Coordinates come from the_geom so we don't depend on
# lat/lng column names, which differ between tables.
$specs = @{
    incidents = @{
        Table    = 'incidents_part1_part2'
        DateCol  = 'dispatch_date'
        File     = 'crime_incidents.csv'
        Select   = @"
dc_key, dispatch_date, dispatch_time, ucr_general, text_general_code, dc_dist, psa,
CASE WHEN ucr_general::int IN (100,200,300,400,800) THEN 'violent' ELSE 'nonviolent' END AS crime_class,
ROUND(ST_Y(the_geom)::numeric, 6) AS lat, ROUND(ST_X(the_geom)::numeric, 6) AS lon
"@
        Required = @('dc_key', 'ucr_general', 'crime_class', 'lat', 'lon')
    }
    shootings = @{
        Table    = 'shootings'
        DateCol  = 'date_'
        File     = 'shootings.csv'
        Select   = @"
dc_key, date_, time, fatal, officer_involved, dist,
ROUND(ST_Y(the_geom)::numeric, 6) AS lat, ROUND(ST_X(the_geom)::numeric, 6) AS lon
"@
        Required = @('dc_key', 'fatal', 'lat', 'lon')
    }
    stops = @{
        Table    = 'car_ped_stops'
        DateCol  = 'datetimeoccur'
        File     = 'traffic_stops.csv'
        Select   = @"
datetimeoccur, stoptype, districtoccur, psa, individual_frisked, individual_searched, individual_arrested,
ROUND(ST_Y(the_geom)::numeric, 6) AS lat, ROUND(ST_X(the_geom)::numeric, 6) AS lon
"@
        Required = @('datetimeoccur', 'stoptype', 'lat', 'lon')
    }
}

function Invoke-Carto([string]$sql) {
    # POST avoids URL-length limits; retry a few times because Carto drops big requests occasionally.
    for ($try = 1; $try -le 4; $try++) {
        try {
            $resp = Invoke-WebRequest -Uri $CartoUrl -Method Post -UseBasicParsing -TimeoutSec 300 `
                -Body @{ q = $sql; format = 'csv' }
            return $resp.Content
        } catch {
            if ($try -eq 4) { throw }
            $wait = [math]::Pow(2, $try)
            Write-Warning "  Carto request failed ($($_.Exception.Message)); retrying in $wait s"
            Start-Sleep -Seconds $wait
        }
    }
}

$meta = [ordered]@{
    updated_at = (Get-Date).ToUniversalTime().ToString('o')
    window     = @{ start = $start.ToString('yyyy-MM-dd'); end = $end.AddDays(-1).ToString('yyyy-MM-dd') }
    source     = 'City of Philadelphia via OpenDataPhilly (phl.carto.com SQL API)'
    datasets   = [ordered]@{}
}
$metaPath = Join-Path $OutDir 'data_meta.json'
if (Test-Path $metaPath) {
    # keep entries for datasets we aren't refreshing this run
    $old = Get-Content $metaPath -Raw | ConvertFrom-Json
    foreach ($p in $old.datasets.PSObject.Properties) { $meta.datasets[$p.Name] = $p.Value }
}

$failed = @()
foreach ($name in $Datasets) {
    $s = $specs[$name]
    Write-Host "== $name ($($s.Table)) $($start.ToString('yyyy-MM-dd')) -> $($end.AddDays(-1).ToString('yyyy-MM-dd'))" -ForegroundColor Cyan
    $tmp = Join-Path $OutDir ($s.File + '.tmp')
    try {
        $header = $null
        $rows = 0
        $writer = [IO.StreamWriter]::new($tmp, $false, [Text.UTF8Encoding]::new($false))
        try {
            # Page one month at a time so no single request is huge.
            for ($m = $start; $m -lt $end; $m = $m.AddMonths(1)) {
                $mEnd = @($m.AddMonths(1), $end) | Sort-Object | Select-Object -First 1
                $sql = "SELECT $($s.Select) FROM $($s.Table) " +
                       "WHERE $($s.DateCol) >= '$($m.ToString('yyyy-MM-dd'))' AND $($s.DateCol) < '$($mEnd.ToString('yyyy-MM-dd'))' " +
                       "AND the_geom IS NOT NULL"
                $csv = Invoke-Carto $sql
                $lines = $csv -split "`r?`n" | Where-Object { $_ -ne '' }
                if ($lines.Count -eq 0) { continue }
                if (-not $header) { $header = $lines[0]; $writer.WriteLine($header) }
                foreach ($l in ($lines | Select-Object -Skip 1)) { $writer.WriteLine($l) }
                $n = [math]::Max(0, $lines.Count - 1)
                $rows += $n
                Write-Host ("  {0:yyyy-MM}  {1,7:N0} rows" -f $m, $n)
            }
        } finally { $writer.Dispose() }

        # ---- validate before replacing the live file ----
        if ($rows -le 0) { throw "no rows returned" }
        $cols = $header -split ','
        $missing = $s.Required | Where-Object { $_ -notin $cols }
        if ($missing) { throw "missing expected columns: $($missing -join ', ')" }
        $final = Join-Path $OutDir $s.File
        if (Test-Path $final) {
            $prevRows = (Get-Content $final | Measure-Object -Line).Lines - 1
            if ($prevRows -gt 1000 -and $rows -lt $prevRows * 0.5) {
                throw "row count dropped from $prevRows to $rows (>50%); keeping previous file"
            }
        }

        Move-Item -Force $tmp $final
        $meta.datasets[$name] = [ordered]@{
            file = $s.File; table = $s.Table; rows = $rows; columns = $cols
            fetched_at = (Get-Date).ToUniversalTime().ToString('o'); status = 'ok'
        }
        Write-Host "  wrote $($s.File): $('{0:N0}' -f $rows) rows" -ForegroundColor Green
    } catch {
        if (Test-Path $tmp) { Remove-Item -Force $tmp }
        Write-Warning "  $name FAILED: $($_.Exception.Message) - previous file left in place"
        if ($meta.datasets.Contains($name)) { $meta.datasets[$name].status = "stale: $($_.Exception.Message)" }
        $failed += $name
    }
}

$meta | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $metaPath
Write-Host "Metadata -> $metaPath"
if ($failed) { Write-Warning "Failed: $($failed -join ', ')"; exit 1 }
