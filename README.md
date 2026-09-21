# Dos Ojos

Tells a Rio Grande Valley farmer, by text message, how many days of water each
of their fields has left and what to do first. The answer is a soil water
balance (FAO-56) run from free Sentinel-2 imagery, public weather and soil
data, and what the farmer texts about their own field.

A farmer who owns nothing but a phone gets the whole satellite side. A farmer
who can fly a drone gets a second pair of eyes at 60 metres. Hence the name.

Edgar Bello and Eduardo Bello.

## The three halves

Three Python packages that share no code. They meet at one string, the field's
id: the drone half reads the satellite half's files by path, and nothing is
imported across.

| Folder | What it is | Start here |
|---|---|---|
| `dosojos_sms/` | The front door: the conversation with the farmer, the web pages, the daily alerts, the local AI | [README](dosojos_sms/README.md) |
| `dosojos_sat/` | Sentinel-2 imagery, weather, soil, and the water checkbook | [README](dosojos_sat/README.md) |
| `dosojos_drone/` | Photogrammetry, canopy height, per-plant flags, terrain, thermal, 3D | [README](dosojos_drone/README.md) |

Each README documents its own half in full, including what was measured and
what the limits are. They are the real documentation; this page is the map.

## On drones, precisely

The drone half is built and tested end to end. **We have flown nothing
ourselves** — we do not hold an FAA Part 107 certificate yet. Every drone
result in this repository was produced by running our pipeline on public UAV
datasets that came with their own ground truth, which is what let us score it
instead of eyeballing it.

`public_demo/` holds one folder per dataset. Each has a `SOURCE.md` with the
citation, the DOI, the licence, exactly what was downloaded, and what the run
came to. The full-size imagery is not in this repository (about 12 GB); the
`SOURCE.md` says where to get it.

| Dataset | What it gave us |
|---|---|
| Purdue sorghum 2018 (doi:10.4231/MY7W-FH43, CC0) | A 72-plot trial with a built-in harvest answer key |
| USDA-ARS citrus, Fort Pierce (doi:10.15482/USDA.ADC/26946823, public domain) | 206 trees measured by hand: height, width, health |
| TERRA-REF Maricopa (doi:10.5061/dryad.4b8gtht99, public domain) | 2,719 real radiometric thermal frames of sorghum |
| USDA Cropland Data Layer, USGS 3DEP lidar | Real Valley fields of four crops, and their ground |

## Where the dataset is

**`share/`.** The three datasets themselves, shrunk to a size that uploads over
a home connection and ready to run: Purdue's finished drone map, the 46 raw
citrus photos, and 2,716 of the 2,719 radiometric thermal frames. 218 MB in
all, against 12 GB for the originals, and enough to put the drone,
photogrammetry and thermal paths through end to end without downloading
anything first.

Each folder carries its own `SOURCE.txt` with the citation, the DOI, the licence
and exactly what was done to make it smaller. Temperatures and ground positions
are unchanged. See [share/README.md](share/README.md) for what feeds what.

## Some results worth checking

Scored against ground truth, not eyeballed:

- Row segment centres sat **0.9 cm** from Purdue's own mapped rows.
- **349 of 356** harvested row segments flagged; **1 of 2,327** undisturbed rows
  flagged. The satellite, judging the same field the same day, did not flag it
  at all: two cut rows in twelve are invisible at 10 m.
- A trained tree model found **96-98%** of citrus trees on rows it never saw in
  training, against 61-68% for the classical watershed, with heights within
  **10-14 cm** of the tape measure instead of 33-67 cm.
- Canopy volume lands within **0.6%** of the analytic answer on known crowns.

## Running it

Python 3.11 or 3.12 (the geospatial stack has no wheels for 3.13+). Each half
installs on its own:

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
./.venv/Scripts/python.exe -m pip install -e . --no-deps
./.venv/Scripts/python.exe -m pytest -q
```

About 1,600 tests across the three. None needs the network, Docker, a GPU or
real imagery, so the suite runs anywhere in a couple of minutes.

The AI is a local model through [Ollama](https://ollama.com)
(`ollama pull llama3.2:3b`), so nothing a farmer says leaves the farm's
computer and there is no API key. `DOSOJOS_AI=0` turns it off; every answer is
then the water checkbook's, as it was before.

## What is not here

- **Anything a farmer told us.** Their databases, phone numbers, photos and
  field outlines stay on the farm's own computer. Every phone number in this
  repository is a fictional 555 test number.
- **Keys.** `sms.env.example` shows the shape; the real file is git-ignored.
- **The full-size public imagery**, for size. `share/` carries a shrunk copy you
  can run today; `public_demo/*/SOURCE.md` says where each original lives.
