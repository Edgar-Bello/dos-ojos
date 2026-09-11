# Dos Ojos — satellite half

Crop-stress triage for small farms in the Rio Grande Valley. Pulls free Sentinel-2
imagery for user-drawn field polygons, computes vegetation and moisture indices
over time, builds a multi-year seasonal baseline for each field, and flags fields
whose current season is drifting below **their own** normal.

Every field is judged against its own history, never against its neighbours or an
absolute threshold, so a citrus block and a sugarcane field are never compared.

The whole tool runs offline once the cache is populated.

## Quick start

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
./.venv/Scripts/python.exe -m pip install -e . --no-deps
```

Then the four-command demo:

```bash
dosojos-sat init-fields fields.geojson     # validate and register polygons
dosojos-sat fetch --years 5 --workers 8    # ~20 min, the only step needing network
dosojos-sat baseline --season 2026         # day-of-year climatology per field
dosojos-sat score --season 2026 --out out/flags.json
dosojos-sat chart --index NDVI --season 2026
```

After `fetch`, everything else works with the network unplugged. Add `--offline`
to any command to prove it: network access then raises instead of silently
succeeding from a warm DNS cache.

## Requirements

Python 3.11 or 3.12. The geospatial stack (rasterio, GDAL, pyproj) has no usable
wheels on 3.13+ yet, and 3.14 will fail to install.

## Commands

| Command | What it does |
|---|---|
| `init-fields <geojson>` | Validates polygons, computes UTM areas, registers them |
| `fetch` | Searches STAC, reads clipped COGs, masks, computes indices, caches |
| `baseline` | Builds the day-of-year climatology from prior full years |
| `score` | Ranks fields by deviation, writes `out/flags.json` |
| `chart` | Writes a PNG per field: baseline band plus this season |
| `status` | Coverage per field, gaps, disk use, data-quality warnings |
| `scenes` | Diagnostic: search and mask one field without writing anything |

Global flags: `--offline`, `--db <path>`, `--workspace <dir>`, `-v` for debug
logging.

### Past seasons and demo data

```bash
dosojos-sat --workspace ../public_demo/purdue-sorghum-2018/dosojos_sat \
    baseline --season 2018 --history 2019-2022
dosojos-sat --workspace ... score --season 2018 --as-of 2018-07-10
dosojos-sat --workspace ... chart --season 2018 --as-of 2018-07-10 --banner "FREE PUBLIC DATA ..."
```

- `--workspace` keeps `cache/` and `out/` in another folder, so demo fields
  never mix with ours.
- `baseline --history START-END` sets the normal's years explicitly. Sentinel-2
  L2A is only global from 2017, so a 2018 season has no four years before it;
  the years may follow the season but can never include it.
- `score --as-of` and `chart --as-of` judge the season as it stood on a date,
  such as a drone flight's. Charts draw later points faded, and `flags.json`
  records `as_of`.
- `chart --banner` puts a band above the title, used to mark public data.

## How it works

**Source.** AWS Earth Search STAC API, collection `sentinel-2-l2a`. No
authentication, Cloud-Optimized GeoTIFFs, so only the pixels inside each polygon
are ever requested. Whole scenes are never downloaded.

**Masking.** SCL classes 0, 1, 3, 8, 9, 10, 11 (nodata, saturated, cloud shadow,
cloud medium and high, cirrus, snow) are dropped. An observation is discarded
unless 70% of the pixels inside the polygon survive. B11 and SCL are upsampled
from 20 m to 10 m — SCL with nearest neighbour, since interpolating a class map
invents classes that do not exist.

**Indices.** NDVI = (B08−B04)/(B08+B04), NDMI = (B08−B11)/(B08+B11),
NDWI = (B03−B08)/(B03+B08). Per field, date and index we store mean, median, p10,
p25, p75, p90, std and valid fraction. Per-pixel arrays never enter the database;
clipped five-band rasters go to `cache/clips/` as small GeoTIFFs.

**Baseline.** For each field and index, the prior four full calendar years. For
each day of year, all historical observations within ±12 days are pooled, then
smoothed circularly so 1 January is smoothed against late December. Each bin
records how many observations and how many distinct years back it, graded
`high` / `medium` / `low` / `none`.

**Scoring.** Two independent triggers flag a field:

1. **p10 run** — NDVI below the baseline p10 for 2+ consecutive observations.
2. **Sustained shortfall** — below the baseline median for 6+ consecutive
   observations with a median shortfall of 10% or more.

The second exists because the first has a blind spot: a bad year inside the
baseline widens p10 so far that a *second* bad year never crosses it. On real
data, one field sat 13% below its own normal for eleven consecutive observations
and was never flagged by the p10 rule alone.

NDMI is a secondary signal. NDVI down *with* NDMI down reads as water stress;
NDVI down alone points at disease, nutrient deficiency, or a harvest.

## Gotchas worth knowing

**The BOA reflectance offset is already applied.** Every item declares
`raster:bands` with `offset: -0.1`, but also carries
`earthsearch:boa_offset_applied: true`, meaning the COG mirror already folded it
in. The declared offset describes ESA's original convention, not the bytes in the
file. Applying it a second time drove ~70% of a field's green and red reflectance
negative and pinned NDVI at 1.0. The flag is the authority, not the offset.
`stac.warn_if_implausible` now shouts if any band comes back >25% negative, and
`status` reports indices pinned at their bound.

**MGRS tiles overlap by ~10 km.** A small field routinely sits wholly inside four
tiles of the same acquisition. Reading all four costs four times the requests for
identical ground, so only the cleanest covering tile is read — a 4.5× speed-up.
The others are kept as fallbacks for the rare tile that is nodata over the field.

**A few items point at requester-pays JP2 originals** rather than the free COG
mirror. Those are skipped in favour of another tile from the same pass. In
practice this affects about 1 day in 288.

**Clamp before dividing.** Atmospheric correction pushes red slightly negative
over dense canopy. Left unclamped this drives NDVI above 1, and discarding those
as out-of-range throws away precisely the greenest pixels — it cost 37% of the
pixels on one field and biased the observation downward.

## Known limitations

- **A harvested field looks like a stressed one.** NDVI crashes either way.
  Distinguishing them needs crop calendars we do not have. The charts make it
  obvious to a human, which is why this is triage rather than an alarm.
- **`fields.geojson` ships with placeholder polygons.** They are plausible
  footprints on real RGV farmland, not surveyed boundaries, and the crop labels
  are invented. Replace them with real ones; no code change is needed.

## Layout

```
fields.geojson        input polygons (WGS84) with id, name, crop, optional acres
src/
  config.py           settings, band mapping, mask classes, logging
  fields.py           polygon parsing, validation, UTM area
  stac.py             STAC search, tile selection, windowed COG reads
  indices.py          index maths, SCL masking, summary statistics
  cache.py            SQLite schema and all read/write
  baseline.py         climatology and deviation scoring
  pipeline.py         fetch orchestration over a thread pool
  charts.py           matplotlib output
  cli.py              click commands
cache/                sqlite database and clipped GeoTIFFs
out/                  flags.json and chart PNGs
tests/                157 tests, no network required
```

`fields.py`, `charts.py`, `config.py` and `pipeline.py` are additions to the
original module plan.

## Tests

```bash
./.venv/Scripts/python.exe -m pytest -q
```

157 tests, none of which touch the network. They cover index arithmetic against
hand-computed values, the reflectance transform, SCL masking, tile selection,
circular day-of-year distance and windowing, percentile pooling against a known
distribution, robust z, run detection, and both flag triggers.
