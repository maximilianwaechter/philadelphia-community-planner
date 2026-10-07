#!/usr/bin/env python3
"""
check_data.py - checks every data file the Community Area Planner loads.

Run from the repo folder:   python scripts/check_data.py
Only the Python standard library is used. Exit code 1 if anything FAILs.

What it checks
  * every reference layer in docs/data: valid GeoJSON, feature counts, geometry types,
    coordinates inside Philadelphia, the label field the app expects
  * ACS tract CSV and building weights: building model sums back to each tract's ACS totals
  * outreach heat map vs. outreach-by-ZIP totals
  * police contacts match the district / division boundaries
  * crime data (2014 on): month list, offense list, areas.json, segments.json, seg_YYYY.json,
    that month numbers point at the right year, and how much of each year lands on streets
"""
import csv, json, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'docs' / 'data'
CRIME = DATA / 'crime'
CRIME_START = '2014-01'
BBOX = (-75.30, 39.85, -74.94, 40.15)       # Philadelphia with a margin (lon/lat)
PRIORITY_ZIPS = {'19121', '19122', '19123', '19125', '19130', '19132', '19133', '19140'}
csv.field_size_limit(10**8)

rows_out = []
def report(level, name, msg): rows_out.append((level, name, msg))
PASS = lambda n, m: report('PASS', n, m)
WARN = lambda n, m: report('WARN', n, m)
FAIL = lambda n, m: report('FAIL', n, m)

def load_json(p):
    with open(p, encoding='utf-8-sig') as f:
        return json.load(f)

def coords(g):
    if not g: return
    if g.get('type') == 'GeometryCollection':
        for x in g.get('geometries') or []: yield from coords(x)
        return
    def walk(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)): yield c
        elif isinstance(c, (list, tuple)):
            for x in c: yield from walk(x)
    yield from walk(g.get('coordinates'))

def inside(x, y): return BBOX[0] <= x <= BBOX[2] and BBOX[1] <= y <= BBOX[3]

# ---------------------------------------------------------------- reference layers
# (name in app, file, required?, label field the app uses, expected geometry)
LAYERS = [
    ('Philadelphia City Limits', 'City_Limits.geojson', True, None, 'poly'),
    ('Council Districts 2024', 'Council_Districts_2024.geojson', True, None, 'poly'),
    ('Police Divisions', 'police_divisions.geojson', False, 'division', 'poly'),
    ('Police Districts', 'police_districts.geojson', False, 'dist_numc', 'poly'),
    ('Police Service Areas', 'police_psa.geojson', False, 'psa', 'poly'),
    ('Political Wards', 'political_wards.geojson', False, 'ward_num', 'poly'),
    ('Voting Divisions', 'political_divisions.geojson', False, 'division_num', 'poly'),
    ('ZIP Codes', 'zip_codes.geojson', False, 'code', 'poly'),
    ('Neighborhood Advisory Committees', 'NeighborhoodAdvisoryCommittees.geojson', False, 'ORGANIZATION', 'poly'),
    ('TU Police Boundary', 'TU_police_boundary.geojson', True, 'Campus', 'poly'),
    ('TU Buildings', 'TU_buildings.geojson', True, 'PROPERTY_A', 'poly'),
    ('TU Police Stations', 'TU_Police_Stations.geojson', True, None, 'point'),
    ('Philadelphia Police Stations', 'Police_Stations.geojson', True, 'dist_num', 'point'),
    ('Schools of Interest', 'Schools_of_Interest.geojson', True, None, 'point'),
    ('NAC Offices', 'NAC_Offices.geojson', False, 'ORGANIZATION', 'point'),
    ('Planning polygons (base plan)', 'Polygons_of_interest.geojson', True, None, 'poly'),
]
LIVE_FALLBACK = {'police_districts.geojson', 'police_psa.geojson', 'political_wards.geojson',
                 'political_divisions.geojson', 'zip_codes.geojson'}
geo = {}

def check_layer(name, fname, required, field, kind):
    p = DATA / fname
    if not p.exists():
        if required: FAIL(name, f'missing docs/data/{fname}')
        elif fname in LIVE_FALLBACK: WARN(name, f'docs/data/{fname} missing; the app will pull it live from the City')
        else: WARN(name, f'docs/data/{fname} missing; layer will show "not loaded"')
        return
    try: fc = load_json(p)
    except Exception as e: FAIL(name, f'{fname} is not valid JSON: {e}'); return
    feats = fc.get('features') if isinstance(fc, dict) else None
    if not feats: FAIL(name, f'{fname} has no features'); return
    geo[fname] = fc
    types, nogeom, n_pts, n_out, projected = defaultdict(int), 0, 0, 0, 0
    for f in feats:
        g = f.get('geometry')
        if not g: nogeom += 1; continue
        types[g.get('type')] += 1
        for c in coords(g):
            n_pts += 1
            if abs(c[0]) > 180 or abs(c[1]) > 90: projected += 1
            elif not inside(c[0], c[1]): n_out += 1
    tstr = ', '.join(f'{v} {k}' for k, v in types.items())
    want = ('Polygon', 'MultiPolygon') if kind == 'poly' else ('Point', 'MultiPoint')
    wrong = sum(v for k, v in types.items() if k not in want and k != 'GeometryCollection')
    msg = f'{len(feats):,} features ({tstr})'
    level = PASS
    if projected:
        crs = ((fc.get('crs') or {}).get('properties') or {}).get('name', 'no crs tag')
        msg += f'; coordinates are projected ({crs}) - the app reprojects only PA State Plane South (2272/32129) and Web Mercator'
        level = WARN
    if n_out:
        msg += f'; {n_out:,} of {n_pts:,} vertices fall outside Philadelphia'
        level = FAIL if n_out / max(n_pts, 1) > 0.05 else WARN
    if nogeom: msg += f'; {nogeom} with no geometry (skipped by the app)'; level = WARN if level is PASS else level
    if wrong and fname != 'Polygons_of_interest.geojson':
        msg += f'; {wrong} not {"polygons" if kind == "poly" else "points"}'; level = WARN if level is PASS else level
    if field:
        have = sum(1 for f in feats if (f.get('properties') or {}).get(field) not in (None, ''))
        if have < len(feats):
            msg += f'; label field "{field}" missing on {len(feats) - have} feature(s)'
            level = WARN if level is PASS else level
    level(name, msg)

for args in LAYERS: check_layer(*args)

if 'zip_codes.geojson' in geo:
    codes = {str((f.get('properties') or {}).get('code')) for f in geo['zip_codes.geojson']['features']}
    miss = PRIORITY_ZIPS - codes
    (FAIL if miss else PASS)('Temple Priority ZIP Codes', f'missing {sorted(miss)}' if miss else 'all 8 priority ZIPs present')

if 'Polygons_of_interest.geojson' in geo:
    names = [str((f.get('properties') or {}).get('Name') or (f.get('properties') or {}).get('name') or '') for f in geo['Polygons_of_interest.geojson']['features']]
    unnamed = sum(1 for n in names if not n.strip())
    (WARN if unnamed else PASS)('Planning polygon names', f'{len(names)} polygons, {unnamed} unnamed' if unnamed else f'{len(names)} polygons, all named')

# ---------------------------------------------------------------- demographics
COUNT_FIELDS = ['pop_total', 'hh_pop', 'hh_occ', 'owner_occ', 'renter_occ', 'pov_univ', 'pov_below',
    'race_total', 'white_nh', 'black_nh', 'asian_nh', 'hispanic', 'workers', 'drv_alone', 'pub_trans', 'bicycle',
    'walked', 'wfh', 'veh_hh', 'no_vehicle', 'sch_univ', 'k12_enr', 'pop25plus', 'hs_grad', 'bachelors', 'masters',
    'prof_deg', 'doctorate', 'labor_frc', 'employed', 'unemployed', 'hins_univ', 'dis_univ', 'lang_univ', 'inet_univ',
    'under18', 'age65plus']
MEDIANS = ['med_hh_inc', 'med_rent', 'med_value']

def num(v):
    try: return float(str(v).replace(',', ''))
    except (TypeError, ValueError): return None
def geoid(v): return str(v or '').strip().removesuffix('.0').zfill(11)

tracts = {}
p = DATA / 'philly_tracts_acs2024.csv'
if not p.exists(): FAIL('ACS tracts', 'missing docs/data/philly_tracts_acs2024.csv (no demographics)')
else:
    with open(p, encoding='utf-8-sig', newline='') as f:
        rd = csv.DictReader(f); hdr = rd.fieldnames or []
        gcol = next((c for c in ('TRACT_GEOID', 'tract_geoid', 'GEOID', 'geoid') if c in hdr), None)
        if not gcol: FAIL('ACS tracts', 'no TRACT_GEOID/GEOID column')
        else:
            dup = 0
            for r in rd:
                g = geoid(r[gcol])
                if g in tracts: dup += 1
                tracts[g] = {k: num(v) for k, v in r.items()}
    if tracts:
        nonphl = [g for g in tracts if not g.startswith('42101')]
        miss = [c for c in COUNT_FIELDS + MEDIANS if c not in hdr]
        neg = defaultdict(int)
        for t in tracts.values():
            for c in COUNT_FIELDS + MEDIANS:
                if t.get(c) is not None and t[c] < 0: neg[c] += 1
        pop = sum(t.get('pop_total') or 0 for t in tracts.values())
        msg = f'{len(tracts)} tracts, city population {pop:,.0f}'
        lvl = PASS
        if dup: msg += f'; {dup} duplicate GEOIDs'; lvl = FAIL
        if nonphl: msg += f'; {len(nonphl)} GEOIDs outside Philadelphia County'; lvl = WARN
        if miss: msg += f'; columns missing (those metrics hide): {", ".join(miss)}'; lvl = WARN if lvl is PASS else lvl
        if not 1_400_000 < pop < 1_700_000: msg += '; city population looks wrong'; lvl = WARN if lvl is PASS else lvl
        lvl('ACS tracts', msg)
        if neg: WARN('ACS negative values', 'ignored by the app (ACS "no estimate" codes): ' + ', '.join(f'{k} in {v} tracts' for k, v in neg.items()))
        bad = [g for g, t in tracts.items() if None not in (t.get('owner_occ'), t.get('renter_occ'), t.get('hh_occ'))
               and abs(t['owner_occ'] + t['renter_occ'] - t['hh_occ']) > 1]
        (WARN if bad else PASS)('ACS internal check', f'owner + renter != households in {len(bad)} tracts' if bad else 'owner + renter households = households in every tract')

p = DATA / 'building_weights_web.csv'
if not p.exists(): FAIL('Building weights', 'missing docs/data/building_weights_web.csv (no demographics)')
elif tracts:
    sums = defaultdict(lambda: defaultdict(float)); n = no_xy = out = no_tract = 0
    with open(p, encoding='utf-8-sig', newline='') as f:
        rd = csv.DictReader(f); hdr = rd.fieldnames or []
        model = 'hh_pop' in hdr
        for r in rd:
            n += 1
            x, y = num(r.get('lon') or r.get('longitude')), num(r.get('lat') or r.get('latitude'))
            if x is None or y is None: no_xy += 1; continue
            if not inside(x, y): out += 1
            t = geoid(r.get('tract_geoid') or r.get('TRACT_GEOID'))
            if t not in tracts: no_tract += 1; continue
            if model:
                for k in ('hh_pop', 'occ', 'own', 'rnt'): sums[t][k] += max(num(r.get(k)) or 0, 0)
    msg = f'{n:,} buildings' + ('' if model else ' (old capacity-only file: no per-building population)')
    lvl = PASS if model else WARN
    if no_xy: msg += f'; {no_xy:,} without coordinates (dropped)'; lvl = WARN
    if out: msg += f'; {out:,} outside Philadelphia'; lvl = WARN
    if no_tract: msg += f'; {no_tract:,} with a tract not in the ACS file (dropped)'; lvl = WARN
    lvl('Building weights', msg)
    if model:
        # The model is meant to sum back to each tract's ACS totals.
        for bk, ak, label in (('hh_pop', 'hh_pop', 'household population'), ('occ', 'hh_occ', 'households'),
                              ('own', 'owner_occ', 'owner households'), ('rnt', 'renter_occ', 'renter households')):
            off, lost, worst = 0, 0, (0, '')
            for g, t in tracts.items():
                acs = t.get(ak)
                if not acs or acs <= 0: continue
                got = sums[g][bk] if g in sums else 0
                if got == 0: lost += 1; continue
                rel = abs(got - acs) / acs
                if rel > 0.02: off += 1
                if rel > worst[0]: worst = (rel, g)
            msg = f'buildings sum to the ACS {label} within 2% in all but {off} tract(s)'
            if worst[1]: msg += f' (worst {worst[1]}: {worst[0]:.1%} off)'
            if lost: msg += f'; {lost} tract(s) with ACS {label} but no residential buildings'
            (PASS if off == 0 and lost == 0 else WARN)(f'Model balance: {label}', msg)

# ---------------------------------------------------------------- outreach
heat_total = None
p = DATA / 'outreach_heat.csv'
if not p.exists(): WARN('Outreach heat map', 'docs/data/outreach_heat.csv missing (layer and member counts hidden)')
else:
    with open(p, encoding='utf-8-sig', newline='') as f:
        rd = list(csv.DictReader(f))
    good = [r for r in rd if num(r.get('lat')) is not None and num(r.get('lon')) is not None]
    heat_total = sum(num(r.get('total')) or 0 for r in good)
    out = sum(1 for r in good if not inside(num(r['lon']), num(r['lat'])))
    cols = [c for c in ('total', 'active', 'disengaged', 'milestone', 'inquiry') if rd and c in rd[0]]
    sub = sum(sum(num(r.get(c)) or 0 for c in cols if c != 'total') for r in good)
    msg = f'{len(good):,} cells, {heat_total:,.0f} members; statuses: {", ".join(cols)}'
    lvl = PASS if good else FAIL
    if out: msg += f'; {out} cells outside Philadelphia'; lvl = WARN
    if len(cols) > 1 and abs(sub - heat_total) > 0.5: msg += f'; statuses add to {sub:,.0f}, not the total'; lvl = WARN
    lvl('Outreach heat map', msg)
p = DATA / 'outreach_by_zip.csv'
if p.exists():
    with open(p, encoding='utf-8-sig', newline='') as f:
        zt = sum(num(r.get('total')) or 0 for r in csv.DictReader(f))
    msg = f'{zt:,.0f} members by ZIP'
    if heat_total is not None: msg += f' vs {heat_total:,.0f} on the heat map ({zt - heat_total:+,.0f}: members with a ZIP but no mappable address)'
    (WARN if heat_total is not None and heat_total > zt else PASS)('Outreach by ZIP', msg)

# ---------------------------------------------------------------- police contacts
p = DATA / 'police_contacts.json'
if not p.exists(): WARN('Police contacts', 'docs/data/police_contacts.json missing (no captain/phone popups)')
else:
    c = load_json(p); d = c.get('districts') or {}; v = c.get('divisions') or {}
    msg, lvl = f'{len(d)} districts, {len(v)} divisions, as of {c.get("as_of", "?")}', PASS
    if 'police_districts.geojson' in geo:
        keys = {str((f.get('properties') or {}).get('dist_numc', '')).removesuffix('.0').zfill(2) for f in geo['police_districts.geojson']['features']}
        miss = sorted(keys - set(d))
        if miss: msg += f'; no contact for districts {miss}'; lvl = WARN
    if 'police_divisions.geojson' in geo:
        keys = {str((f.get('properties') or {}).get('division')) for f in geo['police_divisions.geojson']['features']}
        miss = sorted(keys - set(v))
        if miss: msg += f'; no entry for divisions {miss}'; lvl = WARN
    orphan = sorted({n for x in v.values() for n in x.get('districts', [])} - set(d))
    if orphan: msg += f'; divisions list unknown districts {orphan}'; lvl = WARN
    lvl('Police contacts', msg)

# ---------------------------------------------------------------- crime
def crime_checks():
    if not (CRIME / 'meta.json').exists():
        FAIL('Crime data', 'docs/data/crime/meta.json missing - run scripts/06_crime_data.py'); return
    meta = load_json(CRIME / 'meta.json')
    months, offs = meta.get('months') or [], meta.get('offenses') or []
    if not months or not offs: FAIL('Crime meta', 'no months or offenses in meta.json'); return
    gaps = [f'{a}->{b}' for a, b in zip(months, months[1:])
            if (int(b[:4]) * 12 + int(b[5:])) - (int(a[:4]) * 12 + int(a[5:])) != 1]
    k = next((i for i, m in enumerate(months) if m >= CRIME_START), None)
    if k is None: FAIL('Crime meta', f'no months from {CRIME_START} on'); return
    kept = months[k:]
    msg = f'{len(months)} months in file ({months[0]} to {months[-1]}); app keeps {len(kept)} ({kept[0]} to {kept[-1]}), dropping {k}'
    lvl = PASS
    if gaps: msg += f'; months not consecutive: {gaps[:5]}'; lvl = FAIL
    if months[0] > CRIME_START: msg += f'; data starts after {CRIME_START}'; lvl = WARN
    lvl('Crime months', msg)
    kinds = defaultdict(int)
    for o in offs: kinds[o.get('kind')] += 1
    cov = meta.get('coverage') or {}
    covs = '; '.join(f'{kk}: {max(v["first"], CRIME_START)} to {v["last"]}' for kk, v in cov.items() if v)
    (PASS if kinds.get('crime') else FAIL)('Crime offenses', f'{dict(kinds)}; coverage after the 2014 cut - {covs}')

    # areas.json
    if not (CRIME / 'areas.json').exists(): FAIL('Crime areas.json', 'missing'); return
    areas = load_json(CRIME / 'areas.json')
    year_of = lambda i: months[i][:4]
    def totals(flat, label):
        if len(flat) % 3: FAIL(label, 'length is not a multiple of 3'); return None
        t, bad = defaultdict(float), 0
        for i in range(0, len(flat), 3):
            m, o, c = int(flat[i]), int(flat[i + 1]), flat[i + 2]
            if not (0 <= m < len(months)) or not (0 <= o < len(offs)) or c < 0: bad += 1; continue
            if m >= k: t[(year_of(m), offs[o]['kind'])] += c
        if bad: FAIL(label, f'{bad} entries with an out-of-range month/offense or a negative count')
        return t
    city = totals(areas.get('city') or [], 'Crime citywide')
    if city is None: return
    years = sorted({y for y, _ in city})
    PASS('Crime citywide 2014+', '; '.join(f'{y}: {city[(y, "crime")]:,.0f} incidents' + (f', {city[(y, "shooting")]:,.0f} shooting victims' if city.get((y, 'shooting')) else '') + (f', {city[(y, "stop")]:,.0f} stops' if city.get((y, 'stop')) else '') for y in years))
    for grp, gname, fname, prop in (('dist', 'districts', 'police_districts.geojson', 'dist_numc'),
                                    ('psa', 'PSAs', 'police_psa.geojson', 'psa'),
                                    ('division', 'divisions', 'police_divisions.geojson', 'division')):
        sub = areas.get(grp) or {}
        if not sub: WARN(f'Crime by {gname}', 'none in areas.json'); continue
        tot = defaultdict(float)
        for key, flat in sub.items():
            t = totals(flat, f'Crime {gname} {key}')
            if t:
                for kk, v in t.items(): tot[kk] += v
        share = sum(tot[(y, 'crime')] for y in years) / max(sum(city[(y, 'crime')] for y in years), 1)
        msg, lvl = f'{len(sub)} {gname}; together hold {share:.1%} of citywide incidents since 2014 (the rest have no district)', PASS
        if share > 1.001: msg += ' - MORE than the city total, something is double counted'; lvl = FAIL
        elif share < 0.95: lvl = WARN
        if fname in geo:
            def norm(v): v = str(v).removesuffix('.0'); return v.zfill(2) if grp == 'dist' else v
            keys = {norm((f.get('properties') or {}).get(prop, '')) for f in geo[fname]['features']}
            miss = sorted(keys - set(sub))
            if miss: msg += f'; map {gname} with no crime entry: {miss[:10]}'; lvl = WARN if lvl is PASS else lvl
        lvl(f'Crime by {gname}', msg)

    # segments.json
    if not (CRIME / 'segments.json').exists(): FAIL('Crime segments.json', 'missing (no street map, no drawn-area counts)'); return
    s = load_json(CRIME / 'segments.json')
    n = len(s.get('seg_id') or [])
    lens = {kk: len(s.get(kk) or []) for kk in ('seg_id', 'lon', 'lat', 'ft')}
    if len(set(lens.values())) != 1: FAIL('Crime segments.json', f'arrays differ in length: {lens}'); return
    out = sum(1 for x, y in zip(s['lon'], s['lat']) if not inside(x, y))
    dup = n - len(set(s['seg_id']))
    zero = sum(1 for f in s['ft'] if not f or f <= 0)
    msg, lvl = f'{n:,} street segments', PASS
    if out: msg += f'; {out} midpoints outside Philadelphia'; lvl = WARN
    if dup: msg += f'; {dup} duplicate seg_ids'; lvl = FAIL
    if zero: msg += f'; {zero} with zero length'; lvl = WARN if lvl is PASS else lvl
    lvl('Crime segments.json', msg)

    # seg_YYYY.json, 2014 on
    want = [int(y) for y in meta.get('years') or [] if int(y) >= int(CRIME_START[:4])]
    for y in want:
        p = CRIME / f'seg_{y}.json'
        if not p.exists(): FAIL(f'Crime seg_{y}.json', 'missing (street map and drawn areas show 0 for this year)'); continue
        rows = load_json(p).get('rows') or []
        if len(rows) % 4: FAIL(f'Crime seg_{y}.json', 'rows length is not a multiple of 4'); continue
        t, wrong_year, bad = defaultdict(float), 0, 0
        for i in range(0, len(rows), 4):
            si, m, o, c = rows[i:i + 4]
            if not (0 <= si < n) or not (0 <= m < len(months)) or not (0 <= o < len(offs)): bad += 1; continue
            if months[m][:4] != str(y): wrong_year += 1
            t[offs[o]['kind']] += c
        if wrong_year:
            FAIL(f'Crime seg_{y}.json', f'{wrong_year:,} rows whose month number is not in {y}: month numbers are not '
                 f'positions in meta.months, so the 2014 cut would shift months. Tell me and I will adjust the patch.')
            continue
        on_street = t['crime'] / max(city.get((str(y), 'crime'), 0), 1)
        qa = (meta.get('qa') or {}).get(str(y), {}).get('incidents', {})
        msg = f'{len(rows) // 4:,} rows; {on_street:.1%} of the year\'s incidents are on a street segment'
        if qa.get('records'):
            expect = 1 - (qa.get('no_coordinates', 0) + qa.get('not_on_a_street', 0)) / qa['records']
            msg += f' (QA in meta.json expects {expect:.1%})'
        lvl = FAIL if bad or on_street > 1.001 else WARN if on_street < 0.85 else PASS
        if bad: msg += f'; {bad} rows with an out-of-range index'
        lvl(f'Crime seg_{y}.json', msg)

try: crime_checks()
except Exception as e: FAIL('Crime data', f'check crashed: {type(e).__name__}: {e}')

# ---------------------------------------------------------------- report
colors = {'PASS': '\033[32m', 'WARN': '\033[33m', 'FAIL': '\033[31m'}
use_color = sys.stdout.isatty()
for lvl, name, msg in rows_out:
    tag = f'{colors[lvl]}{lvl}\033[0m' if use_color else lvl
    print(f'{tag}  {name}: {msg}')
c = defaultdict(int)
for lvl, _, _ in rows_out: c[lvl] += 1
print(f'\n{c["PASS"]} passed, {c["WARN"]} warnings, {c["FAIL"]} failed')
sys.exit(1 if c['FAIL'] else 0)
