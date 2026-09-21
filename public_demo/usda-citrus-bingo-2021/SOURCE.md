# FREE PUBLIC DATA, NOT A DOS OJOS FIELD

A citrus grove flown by a USDA research team in Florida, not in the Rio Grande
Valley. Everything here comes from, or will be computed from, their public
dataset. It is here because we can't fly drones yet (no license) and still
need to show the drone half on a tree crop, not only on row crops. It must
never be mixed with flights we collect.

## The dataset

Niedz, R. P., and Bowman, K. D. (2024). *UAV image and ground data of citrus
'Bingo' mandarin hybrid (Citrus reticulata, Blanco) rootstock trial.* Ag Data
Commons. doi:10.15482/USDA.ADC/26946823.v1

- **Paper:** Niedz and Bowman (2024), *Data in Brief* 111206, doi:10.1016/j.dib.2024.111206
- **License:** U.S. Public Domain. Citation is given as a courtesy.
- **Where:** USDA-ARS U.S. Horticultural Research Laboratory, Picos Road farm, Fort Pierce, Florida (27.4371, -80.4267).
- **What:** 206 'Bingo' mandarin trees on 14 rootstocks, planted in 2018 in four rows.
- **The flight:** DJI Phantom 4 Pro at 46 m, 80% side and 70% forward overlap, 12 km/h, 2021.
- **Downloaded (all 48 files, 417 MB):**
  - `download/DJI_*.JPG`: the 46 drone photos, 5472 x 3648 RGB, geotagged
  - `download/*labeled*.png`: the team's composite with rows and tree numbers marked
  - `download/Bingo ground data_Rows 1-4_2021.xlsx`: planting plan and per-tree ground measurements (canopy height, width north-south and east-west, health 0 to 5, April 2021)
  - `download/article.json`: the repository's record of the dataset, with checksums

## The answer key it came with

The spreadsheet measures every tree by hand a few weeks before the flight. So the
drone half's per-tree results can be checked against a real answer key — tree
height against canopy height, and how many trees we find against how many are
there. `check_answer_key.py` does it and prints the score; see the next section
for what it came to. This is the only place in Dos Ojos where we can say how wrong
we are in centimetres instead of saying a result looks about right.

There is no satellite side here. The trial is four rows of trees, about 20 m
by 150 m, only a couple of Sentinel-2 pixels wide. So `survey` warns that it has
no field outline to check coverage against. That warning is expected.

## What the flight scored, 2026-09-16

The whole run takes about half an hour: ODM 27 min on 46 photos at high quality,
everything after it under a minute. `check_answer_key.py` scores the result against
the team's tape measure and writes `out/PUBLIC-usda-bingo-20210512/answer_key.png`.
`run_demo.sh` ends with `dosojos-drone page`, which puts the lot on one file —
and this field is the reason that command exists, because it has no satellite
record to show and says so.

To place a tree without consulting the key, the check registers the planting
lattice on the crowns we found — the rows come out 7.7 m apart, the trees 2.13 m
apart, 0.35 deg off north, and a crown then sits 24 cm from a planting spot on
average. The key's heights are used to score a tree, never to place one.

**Height: right order, short — and point-cloud quality is most of it.**

The flight was run twice, at `--pc-quality medium` and then at `high`:

| | medium | **high** |
|---|---|---|
| measured by hand | 1.85 m mean (0.76 to 2.48) | same |
| from our canopy model | 1.11 m | **1.46 m** |
| **short by** | 0.74 m, 40% | **0.39 m, 21%** |
| typical miss | 0.67 m | **0.33 m** |
| within 25 cm | 0% | **26%** |
| correlation | 0.78 | 0.77 |
| trees found as crowns | 132 of 216 (61%) | **149 of 216 (69%)** |
| ODM run | 17.8 min | 27 min |

**Quality halves the error and costs ten minutes on 46 photos.** Use `high` on tree
crops. It ranks the trees correctly at either setting and reads every one short by
about the same amount — the scatter sits parallel to the 1:1 line and under it.

Our own `chm` step is not the cause: its only smoothing is a 3 cm sigma, far too
small to shave a 1.7 m crown. Splitting the remaining shortfall by taking the ground
from the grass alley instead of from under the tree:

- **0.31 m** is the **surface model not reaching the top of a citrus crown**. Fine
  leaves against sky reconstruct poorly. This is now nearly all of it.
- **0.08 m** is the **ground model creeping up under the row**, down from 0.25 m:
  the denser cloud finds ground points under the canopy that the sparse one missed.

**Treat tree heights on a tree crop as relative, not absolute** — good for "which
tree is smallest", not for "how tall is this tree".

**Counting trees: a floor, not a census.** At 2.13 m apart these three-year-old
trees have grown into each other, and the watershed cuts one crown where two touch.
Per row at high quality: 73%, 67%, 63%, and 78% on the short row 4.

**The false alarm, and the fix.** `flag` infers the planting grid from the crowns it
is given, and merged crowns doubled the spacing it measured: 4.32 m where the trees
stand 2.13 m apart. It then reported **272 missing trees, 49.2%** for a grove that
is nearly fully planted, and the overlay was a blizzard of orange rings over the
alleys and the farm road. `flag` now refuses to count when more than half the grid
it derived comes up empty, and says why. On this flight it refuses at 82%, and the
overlay reads **23 of 324 trees need a look**.

One thing the refusal does not fix: **19 trees called DEAD against 7 in the key**,
several of them on the canal bank at the edge of the reconstruction. Edge crowns
are worth a rule of their own.

## Where it stands

- **Done:**
  - `register` put the flight in `dosojos_drone/flights.json`, as `PUBLIC-usda-bingo-20210512` on field `PUBLIC-usda-bingo`.
  - The photos are hard links into `data/raw/`, so there's no second copy.
  - `survey` checked them: 46 geotagged photos, flying height about 46 m above takeoff, 1.25 cm pixels, 78% forward overlap, fit for OpenDroneMap. See `out/PUBLIC-usda-bingo-20210512/quicklook.png`.
  - **OpenDroneMap ran** (2026-09-16): orthophoto, DSM, DTM and point cloud, all georeferenced, then `chm`, `detect --method watershed`, `metrics`, `flag` and `report`. Scored above.
- **Docker:**
  - **If Docker Desktop won't start** (fixed 2026-09-15, written down in case it comes back):
    it leaves behind empty Unix socket files when it crashes, and on the next start it
    cannot rename them out of the way: *"listening on unix://...sailor-ingest.sock: rename
    ... .stale: The file cannot be accessed by the system"*. Windows cannot open, move or
    delete those files either, so neither Explorer nor `Remove-Item` nor `rm` in Git Bash
    clears them. **WSL can.** With Docker Desktop closed:

    ```
    wsl -d Ubuntu -e sh -c "rm -f /mnt/c/Users/edgar/AppData/Local/Docker/run/* /mnt/c/Users/edgar/AppData/Local/docker-secrets-engine/*.sock"
    ```

    then start Docker Desktop. It crashes once per leftover socket, each time naming the
    next one in the log at `%LOCALAPPDATA%\Docker\log\host\com.docker.backend.exe.log`, so
    repeat until it comes up. There were three: two in `Docker\run` and one in
    `docker-secrets-engine`.
  - **Download:** the first `odm` downloads the OpenDroneMap image (2.4 GB) once. It is
    already on this computer.

## Layout

```
download/             the files exactly as downloaded
dosojos_drone/        drone workspace: flights.json, data/raw/<flight>/ (links to the photos), out/
check_answer_key.py   scores the flight against the team's tape measure
run_demo.sh           every command, in order (Git Bash)
run_demo.cmd          runs run_demo.sh from PowerShell: .\run_demo.cmd
```
