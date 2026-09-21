#!/usr/bin/env bash
# Re-run the Valley four-crop public-data demo end to end. FREE PUBLIC DATA, NOT OUR FIELDS.
# Runs from any folder, in Git Bash (from PowerShell: run_demo.cmd). Needs the internet;
# what is already cached is not downloaded again. See SOURCE.md.
set -euo pipefail
if [[ -n "${WSL_DISTRO_NAME:-}" ]]; then
    echo "This is WSL's bash (what 'bash' starts in PowerShell); it cannot run the Windows tools." >&2
    echo "From PowerShell run run_demo.cmd instead, or run this script in a Git Bash window." >&2
    exit 1
fi
cd "$(dirname "${BASH_SOURCE[0]}")/../.."   # every path below is relative to Dos_Ojos/
export PYTHONIOENCODING=utf-8

DEMO=public_demo/rgv-crops-2025
# Through Python: Windows Smart App Control blocks the unsigned dosojos-*.exe launchers.
SAT="dosojos_sat/.venv/Scripts/python.exe -m dosojos_sat --workspace $DEMO/dosojos_sat"
DRONE="dosojos_drone/.venv/Scripts/python.exe -m dosojos_drone --workspace $DEMO/dosojos_drone"

# The fields: one real field per crop, picked from USDA's 2025 crop map.
$SAT cropmap --year 2025 --crops sorghum,cotton,corn,citrus --out $DEMO/dosojos_sat/fields.geojson \
    --banner "FREE PUBLIC DATA, NOT OUR FIELDS  -  USDA NASS Cropland Data Layer 2025"
$SAT init-fields $DEMO/dosojos_sat/fields.geojson

# Satellite: Sentinel-2 2021-2025, this season judged against 2021-2024, as of 20 May 2025.
$SAT fetch --start 2021-01-01 --end 2025-12-31 --workers 8
$SAT weather --season 2025
$SAT soil
$SAT baseline --season 2025 --history 2021-2024
$SAT score --season 2025 --as-of 2025-05-20
$SAT chart --season 2025 --as-of 2025-05-20 --index all \
    --banner "FREE PUBLIC DATA, NOT OUR FIELDS  -  real Valley fields picked from the USDA 2025 crop map; Sentinel-2 imagery"

# Ground: USGS 3DEP airborne lidar (flown 2019) instead of a drone, for the furrow crops.
# Not the citrus: under the trees the laser mostly hit canopy. Not the sorghum: when the
# survey flew, a crop over 1 m tall stood on 85% of that field, and 'lidar' refuses it.
for crop in cotton corn; do
    $DRONE lidar PUBLIC-rgv-$crop-1-lidar --field PUBLIC-rgv-$crop-1 --force
    $DRONE terrain PUBLIC-rgv-$crop-1-lidar
done

echo
echo "Done. The pictures (FREE PUBLIC DATA, not ours), inside Dos_Ojos/:"
echo "  crop map   $DEMO/dosojos_sat/out/cropmap_2025.png"
echo "  satellite  $DEMO/dosojos_sat/out/PUBLIC-rgv-<crop>-1_NDVI.png  (sorghum, cotton, corn, citrus)"
echo "  ground     $DEMO/dosojos_drone/out/PUBLIC-rgv-<crop>-1-lidar/terrain.png  (cotton, corn)"
echo "The same four fields, with made-up farmers texting about them: dosojos_sms/examples/setup_sms_demo.cmd"
