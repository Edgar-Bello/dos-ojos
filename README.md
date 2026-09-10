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
| 5 | Plant/row detection | not started |
| 6 | Per-plant metrics and RGB indices | not started |
| 7 | Dead / missing / stressed flags | not started |
| 8 | Overlay, histogram, block summary JSON | not started |

## Setup

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
./.venv/Scripts/python.exe -m pip install -e . --no-deps
```

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

102 tests, none needing Docker, a network, or real imagery.

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
  odm_runner.py       docker invocation, staging, verification, diagnosis
  video.py            MP4 + SRT -> geotagged JPEGs
  viz.py              quicklook now, overlays and histograms later
  cli.py              click commands
tools/                synthetic flight and video generators
```

`config.py`, `video.py` and `viz.py` are additions to the original module plan.

## How the two halves join

The drone side reads `../dosojos_sat/fields.geojson` by path to get field
outlines, and shares nothing else. There is no import between the codebases. The
join key is the `field_id` string recorded in `flights.json`, which is the same
identifier the satellite pipeline writes into `out/flags.json`.
