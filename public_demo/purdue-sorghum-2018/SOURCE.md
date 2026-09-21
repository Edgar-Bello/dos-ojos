# FREE PUBLIC DATA, NOT A DOS OJOS FIELD

Everything in this folder comes from, or was computed from, a public research
dataset. None of it is our data, none of it is from the Rio Grande Valley, and
it must never be mixed with flights we collect. It exists to show the pipeline
working on real imagery before our own flights exist.

## The dataset

Tuinstra, M. R., Crawford, M. M., Delp, E. J., Habib, A. F., Cherkauer, K. A.,
and Biehl, L. L. (2021). *Geospatial Image Data for Sorghum Phenotyping.*
Purdue University Research Repository. doi:10.4231/MY7W-FH43.
<https://purr.purdue.edu/publications/3167/1>

License: CC0 1.0 Universal (public domain). Citation is given as a courtesy.

- **Where:** field 54, Purdue's Agronomy Center for Research and Education (ACRE),
  West Lafayette, Indiana. UTM zone 16N, NAD83 (EPSG:26916).
- **What:** the "Hybrid Calibration" trial, with 18 commercial sorghum hybrids x 4
  replications = 72 plots, each 12 rows on 0.762 m (30 in) spacing, sown 8 May 2018.
- **Downloaded (2.65 GB of the 3.75 GB bundle, on 10 Sep 2026):**
  - `download/2018/uav/rgb/20180710/...1cm_lidar_nad83.tif`: drone RGB orthophoto, 1 cm, 10 July 2018
  - `download/2018/uav/lidar/20180710/20180710_field54_dsm_4cm.las`: drone LiDAR surface, 10 July
  - `download/2018/uav/lidar/20180604/20180604_field54_dsm_4cm.las`: drone LiDAR surface, 4 June (plants small; used as ground)
  - `download/2018/ground_reference/`: plot and row outlines, hand-measured plant heights, stand counts, Purdue's readme
  - Not downloaded: hyperspectral mosaics, the 1 August RGB.

## The answer key it came with

Purdue's readme records destructive sampling: on 25 June 2018 rows 8 and 9 of
every plot were machine-harvested for biomass in replications 1-3; replication 4
was not harvested because of weather. Plants were also removed from row 11 for
sampling in June. The 10 July flight therefore shows known, deliberate gaps. They
are research sampling, not crop stress, and that is how to read the flags here.

## Satellite side

Sentinel-2 L2A over the same field 54 (Earth Search, free), 2018-2022. The field
outline (`dosojos_sat/fields.geojson`) was drawn from the LiDAR canopy extent,
not supplied by Purdue. The "normal" is 2019-2022, years in which a research farm
may well have grown other crops there, so the satellite verdict illustrates the
mechanism more than it judges this field.

## Water checkbook and ground

- **Weather:** gridMET (University of Idaho), daily reference ET and rain at
  the field for 2018, fetched by `dosojos-sat weather`.
- **Soil:** USDA SSURGO under the field outline, fetched by `dosojos-sat soil`.
  Mostly Chalmers silty clay loam, 2.3 in of water per foot.
- **Field log** (`dosojos_sat/field_log.csv`): only the sowing date, 8 May
  2018, from Purdue's readme. The dataset records no irrigation, so the field
  is set to `irrigation: none` (rainfed). That is our assumption, not
  Purdue's record.
- **Result as of 10 July:** 6.7 in of water in the root zone, 2.7 in above the
  stress point. About 13 days (9-18) until rain was needed, at boot to
  flowering. Only about 0.5 in of rain fell until 29 July-2 August.
  Re-running as of 17 July gives "by 24 July", a day off the 10 July
  projection. That is a consistency check, not proof of stress: Purdue
  recorded none.
- **Ground:** the drone's ground model is the 4 June laser scan (plants small).
  Under the plots, the field falls 0.45% north to south, and 68% of it lies
  within 3 cm of a smooth plane. The flags follow neither the ground nor the
  row ends (14% at the tail vs 14% elsewhere), which is right for harvest
  sampling. The plot layout shows as faint stripes of a few cm in the relief
  map, probably young plants in the scan.

## The one thing to open

```
dosojos_drone/out/PUBLIC-purdue-20180710/page.html
```

`run_demo.sh` ends with `dosojos-drone page`, which puts both halves on a single
self-contained file: what the satellite saw, this season against the field's own
normal, the water checkbook, then the flight - canopy height, plant by plant, how
the field is spread, and the lie of the land. The pictures live inside the file,
so it can be saved or forwarded and still works with no signal.

**This is the demo to look at first**, because it is the only public one with both
eyes open. The USDA citrus grove next door has no satellite record - four rows of
trees are a couple of Sentinel-2 pixels - and its page says so and stands on the
drone half alone. Between the two you can see what each eye adds.

## Layout

```
download/        the files exactly as downloaded
dosojos_sat/     satellite workspace: fields.geojson, cache/, out/
dosojos_drone/   drone workspace: flights.json, data/odm/ (imported maps), out/
run_demo.sh      every command, in order (Git Bash)
run_demo.cmd     runs run_demo.sh from PowerShell: .\run_demo.cmd
```
