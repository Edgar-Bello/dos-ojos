# FREE PUBLIC DATA, NOT A DOS OJOS FIELD

Everything in this folder comes from, or was computed from, a public research
dataset. None of it is our data, none of it is from the Rio Grande Valley, and it
must never be mixed with flights we collect. It exists because it is, as far as
we can find, **the only free radiometric thermal imagery of sorghum there is**,
and the thermal option could not otherwise be shown running on anything real.

It replaces the made-up mosaic in `public_demo/thermal-synthetic`, which stays
only as a test pattern with a known answer in it.

## The dataset

LeBauer, D.S., Burnette, M.A., Demieville, J., Fahlgren, N., French, A.N.,
Garnett, R., Hu, Z., Huynh, K., Kooper, R., Li, Z., Maimaitijiang, M., Mao, J.,
Mockler, T.C., Morris, G.S., Newcomb, M., Ottman, M., Ozersky, P., Paheding, S.,
Pauli, D., Pless, R., Qin, W., Riemer, K., Rohde, S., Rooney, W.L., Sagan, V.,
Shakoor, N., Stylianou, A., Thorp, K., Ward, R., White, J.W., Willis, C., and
Zender, C.S. (2020). *TERRA-REF, An Open Reference Data Set From High Resolution
Genomics, Phenomics, and Imaging Sensors.* Dryad Digital Repository.
doi:10.5061/dryad.4b8gtht99

**Public domain.** The first TERRA-REF data release is in the public domain;
citation is a courtesy and is given above. <https://terraref.org/data/access-data.html>

- **Where:** the Field Scanalyzer at the University of Arizona Maricopa
  Agricultural Center, Maricopa, Arizona. A gantry on rails carries the sensors
  over a strip of field about 24 m wide; the outline in
  `dosojos_sat/fields.geojson` was drawn by us from USGS aerial imagery, not
  supplied by TERRA-REF.
- **What:** season 6, grain sorghum, **planted 2018-04-20, harvested 2018-08-02**
  (docs.terraref.org). It is a **variety trial**: hundreds of different sorghum
  lines side by side, which matters for how the warm patches must be read (below).
- **The camera:** FLIR SC615 on the gantry, about 2 m above the crop, writing one
  640 x 480 frame roughly every 1.5 s, each already georeferenced, each pixel a
  temperature in Kelvin. `ir_geotiff`, Level 1.
- **Downloaded** (by the account's owner, over Globus, 17 Sep 2026): the whole of
  **2018-05-20, 09:22 to 10:47 local**, 3,242 capture folders, **2,719 frames,
  3.3 GB**, in `download/ir_geotiff/`. Three frames are truncated and are
  skipped with a note when the scan is stitched.
- **Not downloaded:** the colour (`stereoTop`) and laser (`scanner3DTop`) sensors
  for the same window, which run to tens of gigabytes. That absence is the single
  biggest limitation here; see below.

## What the scan covers

The 2,719 frames are 62 east-west passes, 45 frames each, stepping south 1.05 m
at a time and covering **24 m by 83 m, about half an acre**, in 84 minutes. Each
frame is 1.3 m by 1.8 m of ground at **2.7 mm per pixel**, so the plants are
resolved leaf by leaf. Thirty days after sowing, the crop covered about **18% of
the ground** (measured below), which is what a 30-day sorghum looks like.

## Two things had to be solved before any of it meant anything

Both are in `dosojos_drone/src/mosaic.py`, and both apply to a drone's thermal
camera as much as to a gantry's.

1. **Which pixels are leaves.** The colour half tells leaves from soil with a
   canopy height model, and this flight has no colour camera. So leaves are told
   by contrast instead: a pixel at least **3 °C cooler than the ground
   immediately around it** is a leaf. Sunlit soil that morning ran 5 to 12 °C
   above the crop, and ground in a plant's shadow only 1 to 2 °C below the
   sunlit soil, so the rule separates leaf from shade cleanly. **What it costs:**
   a plant so far gone that it has stopped cooling itself reads as soil and is
   left out - and that is the plant worth finding. The report says so on its
   face, and a canopy height model wins whenever a flight has one.
2. **The sun climbing while the camera worked.** Over the 84 minutes the soil
   warmed about 9 °C and the crop about 6 °C, north to south, in step with the
   scan. Uncorrected, the half scanned last is one enormous warm patch. On top of
   that an uncooled thermal camera's own reading wanders as it works: two passes
   over the same ground disagreed by **1 to 2 °C**. Both are taken out by
   **lining every frame up with the frames it overlaps** and holding the whole
   set to the warming trend read from the scan itself.

## What it comes to

```
frames used      2,716 (3 unreadable)
the scan took    84 minutes
ground covered   1,875 m2, 338 m2 of it leaf   (18% cover, 30 days after sowing)
crop warmed      5.9 C while it ran
frames lined up  0.5 C for the middling frame, 3.0 C for the worst
canopy median    35.7 C
```

The satellite half, fetched separately for the same field, agrees with the
documented season and with the thermal scan: Sentinel-2 NDVI sits at bare-soil
values through April and May (**0.11 on scan day**, consistent with 18% cover),
climbs from 28 May, peaks at 0.50 in late June, and is back to bare soil by
3 August, the day after the recorded harvest. **On the day of this scan the
satellite could see almost nothing and the thermal camera could see every
plant** - which is the clearest statement of what the thermal option is for.

## How to read the warm patches here, and how not to

This is a variety trial. Hundreds of different sorghums, each in its own small
plot, differ in how fast they close their stomata; the strongest pattern in the
map repeats at **2.6 to 4 m, the plot spacing**, not at anything the scanner
does. So a warm patch in this field is **at least as likely to be a different
variety as a sick plant**. On a grower's own field, planted to one hybrid, that
ambiguity does not arise - but nothing in the data proves that here, and the
score must not be read as if it did.

Two further limits, both stated in the report the step writes:

- **A warm stretch wider than the camera's own view cannot be told from the
  camera drifting**, and is levelled away with it. A field running hot all over
  has to come from the water checkbook, not from this map.
- **There is no ground model and no colour camera** for this flight, so two of
  the three things the pest score leans on are missing. It is a ranking of
  places to walk to, and a thin one.

## Running it

```
run_demo.cmd
```

from PowerShell, or `bash run_demo.sh`. It stitches the frames (about three
minutes), scores the warm patches, and writes the page. Nothing is downloaded
except the satellite imagery and weather, which are cached after the first run.

## Where the farmer-facing demo uses it

`dosojos_sms/examples/setup_thermal_demo.cmd` builds a text-message demo on this
same field and flight, pinned to 2018-05-20, so the thermal option can be tried
end to end: a farmer texts in, uploads the scan, and gets the answer back.
