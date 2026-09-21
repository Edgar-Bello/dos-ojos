# The uploads

Three public datasets, already shrunk to a size that uploads over a home
connection, for anyone who wants to put our pipeline through its paces without
downloading the originals first. **None of this is a Dos Ojos farm.** Every one
is a public research field, and each folder carries a `SOURCE.txt` with the
citation, the DOI, the licence and exactly what was changed to make it smaller.

| Upload | What it is | Feeds |
|---|---|---|
| `Dan_Field_drone_map_Purdue_2018_public.tif` | A finished drone map of Purdue's field 54: 72 plots of sorghum, flown 10 July 2018 (CC0) | The drone demo. Its harvest answer key is what scored the row flags |
| `Edgar_Field_citrus_photos_USDA_2021_public/` | 46 raw drone photos of a citrus grove at Fort Pierce, flown 12 May 2021 (US public domain), each keeping its GPS position and heading | Photogrammetry from scratch: 46 photos in, a map and 206 trees out |
| `Edgar_Field_thermal_frames_TERRAREF_2018_public/` | 2,716 real radiometric thermal frames of sorghum, scanned 20 May 2018 (public domain), quarter width, 130 MB instead of 3.3 GB | The thermal demo: the temperatures and each frame's ground position are unchanged |

The names are the two of us because these are what we upload when we show the
system to someone: each of us plays a farmer with a field. The farmers are made
up and their numbers are fictional 555 ones; the fields are real and public.

## Using them

Send `FOTO` from the demo conversation and the web page will offer an upload
box; hand it the folder or the file above. Or point the drone pipeline straight
at one:

```bash
./.venv/Scripts/python.exe -m src.cli chm --flight <id>
```

The three demos in `dosojos_sms/examples/` (`setup_drone_demo.cmd`,
`setup_thermal_demo.cmd`, `setup_sms_demo.cmd`) set up the conversation each of
these belongs to, pinned to the day after its flight so the answers never drift.

For the full-size originals and the commands that produced our published
numbers, see `public_demo/*/SOURCE.md`.
