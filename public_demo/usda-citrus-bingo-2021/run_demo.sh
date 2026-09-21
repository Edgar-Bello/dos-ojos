#!/usr/bin/env bash
# Re-run the USDA citrus public-data demo. FREE PUBLIC DATA, NOT OURS: a Florida research
# grove. Runs from any folder, in Git Bash (from PowerShell: run_demo.cmd). The download is
# not repeated; see SOURCE.md. OpenDroneMap needs Docker Desktop running.
set -euo pipefail
if [[ -n "${WSL_DISTRO_NAME:-}" ]]; then
    echo "This is WSL's bash (what 'bash' starts in PowerShell); it cannot run the Windows tools." >&2
    echo "From PowerShell run run_demo.cmd instead, or run this script in a Git Bash window." >&2
    exit 1
fi
cd "$(dirname "${BASH_SOURCE[0]}")/../.."   # every path below is relative to Dos_Ojos/
export PYTHONIOENCODING=utf-8

DEMO=public_demo/usda-citrus-bingo-2021
# Through Python: Windows Smart App Control blocks the unsigned dosojos-*.exe launchers.
PYTHON="dosojos_drone/.venv/Scripts/python.exe"
DRONE="$PYTHON -m dosojos_drone --workspace $DEMO/dosojos_drone"
FLIGHT=PUBLIC-usda-bingo-20210512
RAW=$DEMO/dosojos_drone/data/raw/$FLIGHT

# The 46 photos, as a second name for the same bytes (hard links: no extra space).
mkdir -p "$RAW"
for photo in "$DEMO"/download/DJI_*.JPG; do
    [[ -e "$RAW/$(basename "$photo")" ]] || ln "$photo" "$RAW/"
done

$DRONE register $FLIGHT --field PUBLIC-usda-bingo --date 2021-05-12 \
    --crop "citrus (Bingo mandarin, planted 2018)" \
    --source "USDA-ARS Fort Pierce FL, Ag Data Commons doi:10.15482/USDA.ADC/26946823, public domain" \
    --notes "Rootstock trial, 206 trees in 4 rows; per-tree height, width and health measured April 2021. Not a Dos Ojos flight." \
    --force
$DRONE survey $FLIGHT

if ! $DRONE doctor --images 46; then
    echo >&2
    echo "Docker is not ready, so OpenDroneMap cannot run yet; stopping after the survey." >&2
    echo "See SOURCE.md, 'Where it stands', for the fix; then run this again." >&2
    exit 1
fi
# High quality, not the default medium. On this grove it halves the height error
# (0.74 m short down to 0.39 m) and finds 17 more trees, for ten more minutes on
# 46 photos. Fine leaves against sky need the denser cloud; row crops do not.
$DRONE odm $FLIGHT --pc-quality high --feature-quality high
$DRONE chm $FLIGHT
# One crown per tree, not row segments: every step after detect needs telling too,
# because they all default to --method rows and would look for a file that is not there.
$DRONE detect $FLIGHT --method watershed
$DRONE metrics $FLIGHT --method watershed
$DRONE flag $FLIGHT --method watershed
$DRONE report $FLIGHT --method watershed

# This field has no satellite record, so the page says so and stands on the drone
# half alone. That is the case it was built for.
$DRONE page $FLIGHT

echo
echo "Done. Open this (FREE PUBLIC DATA, not ours), inside Dos_Ojos/:"
echo "  THE PAGE  $DEMO/dosojos_drone/out/$FLIGHT/page.html"
echo
echo "The pictures it is made of:"
echo "  survey  $DEMO/dosojos_drone/out/$FLIGHT/quicklook.png"
echo "  trees   $DEMO/dosojos_drone/out/$FLIGHT/flag_overlay.png"

# The whole reason this dataset is here: the team measured every tree by hand, so
# the flight can be scored instead of admired.
$PYTHON "$DEMO/check_answer_key.py"
