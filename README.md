# Dos Ojos — drone half

Post-processing for UAV flights over Rio Grande Valley crop fields. The drone
records; you bring the SD card back; this turns it into per-plant metrics. There
is no onboard compute and nothing happens in real time.

Outputs join the satellite half on `field_id`, so a field flagged from orbit can
be confirmed or dismissed from 60 metres.

## Status

| Step | What | State |
|---|---|---|
| 1 | Ingest, EXIF validation, coverage quicklook | done |
| 2 | Video fallback (MP4 + DJI SRT → geotagged JPEGs) | done |
| 3 | ODM run via Docker | done |
| 4 | Canopy height model (DSM − DTM) | done |
| 5 | Plant/row detection | done |
| 6 | Per-plant metrics and RGB indices | done |
| 7 | Dead / missing / stressed flags | done |
| 8 | Overlay, histogram, block summary JSON, satellite join | done |
| — | Run on real public data (Purdue 2018 sorghum): import, blocks, known row spacing, row tracking | done |

## Setup

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
./.venv/Scripts/python.exe -m pip install -e . --no-deps
```

On Windows 11, Smart App Control may refuse `dosojos-drone` with "An Application
Control policy has blocked this file": it blocks the unsigned launcher pip
builds. Every command works the same through Python, which it allows:
`./.venv/Scripts/python.exe -m dosojos_drone <command>`, or `python -m dosojos_drone`
with the venv active.

Python 3.11 or 3.12. ffmpeg ships with the project via `imageio-ffmpeg`, so the
video path needs no system install; a system ffmpeg on PATH is preferred if
present.

**Docker is required for step 3.** Check it with `dosojos-drone doctor`. On this
machine Docker Desktop runs on the WSL2 backend with 10 GB and 10 CPUs, set in
`%USERPROFILE%\.wslconfig`:

```ini
[wsl2]
memory=10GB
processors=10
swap=16GB
```

The swap matters. ODM spikes during dense reconstruction, and swap turns an
out-of-memory kill four hours in into something merely slow.

## Normal workflow

```bash
dosojos-drone register demo-001 --field rgv-002 --crop "grain sorghum" \
    --date 2026-09-05 --ground-elevation 12
dosojos-drone survey demo-001
```

`register` writes `flights.json`, which maps a flight to a satellite field.
`survey` reads every EXIF header and tells you whether the set is worth an hour
of photogrammetry, then writes `out/<flight_id>/quicklook.png` and `survey.json`.

## What `survey` checks

It exists to fail before ODM does. A flight with thin overlap or missing GPS will
burn hours and then produce a hole-ridden orthomosaic.

```
  images           200  (200 with GPS)
  camera           FC6310
  altitude         72 m        spread  2.2%
  GSD              6.57 cm/px
  footprint        90 x 60 m
  shot spacing     12.4 m
  forward overlap  80%

  coverage         45% of the registered field (17.8 of 39.9 acres)
  full coverage    about 499 images at this altitude
                   or roughly 95 m AGL to cover it with the images you have
```

Messages prefixed `BLOCKER` mean the run is not worth starting; the command exits
non-zero on those. Everything else is advisory.

**Flying height needs a reference.** Absolute GPS altitude says nothing about the
ground beneath it, so height above ground comes from DJI's XMP `RelativeAltitude`
tag, or from `--ground-elevation <metres AMSL>` at registration. Without either,
GSD and overlap are reported as unknown rather than guessed — an earlier version
inferred ground level from the lowest altitude in the survey and reported a false
"0% overlap" failure on a perfectly good flight.

**Coverage is worth planning around.** At 60 m with standard 80/70 overlap, 200
frames cover roughly 45% of a 40-acre field. Full coverage needs about 500 frames,
or a higher flight at coarser resolution.

## Photogrammetry

```bash
dosojos-drone doctor --images 200      # Docker, memory, ODM image
dosojos-drone odm demo-001 --dry-run   # stage images, print the command
dosojos-drone odm demo-001
```

Produces orthophoto, DSM, DTM and point cloud under `data/odm/<flight_id>/`.
Expect hours on a laptop for a few hundred images.

Images are hard-linked into `<project>/images`, which is the layout ODM insists
on, so staging costs no extra disk. The exact docker command is written to
`<project>/odm_command.txt` and logged, so any run can be repeated by hand
without this tool. Full output goes to `<project>/odm_run.log` with ODM's colour
codes stripped.

`--rerun-from <stage>` resumes rather than starting over, which matters when a
run fails late. Stages are `dataset`, `split`, `merge`, `opensfm`, `openmvs`,
`odm_filterpoints`, `odm_meshing`, `mvs_texturing`, `odm_georeferencing`,
`odm_dem`, `odm_orthophoto`, `odm_report`, `odm_postprocess`.

**Outputs are verified before the next step trusts them.** A product counts only
if it exists, is non-empty, and carries a CRS. That last check matters because
ODM will happily produce an orthophoto in an arbitrary local frame when GPS is
missing, and such an output cannot be joined to a field or compared against
satellite imagery.

**Failures name the stage.** Rather than leaving you with a forty thousand line
log, a failed run reports where it stopped and what to do:

```
  FAILED during the 'opensfm' stage.
  Features were found in individual images but none matched across them, so
  nothing could be triangulated. That means either too little overlap, images
  from more than one area, heavy motion blur, or a surface too self-similar to
  match (bare soil, water, or uniform canopy).
```

Recognised signatures cover no cross-image matches, no reconstruction, thin
overlap, too few matched images, an empty images directory, missing
georeference, and out-of-memory (including a bare exit code 137).

**Memory sizing** is checked before a run starts, since ODM tends to fail late.
Rough guidance at medium quality: 100 images want 4 GB, 250 want 8 GB, 500 want
16 GB, 1500 want 32 GB.

## Canopy height model

```bash
dosojos-drone chm chm-rows
```

Writes `out/<flight_id>/chm.tif` and a colourised `chm.png`. The model is
`DSM − DTM`: how tall the crop is, as distinct from how high the ground is.

The arithmetic is trivial. Everything else in this step is about the ways a real
reconstruction is not:

- **Nodata** arrives as `-9999`, as NaN, or occasionally as a very large
  sentinel. All three become NaN.
- **Grids can differ.** ODM normally emits DSM and DTM on the same grid, but a
  changed `--dem-resolution` or a resumed run can leave them mismatched.
  Subtracting mismatched arrays either raises or silently broadcasts, so the DTM
  is resampled onto the DSM's grid with a warning when they disagree.
- **Sub-ground noise** is clamped to zero. Interpolation routinely puts the
  surface a few centimetres below the ground; that is not negative canopy.
- **Holes** smaller than `--fill-holes` (m², default 0.25) fill from their
  neighbours. Larger voids stay as nodata, because interpolating across a gap the
  size of a plant invents canopy nobody observed and every downstream volume
  would inherit it.
- **Spikes** above `--max-height` (default 8 m) are clipped. No field crop is
  that tall; sorghum tops out near 3 m and cane near 5 m.
- **Smoothing ignores NaN.** A plain Gaussian filter smears one hole across its
  whole kernel.

**Cleaning parameters are in ground units, not pixels.** This matters: with a
sigma in pixels, `--smooth 1` would mean 5 cm of smoothing on a 5 cm DEM and 2 cm
on a 2 cm one, so changing `--dem-resolution` between runs would silently change
how much canopy structure the cleaning destroys.

**Smoothing is the only meaningful source of error**, measured against a canopy
whose true heights are known:

| `--smooth` | RMSE | correlation |
|---|---|---|
| 0 m | 0.011 m | 0.99993 |
| 0.03 m (default) | 0.090 m | 0.99665 |
| 0.05 m | 0.178 m | 0.98803 |
| 0.10 m | 0.386 m | 0.96002 |

Bias is zero at every setting. The default is deliberately light because step 5
measures the row structure that smoothing flattens. On an orchard pattern, where
crowns are smooth domes rather than sharp ridges, the same default gives 0.017 m
RMSE.

`--clip-field` (on by default) masks everything outside the registered field
outline. ODM reconstructs whatever the flight saw, including headlands, roads and
the neighbour's crop, and statistics over that are not statistics about this field.

## Detection

```bash
dosojos-drone detect chm-rows  --method rows
dosojos-drone detect chm-trees --method watershed --min-height 0.6 --min-distance 2.5
```

Writes `out/<flight_id>/units_<method>.geojson` and an overlay PNG. Every method
returns polygons with stable ids and a CRS, so everything downstream is
indifferent to which one produced them.

**`rows` is primary, and the reason is arithmetic.** Sorghum plants sit about
15 cm apart in a 0.76 m row. At 5 cm that is three pixels, so individual plants
are not separable and "per-plant" would be a fiction. The row segment is the
smallest unit that can honestly be measured.

Row geometry comes from the canopy model's own periodicity: planted rows make
the canopy periodic across them, which is a single bright peak in the
two-dimensional power spectrum. The peak's angle gives the direction and its
frequency the spacing, without tracing any individual row. On a field with known
planting, spacing recovers to **0.29 cm** and direction to **0.26°**.

The phase matters as much as the spacing. Right spacing and angle with the wrong
phase puts every segment in the furrow between two rows, and every canopy volume
downstream then describes bare ground. Phase is found by folding the canopy
modulo the spacing and taking the peak; on the test field this puts 1.64 m of
canopy on the detected ridge against 0.11 m in the furrow.

**A peak always exists, so the reported strength is what says whether to believe
it.** Strength is the peak over the 99th percentile of the searched band, not
over its median, because the median comparison cannot tell rows from a merely
lumpy canopy:

| canopy | vs median | vs p99 |
|---|---|---|
| clean rows | 814000 | 404 |
| rows under heavy noise | 116 | 42 |
| lumpy, no rows | 340 | 4.4 |
| white noise | 3.6 | 1.4 |

A closed-canopy field with no visible rows scores 340 against the median and
would be reported as confident. The threshold is 10, in the gap.

`watershed` is for orchards: local maxima seed a marker-controlled watershed,
one polygon per crown. On the synthetic orchard it finds **90 crowns against 90
planted**, correctly ignoring all 10 empty positions.

`deepforest` is wired but **not installed**. It pulls PyTorch, roughly 2.5 GB,
and risks the working environment; its pretrained weights come from forest
canopy, so it is the wrong detector for row crops. Install it only when pointing
this at citrus:

```bash
pip install deepforest
```

## Metrics

```bash
dosojos-drone metrics chm-rows  --method rows
dosojos-drone metrics chm-trees --method watershed
```

Writes `out/<flight_id>/metrics_<method>.geojson` and a matching CSV, one row per
unit: area, maximum and mean canopy height, canopy cover, volume, data coverage,
and the mean VARI, ExG and GLI, each with a within-flight percentile rank.

**Volume integrates canopy height over the footprint** — the sum over a unit's
pixels of height times pixel area. Against ellipsoidal crowns whose volume is
exactly two thirds pi r squared h, it lands within **0.6%** (correlation 0.9999).
That is scored against an analytic answer on purpose: a voxel count and a CHM
integral are the same calculation at different discretisations, so comparing
them only shows they agree with each other.

`canopy_volume_voxel` is still provided, reading ODM's LAZ point cloud directly
via `laspy`. It fills each ground column from the ground to its highest point,
because photogrammetry sees surfaces rather than interiors and counting occupied
voxels would measure the canopy's skin. On a real flight it is an independent
check, since it never touches the interpolated raster.

**Structure and colour sit on different grids** — typically 5 cm and 2 cm — so
each unit is rasterised onto each grid separately rather than resampling one
raster onto the other. Everything is computed zonally in one pass per raster.

**ExG is computed on chromatic coordinates**, r = R/(R+G+B) and so on, which
removes overall brightness: a cloud shadow drifting across the field halves
every band and leaves ExG unchanged. Then every index is **ranked within the
flight**, because absolute RGB depends on camera, exposure, sun angle and haze,
and an ExG of 0.12 means nothing on its own.

On the synthetic sorghum field, structure and colour agree:

| truth | volume vs normal | median ExG | ExG percentile |
|---|---|---|---|
| normal | 100% | 0.277 | 55 |
| stunted | 64% | 0.164 | 6 |
| gap | 48% | 0.121 | 3 |

On the orchard, stressed trees hold 15% of a healthy tree's volume and sit at
the 7th greenness percentile against 57 for healthy ones.

## Flags

```bash
dosojos-drone flag chm-rows  --method rows
dosojos-drone flag chm-trees --method watershed
```

Writes `flags_<method>.geojson` (every unit with its flag and the reason that
decided it), `missing_<method>.geojson`, and `flags_<method>_summary.csv`.

| flag | rows | orchard |
|---|---|---|
| MISSING | segment with under 70% of the field's median canopy cover | empty position in the inferred planting grid |
| DEAD | canopy under 10% of the median, or greenness down 3+ sd *and* under 25% of the median | same |
| STRESSED | bottom 15% of height or greenness, *and* 20% below the median, *and* 1.5 sd below it | same, on volume |
| EDGE | segment clipped under 75% of a full one at the boundary: not judged | never |
| HEALTHY | everything else | |

Missing plants and dead plants call for different things, so they are reported
separately: missing means replanting, while dead standing plants point to
disease or drought. Along rows, consecutive missing segments merge into one gap
with its length, since a grower replants a stretch of row rather than a list of
two-metre tiles.

### Why the rules look the way they do

Each rule was measured against ground truth rather than chosen by hand, and
several obvious versions turned out to be wrong.

**Rows compare canopy height, not volume.** Volume grows with area, so a segment
clipped to half its length by the field boundary holds half the volume and reads
as stunted even when its canopy is healthy. That one effect caused 109 of the
first 214 false alarms. Mean canopy height doesn't depend on area, so clipping
can't move it. Crowns are the opposite case: a stressed tree grows a smaller
crown, so for an orchard volume is the signal.

**STRESSED needs two guards, not the bare percentile.** "Bottom 15%" alone always
flags 15% of a field, including a perfectly healthy one. The obvious fix is a
z-score guard, but it fails too: any z threshold also flags a fixed share of a
normal distribution, so a healthy field still lost 8.5% of its plants. A guard
on the shortfall below the median fixes that, but fails the opposite way on a
naturally variable orchard, where the smallest healthy tree already has a 31%
shortfall. Each guard covers the other's blind spot, and a genuinely stressed
plant fails both:

| guard | rows precision | rows recall | orchard errors | healthy field flagged |
|---|---|---|---|---|
| none (percentile only) | 56.8% | 97.1% | 6 of 90 | 28.0% |
| z < −1.5 only | 73.0% | 91.3% | 0 of 90 | 8.5% |
| 20% shortfall only | 71.1% | 91.3% | 3 of 90 | 0.5% |
| **both (default)** | **73.6%** | **90.3%** | **0 of 90** | **0.5%** |

**MISSING is relative, not absolute.** A full-width segment always includes the
furrow on each side, so even a perfect row reaches only about 57% canopy cover.
An absolute 10% threshold caught 1 of 74 true gaps. Gap segments carry 52% of
the median cover and stunted ones 88%, so the default cut of 70% falls between
them.

**DEAD by colour also needs an effect size.** On an even orchard, a tree at 43%
of normal greenness sits far past −3 sd and was being called dead while clearly
alive. It must now also have lost three quarters of the field's typical
greenness.

**The missing-tree search is a grid-aligned rectangle, not a convex hull.** When
a corner tree is missing, the hull cuts that corner off and the tree is never
reported. The corner's row and column still contain other trees, so a rectangle
in the grid's own frame keeps it in the search. The drawback is an L-shaped
orchard, where the rectangle would include the empty notch.

### Results against ground truth

Orchard: all 79 healthy trees HEALTHY, all 11 stressed trees STRESSED, all 10
missing positions found, no false positives. The planting grid recovers as
5.000 × 5.000 m at 0.0°.

Rows: 60 of 74 gap segments MISSING, 112 of 133 stunted segments STRESSED, and
97% of healthy segments HEALTHY. The remaining disagreements are mostly segments
that only partly overlap an anomaly, plus the ±12% natural variation built into
the synthetic rows.

Neither synthetic field contains a standing-dead plant, so the DEAD-by-colour
path is covered by unit tests only. It hasn't been validated end to end.

## Demo outputs and the satellite join

```bash
dosojos-drone report chm-rows  --method rows
dosojos-drone report chm-trees --method watershed
dosojos-drone join
```

`report` writes three things per flight into `out/<flight_id>/`:

- `flag_overlay.png` shows every unit's flag over the orthomosaic. Healthy units
  aren't drawn, so the photo shows through and the eye goes to the problems.
- `flag_histogram.png` shows the size distribution stacked by flag, with the
  flagged tail picked out. For crowns it plots canopy volume and for row
  segments mean canopy height, since row volume would add a false tail made of
  segments the boundary clipped short.
- `block_summary.json` is one field-level record with the keys the join needs:
  `field_id`, `n_trees`, `n_dead`, `n_missing`, `n_stressed`,
  `median_canopy_volume`, `mean_ExG`, plus shares and `unit_type`. For a row
  crop, `n_trees` counts row segments and `unit_type` says `row_segment`.

`join` reads the satellite pipeline's `../dosojos_sat/out/flags.json` and every
flight's block summary, and writes `out/triage.json`. The satellite ranking is
kept exactly, in the same order with every original key, and each field gets a
`drone` object and an `agreement` verdict:

| agreement | meaning |
|---|---|
| `confirmed` | satellite flagged it and the drone sees the problem plants |
| `not_confirmed` | satellite flagged it but the plants look mostly fine; **often a harvest**, which looks identical from orbit |
| `drone_only` | the drone found problems the satellite's 10 m pixels couldn't resolve |
| `both_clear` | neither sees a problem |
| `satellite_only` | no flight over this field yet |

The drone confirms a problem when at least 10% of judged units are flagged
(`--concern`). `not_confirmed` is the verdict that matters most. A harvested
field crashes NDVI exactly the way a stressed one does, and only the drone can
tell them apart.

**The demo verdicts pair real satellite data with synthetic drone data.** The
satellite flags come from real Sentinel-2 imagery. The drone "flights" are
fields I generated with deliberately planted anomalies, so when the drone
"confirms" rgv-003, that is a coincidence of the test data, not a finding. The
join works; its verdicts start meaning something once a real flight is run.

### Colour, and why each flag also has its own shape

Flags use the reserved status palette: good, warning, serious, critical.
Validated as a set, STRESSED amber and MISSING orange measure only 13.6 apart
for full colour vision, below the floor of 15, so hue alone can't separate
them. Each flag is therefore also drawn differently: stressed as a solid fill,
missing as a hatched outline with no fill (empty reads as absent), dead as a
crosshatched fill, and missing orchard trees as a ring with a cross.

### Row segments at the edge

Row segments cut by the field or reconstruction boundary are reported as EDGE
and not judged. A segment clipped across its row keeps the bare furrow and loses
the crop ridge, so its cover collapses and it reads as missing plants. On the
synthetic field, partial segments were flagged at 24% against 10% for whole
ones, and a column of false MISSING flags ran down the east edge, one per row.
Setting them aside raised precision from 71% to 78% with recall unchanged.
Shares in the block summary are over judged units, so boundary slivers don't
dilute them.

## The whole thing, start to finish

```bash
dosojos-drone register f1 --field rgv-002 --crop "grain sorghum" --ground-elevation 12 \
    --row-spacing 0.762                       # 30-inch rows; see "Row spacing" below
dosojos-drone survey  f1                      # worth running ODM?
dosojos-drone odm     f1                      # hours; or 'import' finished maps instead
dosojos-drone chm     f1
dosojos-drone detect  f1 --method rows        # or watershed for an orchard
dosojos-drone metrics f1 --method rows
dosojos-drone flag    f1 --method rows
dosojos-drone report  f1 --method rows
dosojos-drone terrain f1                      # is the ground level; does the stress follow it?
dosojos-drone join                            # merge with the satellite ranking and water
```

Every step reads the previous step's output from disk, so any step can be re-run
without redoing ODM.

## Ground and water (`terrain`)

Most Valley fields are watered down furrows. The water runs downhill along the
rows from a head ditch and soaks in as it goes. It fails in two ways. On uneven
ground, high spots stay dry and low spots pond. On rows too long or too flat for
the stream, the tail ends never get their share. `terrain` checks both from the
drone's ground model (the DTM) and the flags:

- **Grade.** A robust plane is fitted to the ground, ignoring ditches and stray
  points beyond 3 MAD. Its tilt is split into along the rows and across them.
- **Evenness.** What is left over is the relief. It reports the share within
  3 cm of the plane (laser-level tolerance), its spread, and the cut to level it.
- **Spots.** High and low spots are patches standing 4 cm or more off the plane,
  traced after 2 m smoothing.
- **Links.** Each judged row piece gets its height off the plane and its position
  from the head of the rows to the tail. Fisher's exact test then asks whether
  flagged pieces bunch at the tail, the head, on high ground, low ground, or any
  one spot. A link needs a 1.5x higher rate, p < 0.01 and at least 10 flagged
  pieces, so a small spot cannot pass on chance.
- **Advice.** Each finding gets plain-language advice, most urgent first:
  - leveling, with soil to move in yd³/acre
  - cutting down a high spot the water runs around
  - shorter runs, cutback or surge valves for dry row tails, adjusted for how
    fast the soil takes water (from the satellite's soil survey)
  - drainage for low spots
  - "not the water" when the stress follows neither the ground nor the row ends

The ground is judged only under the crop: the rows' footprint, or the outline
minus a 3 m headland. Borders and turn rows otherwise pass for spots.

The water's direction comes from the field's `water_enters` side in
`../dosojos_sat/fields.geojson` (N, S, E or W). Otherwise it is taken as
downhill along the rows. A level field with no side set skips the row-end test
and says so. `irrigation` on the field (furrow, flood, drip, none, ...) shapes
the advice: a rainfed field hears about drainage, not sets.

Outputs in `out/<flight>/`:

- `terrain.png`: the relief map with spots, flagged pieces and flow arrow, plus
  a bar chart of where the flagged pieces sit
- `terrain.json`: every number and the advice
- `ground_relief.tif`: cm above or below the plane, for a leveling contractor
- `terrain_spots.geojson`

Cautions it prints:

- A photogrammetry model can bow into a bowl or dome without ground control.
  Over 5 cm of curvature is flagged; confirm with GCPs or an RTK drone before
  leveling.
- Ground under a standing crop is interpolated. For leveling decisions, fly the
  field bare.

`python tools/make_synthetic_field.py <id> --pattern rows --water-pattern`
builds a field falling 0.15% north to south with one 9 cm high spot, and stunts
the crop at the row tails and on the spot. `terrain` recovers the 0.15% grade,
the spot (95% of its row pieces flagged against 14%), and the tail link (20% vs
11%). On Purdue's field, where the flags are harvest sampling, it finds no link
at all (14% at the tail vs 14% elsewhere), which is the right answer.

`join` now reads the satellite's `out/water.json` too. The table gains a WATER
column, and a **where to water first** list follows it. It gives each field's
days left and date, inches to put back (stored and delivered), the crop stage if
sensitive, and the top ground advice from its latest flight.

## Real data: the Purdue 2018 sorghum demo

Everything above was first tested on synthetic fields. To see it on real data
before our own flights exist, it was run end to end on a free public dataset:
Purdue University's *Geospatial Image Data for Sorghum Phenotyping* (PURR,
doi:10.4231/MY7W-FH43, CC0). It is a 72-plot trial of 18 sorghum hybrids at
Purdue's research farm in Indiana, flown on 10 July 2018 with an RGB camera
(1 cm) and a LiDAR scanner, plus a June 4 scan when the plants were small.

It lives apart from our data in `../public_demo/purdue-sorghum-2018/`, with its
own workspace for each half, and every figure carries a blue *FREE PUBLIC DATA*
band. `SOURCE.md` there has the citation and `run_demo.sh` every command; from
PowerShell, `.\run_demo.cmd` in that folder runs it through Git Bash (PowerShell's
own `bash` is WSL's, which cannot start the Windows tools).

The trial came with a built-in answer key: on 25 June Purdue harvested rows 8
and 9 of every plot in replications 1 to 3 for biomass, and left replication 4
alone. Scored against that:

| | |
|---|---|
| Segment centres vs Purdue's own mapped rows | 0.9 cm median offset |
| Harvested row segments flagged | 349 of 356 (98%) |
| Rows 8-9 in replication 4, not harvested | 1 of 116 flagged |
| Undisturbed rows | 1 of 2,327 flagged |
| Row 11 in replications 1-2, where Purdue removed plants for sampling | 44 of 120 flagged |

The satellite, judging the same field as of 10 July, did not flag it: the field
was greener than its 2019-2022 normal. That is the join's *drone found what
satellite missed* case, for real: two cut rows in twelve are invisible at 10 m.

What the real data changed in the code, each measured before it was kept:

- **Finished maps can be imported** (`import`), since Purdue delivered an
  orthophoto and LiDAR, not photos. ODM never ran on this dataset.
- **Known row spacing.** By July the canopy had closed over the rows: in the
  height model they stood only 1.7x above the surrounding spectrum, while the
  harvested strips, repeating every plot, stood 3-4x. The free search reported
  1.79 m for 0.76 m rows. Growers know their spacing, so it can be recorded once.
- **Crop sanity check.** A spacing implausible for the crop now prints a warning
  saying what to do.
- **Blocks.** A variety trial must be judged plot by plot; see below.
- **Row tracking.** Segments now follow the rows where they drift or shift; see
  below.

## Maps made elsewhere (`import`)

```bash
dosojos-drone import f1 --ortho ortho.tif --dsm late.las --dtm early.las --crs EPSG:26916
```

Writes the products where ODM would have, so every later step runs unchanged,
and records in `imported.json` that ODM did not build them. `--dsm`/`--dtm`
accept GeoTIFFs or LAS/LAZ point clouds. A cloud becomes a DSM by taking the
highest return per cell, and a DTM by taking the 5th percentile per 0.5 m cell
(or ground-classified points when present) from a bare-soil or early flight.
Surfaces are cropped to the orthophoto. A file without a coordinate system needs
`--crs`; a `--crs` that contradicts the file is refused. Existing products are
never overwritten without `--force`.

## Row spacing, and a closed canopy

Record the planter spacing with `register --row-spacing` (30 in = 0.762,
38 in = 0.965, 40 in = 1.016, 5 ft cane = 1.524), or pass `detect --spacing`.
Detection then only searches the direction and position of the rows, which it
still finds after the canopy closes. Without it, the free search is confirmed
on short strips along the rows, and a spacing outside the crop's usual range
(sorghum 0.35-1.05 m, cane 1.2-2.2 m) prints a warning.

## Row tracking

A straight comb of rows laid across a whole field goes wrong on real ground: a
quarter of a degree walks a row half a row sideways over 100 m, and planter
passes rarely meet at exactly the row spacing. Detection measures where the
rows sit in tiles of 8 rows by 5 m, after removing a running median that makes
plot edges and gaps drop out, then fits a plane plus one offset per band of
rows. It is only applied when it moves the rows more than 15% of a spacing;
otherwise one comb, measured over the whole field, is more precise.

On the synthetic field, tracking put 93% of segment centres on the rows instead
of 3% (the old comb sat in the furrows, which full-width segments hid) and
raised recall of affected segments from 75% to 85% at the same 99.6% precision.
A two-row offset between planter passes is followed exactly.

## Blocks

```bash
dosojos-drone detect f1 --blocks blocks.geojson --block-field variety
```

A block is any part of a field that should be judged on its own: a variety, a
planting date, a ratoon age, a trial plot. Units are cut at block edges and
dropped outside them (alleys), and `flag` then compares each unit only with its
own block. Judged across a mixed field, the spread of the two varieties hides
genuinely stunted plants of either; within blocks they stand out. Pass
`flag --whole-field` to compare across the flight anyway.

## Workspaces

`--workspace DIR` on either tool keeps its manifest, data, cache and outputs in
`DIR` instead of the project folder. Laid out as `DIR/dosojos_sat` and
`DIR/dosojos_drone`, the two halves find each other exactly as the projects do.
That is how the public demo stays out of our own `flights.json` and triage.

A flight registered with `--source` carries that source on every figure, in
`block_summary.json` and into `triage.json`.

## Video fallback

Use this only when video is all that exists.

```bash
dosojos-drone ingest-video vid-001 \
    --video data/video/demo.mp4 --srt data/video/demo.SRT --fps 2
dosojos-drone survey vid-001
```

**This path produces measurably worse reconstructions than stills.** Video frames
are heavily compressed, rolling-shutter distorted, and motion blurred in ways
stills are not. Feature matching has less to work with, so the point cloud is
sparser and the surface model noisier.

The resolution penalty is the part people underestimate. A Phantom 4 Pro shoots
5472 px stills but records 4K video at 3840 px, and many flights record at 1080p.
At the same altitude that is a 1.4× to 2.8× coarser ground sample distance before
any compression loss. On the synthetic test flight, 640×480 frames at 60 m give
14 cm/px against 1.6 cm/px for stills, and `survey` warns that this is too coarse
for individual plant work.

What the command does:

1. Extracts frames with ffmpeg at `--fps` (default 2/s — at survey speed that
   already yields more forward overlap than a stills flight).
2. Scores each frame by variance of the Laplacian, a standard sharpness measure.
3. Drops the blurriest `--blur-quantile` (default 15%), floored so a uniformly
   soft video still loses its worst frames. `--blur-threshold` sets an absolute
   cut-off instead. The cut is relative by default because absolute sharpness
   depends on scene content.
4. Writes EXIF GPS, altitude, timestamp and lens fields onto the survivors from
   the SRT, matching each frame to the telemetry record covering its moment.

Both DJI SRT layouts are handled: the current bracketed
`[latitude : x] [rel_alt: y abs_alt: z]` form and the older
`GPS(lon,lat,sats)` form. Note that a single bracket can carry two pairs, which
is easy to get wrong and leaves every frame without an altitude.

Without an `--srt`, frames are still extracted but carry no GPS, so nothing is
georeferenced and nothing joins to a field.

## Testing without an SD card

Two generators produce realistic input over a real field polygon from the
satellite project:

```bash
python tools/make_synthetic_flight.py demo-001 --field rgv-002 --count 200
python tools/make_synthetic_video.py --out data/video/demo.mp4 --seconds 30
python tools/make_synthetic_field.py chm-rows --pattern rows
python tools/make_synthetic_field.py chm-trees --pattern trees
```

`make_synthetic_field.py` writes a complete ODM product set — DSM, DTM,
orthophoto and point cloud placeholder — in the exact layout ODM produces, so
the rest of the pipeline cannot tell the difference. It also writes
`truth_canopy.tif` and `ground_truth.json`, so canopy heights and later
detections can be scored against an exact answer rather than eyeballed.

Two patterns: `rows` for sorghum and cane (continuous rows with gaps and stunted
stretches) and `trees` for orchard and citrus (discrete crowns with missing and
undersized individuals).

The stills generator accepts `--drop-gps`, `--forward-overlap` and
`--altitude-jitter` for exercising the warning paths. Frames are written at a
reduced pixel count so a few hundred stay small; footprint and overlap are
unaffected by that, but reported GSD is coarser than the real camera's.

```bash
./.venv/Scripts/python.exe -m pytest -q
```

282 tests, none needing Docker, a network, or real imagery.

## Layout

```
flights.json          flight_id -> field_id, the join to the satellite half
data/raw/<flight>/    input JPEGs
data/odm/<flight>/    ODM products
out/<flight>/         quicklook, survey.json, later the crowns and overlays
src/
  config.py           paths, settings, flight manifest
  ingest.py           EXIF reading, survey geometry, coverage, pre-flight checks
  chm.py              DSM - DTM, cleaning, field clipping
  crowns.py           row geometry, segmentation, watershed crowns
  metrics.py          zonal volume, height, cover, RGB indices
  flags.py            classification, planting grid, missing plants, gaps
  terrain.py          ground grade and evenness, spots, stress links, irrigation advice
  report.py           flag overlay, histogram, terrain map, block summary, satellite join
  odm_runner.py       docker invocation, staging, verification, diagnosis
  video.py            MP4 + SRT -> geotagged JPEGs
  viz.py              quicklook now, overlays and histograms later
  cli.py              click commands
tools/                synthetic flight and video generators
```

`config.py`, `video.py` and `viz.py` are additions to the original module plan.

## How the two halves join

The drone side reads `../dosojos_sat/fields.geojson` by path to get field
outlines and water settings, and `../dosojos_sat/out/flags.json` and `water.json`
for the join. It shares nothing else. There is no import between the codebases.
The join key is the `field_id` string recorded in `flights.json`, which is the
same identifier the satellite pipeline writes into its outputs.
