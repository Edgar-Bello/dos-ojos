# FREE PUBLIC DATA, NOT DOS OJOS FIELDS

Four real Rio Grande Valley fields, one each of grain sorghum, cotton, corn and
citrus, found in public government data. We have no link to these fields or
their owners. Everything here comes from, or was computed from, public-domain
datasets, and none of it may be mixed with data a farmer gives us. It exists to
show that the pipeline works on more than one crop before we can fly our own
drones (we don't have the drone license yet).

## The fields

Picked by `dosojos-sat cropmap` from the **USDA NASS Cropland Data Layer**
(CDL), the government's 30 m map of what grew where. It is public domain and read from
`https://pdi.scinet.usda.gov/image/rest/services/CDL_WM/ImageServer`.

Search box: -98.45, 26.05 to -97.45, 26.55 (Hidalgo, Cameron and Willacy
counties). A block of pixels counts as one field if it meets all of these:

- the same crop throughout in 2025;
- traced 45 m inside its edge, so the border and roads stay out;
- cut at any waist narrower than 160 m;
- at least 90% that crop, and 15 to 250 acres;
- for the annual crops, one crop over at least 75% of it in each of 2021-2024 too, per small CDL clips of those years.

Each pick was then checked by eye against USGS aerial imagery: one field, between roads. The outlines
are the map's pixels, not a survey, and the real fields are a little larger.

| id | crop | where | traced acres |
|---|---|---|---|
| PUBLIC-rgv-sorghum-1 | grain sorghum | near Elsa | 43.9 |
| PUBLIC-rgv-cotton-1 | cotton | near Lyford | 42.1 |
| PUBLIC-rgv-corn-1 | corn | near Lyford, next to the cotton | 45.1 |
| PUBLIC-rgv-citrus-1 | citrus | near Monte Alto | 33.4 |

**Why the history and waist checks exist.** The first version of this demo picked
blocks that turned out to be two fields each. It kept 2025 purity only, and those
picks now sit in `Dos_Ojos/old_demos/rgv-crops-2025-v1-merged-fields`.

- **Corn near Raymondville:** two corn fields with a farm road between them. Their 2021-2023 maps split 58/40 along the road.
- **What that did to the ground check:** lidar showed each field draining to the road ditch. The terrain check read that as one "uneven" field with a low middle, and asked for 119 cubic yards per acre of leveling that neither field needs.
- **The first cotton and sorghum picks:** split roughly in half in earlier years too.

**No sugarcane:** the 2025 map has no cane field in the box that passes these
tests. The Valley's only sugar mill closed in 2024, so we show none rather than
fake one.

`dosojos_sat/out/cropmap_2025.png` shows the crop map with the four fields marked.

## Satellite side

- **Imagery:** Sentinel-2 L2A from Earth Search (AWS, free), 2021 to 2025.
- **Normal range:** 2021 to 2024.
- **Season:** judged as of 20 May 2025.
- **Weather:** gridMET for 2025.
- **Soil:** USDA SSURGO.

`dosojos_sat/out/flags.json` flags none of the four. Where each stood against its own normal:

| field | NDVI on 17 May | normal | percentile |
|---|---|---|---|
| cotton | 0.50 | 0.64 | 30th: greening a little later than usual |
| corn | 0.67 | 0.74 | 33rd |
| citrus | 0.57 | 0.54 | 64th |
| grain sorghum | 0.65 | 0.49 | 89th: ahead of its usual year |

2025 green-up, which the made-up planting dates in the SMS demo follow:

- **Corn:** up from 11 March, peak 0.81 on 10 May, dry-down by late June.
- **Cotton:** up from early April, 0.86 by 4 June.
- **Sorghum:** up from early April, 0.84 on 10 May.
- **Citrus:** a steady 0.55 to 0.75.

**Earth Search gotcha, fixed in `stac.py`:**

- **The problem:** 17 scenes over these fields, from 2022 onward, say `earthsearch:boa_offset_applied=false`, but their pixels already carry the offset.
- **The fix:** the fetch checks the darkest red pixels before trusting that flag, and logs each correction. Applying the offset again would make the red band negative.
- **Older caches:** `dosojos-sat recheck-offsets` repairs caches fetched before the fix.

## Ground (drone half, no drone)

The drone half's `terrain` check, run on **USGS 3DEP airborne lidar**: 2 m
bare-earth (DTM) and surface (DSM) grids flown in 2019, public domain, read from
Microsoft Planetary Computer by `dosojos-drone lidar`. The ground is from 2019,
so later leveling won't show.

- **Cotton near Lyford** (survey TX_South_B8_2018):
  - falls 0.06% from west to east;
  - mostly even: 67% within 3 cm of a smooth plane;
  - two low corners: south-east, down to 13 cm over about 2.4 acres, and south-west, 8 cm.
  - A land-plane touch-up of about 64 cubic yards per acre would even it.
- **Corn near Lyford** (same survey): falls 0.04% from west to east; 72% within 3 cm; one low north-west corner, 8 cm.
- **Grain sorghum near Elsa:** not used, and `lidar` now refuses it.
  - **The crop:** when survey TX_South_B7_2018 flew, a crop over 1 m tall stood on 85% of the field, probably cane, which the 2021 map still shows nearby. The laser rarely reached the ground, and the "relief" was the crop: 2 m high spots, 1,000 cubic yards per acre to move.
  - **Set aside:** that result is in `Dos_Ojos/old_demos/rgv-crops-2025-sorghum-lidar-under-crop`.
  - **The catalog:** it lists that survey's tiles mirrored north to south, up to 60 km off. `lidar` now checks each tile's own extent.
- **Citrus:** not used. The laser hits tree canopy, so the ground under a grove isn't reliable.

## The SMS demo on these fields

`dosojos_sms/examples/setup_sms_demo.cmd` puts three MADE-UP farmers on these
same four outlines, pinned to Tue 20 May 2025.

**The farmers,** each on a different one of the three services, so the demo shows
all three:

- **Juan Ejemplo:** the cotton and the corn, furrows from the west. Satellite,
  drone and thermal camera.
- **Maria Ejemplo:** the sorghum, rainfed. Satellite only, which needs no
  hardware and no licence at all.
- **Mary Example:** the citrus, flooding. Satellite and drone, with no flight
  uploaded yet.

**What's made up:** the farmers, their planting and watering dates (invented to
match the green-up above), and one thermal mosaic.

**The thermal mosaic is synthetic.** We have no thermal camera, so the one warm
patch on Juan's corn was generated by
`../thermal-synthetic/make_thermal.py`; see that folder's `SOURCE.md`. Every
figure drawn from it carries a SYNTHETIC band. It is the only made-up picture in
this demo, and deleting its block from `setup_sms_demo.cmd` leaves everything
else working.

**What's real:** everything else. That's the satellite, weather, soil and the cotton
and corn lidar, all fetched fresh into `dosojos_sms/examples/demo_data`.

**The "why" file.** Every AGUA now ends by offering one: answer `PORQUE` and each
field comes back with a link to a page showing how its answer was worked out —
the NDVI curve against the field's own 2021-2024 normal, the water checkbook's
arithmetic and chart, the lidar ground map, and for Juan the thermal patch with
the reasoning behind its score. Nothing is sent unless the farmer asks.

## Layout

```
dosojos_sat/     satellite workspace: fields.geojson, cache/ (crop map clips, imagery, weather, soil), out/
dosojos_drone/   drone workspace: flights.json, data/ (lidar tiles as read), out/<flight>/terrain.png
run_demo.sh      every command, in order (Git Bash)
run_demo.cmd     runs run_demo.sh from PowerShell: .\run_demo.cmd
```
