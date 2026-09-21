#!/usr/bin/env bash
# Re-run the TERRA-REF 2018 public-data thermal demo end to end. FREE PUBLIC DATA, NOT OURS.
# Runs from any folder, in Git Bash (from PowerShell: run_demo.cmd). The 3.3 GB of thermal
# frames are not downloaded again; see SOURCE.md for what they are and where they came from.
set -euo pipefail
if [[ -n "${WSL_DISTRO_NAME:-}" ]]; then
    echo "This is WSL's bash (what 'bash' starts in PowerShell); it cannot run the Windows tools." >&2
    echo "From PowerShell run run_demo.cmd instead, or run this script in a Git Bash window." >&2
    exit 1
fi
cd "$(dirname "${BASH_SOURCE[0]}")/../.."   # every path below is relative to Dos_Ojos/

DEMO=public_demo/terraref-sorghum-2018
# Through Python: Windows Smart App Control blocks the unsigned dosojos-*.exe launchers.
SAT="dosojos_sat/.venv/Scripts/python.exe -m dosojos_sat --workspace $DEMO/dosojos_sat"
DRONE="dosojos_drone/.venv/Scripts/python.exe -m dosojos_drone --workspace $DEMO/dosojos_drone"
FLIGHT=PUBLIC-terraref-20180520
FIELD=PUBLIC-terraref-mac
SOURCE="public: TERRA-REF field scanner, Maricopa AZ (doi:10.5061/dryad.4b8gtht99, public domain)"
BANNER="FREE PUBLIC DATA, NOT OUR FIELD  -  TERRA-REF field scanner, Maricopa AZ (doi:10.5061/dryad.4b8gtht99, public domain)"

# Satellite: the same strip from space, 2016-2019, judged on the day of the scan.
$SAT init-fields $DEMO/dosojos_sat/fields.geojson
$SAT fetch --start 2016-01-01 --end 2019-12-31
# The field's own normal. Two years only, and a research field grows something different
# in each of them, so the band is an illustration of the mechanism and no more.
$SAT baseline --season 2018 --history 2016-2017
$SAT score --season 2018 --as-of 2018-05-20
$SAT chart --season 2018 --as-of 2018-05-20 --index all --banner "$BANNER"

# Water checkbook: gridMET weather, USDA soil survey, sowing date from TERRA-REF's own docs.
$SAT weather --season 2018
$SAT soil
$SAT water --as-of 2018-05-20 --banner "$BANNER"

# The thermal scan: 2,719 frames stitched into one map of canopy temperature, then scored.
#   --group-gap 1.5  rows are 0.76 m apart, so 1.5 m joins neighbouring rows into one patch
#                    without swallowing the whole field the way the orchard default would
#   --min-patch 8    this is half an acre of research plots, not a 40-acre field
$DRONE register $FLIGHT --field $FIELD --date 2018-05-20 --crop "sorghum, variety trial" \
    --row-spacing 0.762 --force --source "$SOURCE" \
    --notes "Season 6 sorghum, planted 2018-04-20. FLIR SC615 on the gantry, 2,719 frames over 84 minutes. Not a Dos Ojos flight, and not a drone."
$DRONE thermal $FLIGHT --frames $DEMO/download/ir_geotiff --group-gap 1.5 --min-patch 8
$DRONE join
# Both halves on one page, pictures carried inside it. Open this one.
$DRONE page $FLIGHT

echo
echo "Done. Open this (FREE PUBLIC DATA, not ours), inside Dos_Ojos/:"
echo "  THE PAGE   $DEMO/dosojos_drone/out/$FLIGHT/page.html"
echo
echo "The pictures it is made of:"
echo "  thermal    $DEMO/dosojos_drone/out/$FLIGHT/thermal.png"
echo "  satellite  $DEMO/dosojos_sat/out/${FIELD}_NDVI.png"
echo "  water      $DEMO/dosojos_sat/out/${FIELD}_water.png"
