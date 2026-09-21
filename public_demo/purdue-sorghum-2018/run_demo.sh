#!/usr/bin/env bash
# Re-run the Purdue 2018 public-data demo end to end. FREE PUBLIC DATA, NOT OURS.
# Runs from any folder, in Git Bash (from PowerShell: run_demo.cmd). The download is
# not repeated; see SOURCE.md.
set -euo pipefail
if [[ -n "${WSL_DISTRO_NAME:-}" ]]; then
    echo "This is WSL's bash (what 'bash' starts in PowerShell); it cannot run the Windows tools." >&2
    echo "From PowerShell run run_demo.cmd instead, or run this script in a Git Bash window." >&2
    exit 1
fi
cd "$(dirname "${BASH_SOURCE[0]}")/../.."   # every path below is relative to Dos_Ojos/

DEMO=public_demo/purdue-sorghum-2018
# Through Python: Windows Smart App Control blocks the unsigned dosojos-*.exe launchers.
SAT="dosojos_sat/.venv/Scripts/python.exe -m dosojos_sat --workspace $DEMO/dosojos_sat"
DRONE="dosojos_drone/.venv/Scripts/python.exe -m dosojos_drone --workspace $DEMO/dosojos_drone"
DL=$DEMO/download/2018
FLIGHT=PUBLIC-purdue-20180710
SOURCE="Purdue University, PURR doi:10.4231/MY7W-FH43, CC0"

# Satellite: field 54, Sentinel-2 2018-2022, judged as of the flight date.
$SAT init-fields $DEMO/dosojos_sat/fields.geojson
$SAT fetch --start 2018-01-01 --end 2022-12-31
$SAT baseline --season 2018 --history 2019-2022
$SAT score --season 2018 --as-of 2018-07-10
$SAT chart --season 2018 --as-of 2018-07-10 --index all \
    --banner "FREE PUBLIC DATA, NOT OUR FIELD  -  Sentinel-2 over Purdue University field 54 (drone data: PURR doi:10.4231/MY7W-FH43, CC0)"

# Water checkbook: gridMET weather, USDA soil survey, sowing date from Purdue's readme.
$SAT weather --season 2018
$SAT soil
$SAT water --as-of 2018-07-10 \
    --banner "FREE PUBLIC DATA, NOT OUR FIELD  -  Purdue University field 54 (PURR doi:10.4231/MY7W-FH43, CC0); weather gridMET, soil USDA SSURGO"

# Drone: Purdue's finished maps imported in place of ODM, judged plot by plot.
$DRONE register $FLIGHT --field PUBLIC-purdue-f54 --date 2018-07-10 \
    --crop "sorghum, 18-hybrid trial" --row-spacing 0.762 --source "$SOURCE" \
    --notes "Hybrid Calibration trial, 72 plots of 18 commercial hybrids x 4 reps. Not a Dos Ojos flight." --force
$DRONE import $FLIGHT --force --crs EPSG:26916 \
    --ortho $DL/uav/rgb/20180710/20180710_f54e_Hybrid_Calibration_1cm_lidar_nad83.tif \
    --dsm $DL/uav/lidar/20180710/20180710_field54_dsm_4cm.las \
    --dtm $DL/uav/lidar/20180604/20180604_field54_dsm_4cm.las
$DRONE chm $FLIGHT
$DRONE detect $FLIGHT --segment 1.0 \
    --blocks $DL/ground_reference/vector_files/plot_hybrid_2018.geojson --block-field plotId
$DRONE metrics $FLIGHT
$DRONE flag $FLIGHT --segment 1.0
$DRONE report $FLIGHT
$DRONE terrain $FLIGHT
$DRONE join
# Both halves on one page, pictures carried inside it. Open this one.
$DRONE page $FLIGHT

echo
echo "Done. Open this (FREE PUBLIC DATA, not ours), inside Dos_Ojos/:"
echo "  THE PAGE   $DEMO/dosojos_drone/out/$FLIGHT/page.html"
echo
echo "The pictures it is made of:"
echo "  satellite  $DEMO/dosojos_sat/out/PUBLIC-purdue-f54_NDVI.png"
echo "  water      $DEMO/dosojos_sat/out/PUBLIC-purdue-f54_water.png"
echo "  drone      $DEMO/dosojos_drone/out/$FLIGHT/flag_overlay.png"
echo "  ground     $DEMO/dosojos_drone/out/$FLIGHT/terrain.png"
