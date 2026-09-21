"""The team step of the demo: take what a farmer uploaded and run the drone half on it.

In the real product this stays a person's job, and deliberately so. An upload
arrives as whatever the farmer's camera or service wrote - raw photos, a
finished orthophoto, a point cloud, thousands of thermal frames - and someone
has to look at it and decide what it is before any of it is worth running.
``dosojos-sms todo`` lists uploads waiting for exactly that.

This script makes that decision for the demos, and only for them: it recognises
the two public sets they use, says out loud what it decided and why, and runs
the same commands a person would type. Anything it does not recognise it
describes and leaves alone.

    python process_flight.py --data examples\\demo_data_thermal

With nothing uploaded yet it falls back to the public files the demo is built
on, so the drone half can be seen working without waiting on a 3 GB upload.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent                                   # Dos_Ojos/
DRONE_PYTHON = ROOT / "dosojos_drone" / ".venv" / "Scripts" / "python.exe"
SMS_PYTHON = ROOT / "dosojos_sat" / ".venv" / "Scripts" / "python.exe"

#: The public sets the demos stand on, used when nothing has been uploaded yet.
PUBLIC_THERMAL = ROOT / "public_demo" / "terraref-sorghum-2018" / "download" / "ir_geotiff"
PUBLIC_DRONE = ROOT / "public_demo" / "purdue-sorghum-2018" / "download" / "2018" / "uav"

#: Rows 0.76 m apart, so 1.5 m joins neighbouring rows into one patch without
#: swallowing the field the way the orchard default (6 m) would; and these are
#: research plots, not a 40-acre field, so the smallest patch worth naming is
#: smaller too.
THERMAL_ARGS = ["--group-gap", "1.5", "--min-patch", "8"]
#: Fewer photos than this seldom overlap enough to build anything in 3D.
MIN_3D_PHOTOS = 12
#: Quicker than the drone half's own defaults: a farmer is waiting on a message,
#: and neither the 3D view nor the flags need ODM's finest detail or its
#: textured mesh. On 46 photos this takes minutes rather than half an hour.
ODM_ARGS = ["--feature-quality", "medium", "--pc-quality", "medium", "--skip-3dmodel"]
#: Crops measured tree by tree (one crown each) rather than as stretches of row.
TREE_CROPS = {"citrus", "orchard", "pecan", "avocado", "mango"}


def say(message: str) -> None:
    print(message, flush=True)


def run(workspace: Path, *arguments: str, optional: bool = False) -> bool:
    """One drone command, printed before it runs so nothing here is a black box."""
    command = [str(DRONE_PYTHON), "-m", "dosojos_drone", "--workspace", str(workspace),
               *[str(a) for a in arguments]]
    say("\n$ dosojos-drone " + " ".join(str(a) for a in arguments))
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        if optional:
            say("  (that step is a nice-to-have here; the farmer's page does not need it)")
            return False
        raise SystemExit(f"that step failed ({result.returncode}); nothing after it was run")
    return True


def uploads(data: Path) -> list[dict]:
    """Finished uploads in this demo, newest first, with their field and folder."""
    database = data / "sms" / "sms.sqlite"
    if not database.exists():
        raise SystemExit(f"no demo at {data}: run the set-up script for it first")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        # An upload knows its field through the link the farmer opened, which is
        # also what keeps a stale link from writing into someone else's field.
        rows = connection.execute(
            "SELECT u.flight_id, u.folder, u.files, u.done_at, l.field_id, f.crop "
            "FROM uploads u JOIN links l ON l.token = u.token "
            "LEFT JOIN fields f ON f.id = l.field_id "
            "WHERE u.done_at IS NOT NULL ORDER BY u.done_at DESC").fetchall()
    return [dict(row) for row in rows]


def a_field(data: Path) -> tuple[str, str]:
    """The demo's field and the crop on it, for a run with nothing uploaded."""
    with sqlite3.connect(data / "sms" / "sms.sqlite") as connection:
        row = connection.execute(
            "SELECT id, crop FROM fields ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise SystemExit(f"no field in {data}: run the set-up script for it first")
    return row[0], row[1] or "sorghum"


def _placed(path: Path) -> tuple[bool, int]:
    """Whether a TIFF already knows where it lies on the ground, and its band count."""
    import rasterio

    with rasterio.open(path) as dataset:
        return dataset.crs is not None and not dataset.transform.is_identity, dataset.count


def what_is_it(folder: Path) -> tuple[str, dict]:
    """Recognise an upload by what is in it, the way a person would."""
    files = [p for p in folder.rglob("*") if p.is_file()]
    kinds: dict[str, list[Path]] = {}
    for path in files:
        kinds.setdefault(path.suffix.lower(), []).append(path)
    tiffs = kinds.get(".tif", []) + kinds.get(".tiff", [])
    clouds = kinds.get(".las", []) + kinds.get(".laz", [])
    photos = kinds.get(".jpg", []) + kinds.get(".jpeg", [])
    if clouds:
        return "products", {"ortho": max(tiffs, key=lambda p: p.stat().st_size) if tiffs else None,
                            "clouds": sorted(clouds, key=lambda p: p.name)}
    if len(tiffs) > 1:
        placed, bands = _placed(tiffs[0])
        if bands == 1:
            # Many one-band pictures are a thermal camera's. Frames that already
            # know where they lie go straight to the mosaic; a drone's own
            # thermal photos, with only a GPS tag each, are placed first.
            return ("thermal" if placed else "thermal_photos"), {"n": len(tiffs)}
        if not placed:
            photos += tiffs
    if len(photos) > 1:
        return "photos", {"n": len(photos)}
    if len(tiffs) == 1:
        placed, bands = _placed(tiffs[0])
        if placed and bands >= 3:
            return "ortho", {"ortho": tiffs[0]}
    return "unknown", {"n": len(files)}


def thermal(workspace: Path, flight: str, field: str, frames: Path, *, source: str | None,
            date: str, crop: str) -> None:
    """Stitch a thermal scan into one map and score its warm patches."""
    if source:
        run(workspace, "register", flight, "--field", field, "--date", date, "--crop", crop,
            "--force", "--source", source)
    run(workspace, "thermal", flight, "--frames", frames, *THERMAL_ARGS)


def stitched(workspace: Path, flight: str, folder: Path) -> None:
    """Loose colour photos: join them into one map and judge it by colour.

    No Docker and no height model: the quick way, done in minutes. With Docker
    running, 'dosojos-drone odm' makes the full height model instead, in hours.
    """
    run(workspace, "stitch", flight, "--photos", folder)
    run(workspace, "colour", flight)


def docker_ready() -> tuple[bool, str]:
    """Whether OpenDroneMap can run here, and if not, why not."""
    from dosojos_drone import odm_runner

    status = odm_runner.check_docker()
    if not status.available:
        return False, (status.problems or ["Docker is not running"])[0]
    if not status.has_image:
        return False, f"the {odm_runner.ODM_IMAGE} image is not pulled"
    return True, ""


def three_d(workspace: Path, flight: str, crop: str | None) -> bool:
    """Photos into a 3D model with OpenDroneMap, then heights, plants and flags from it.

    False when ODM itself fails, so the flat map can be made instead.
    """
    if not run(workspace, "odm", flight, *ODM_ARGS, optional=True):
        return False
    method = ["--method", "watershed"] if (crop or "") in TREE_CROPS else []
    segment = [] if method else ["--segment", "1.0"]
    run(workspace, "chm", flight)
    run(workspace, "detect", flight, *method, *segment)
    run(workspace, "metrics", flight, *method)
    run(workspace, "flag", flight, *method, *segment)
    run(workspace, "report", flight, *method)
    if method:
        # Orchards: the trained model finds the trees the watershed merges, reads
        # their real height and makes a short list of poor ones.
        run(workspace, "trees-ai", flight, optional=True)
    run(workspace, "model3d", flight, optional=True)
    run(workspace, "terrain", flight, optional=True)
    return True


def products(workspace: Path, flight: str, ortho: Path, clouds: list[Path]) -> None:
    """Finished maps from a drone service: import them where ODM would have written."""
    import rasterio

    with rasterio.open(ortho) as dataset:
        crs = dataset.crs
    if crs is None:
        raise SystemExit(f"{ortho.name} carries no CRS, so the point clouds cannot be placed")
    # The later scan is the crop, the earlier one is the ground under it: that is
    # how a grower flies a field twice, and how Purdue's set is arranged.
    dsm, dtm = clouds[-1], clouds[0]
    say(f"  the orthophoto is in {crs.to_string()}, so the point clouds are read as that too")
    say(f"  {dsm.name} is the crop surface, {dtm.name} the bare ground under it")
    run(workspace, "import", flight, "--force", "--crs", crs.to_string(),
        "--ortho", ortho, "--dsm", dsm, "--dtm", dtm)
    run(workspace, "chm", flight)
    run(workspace, "detect", flight, "--segment", "1.0")
    run(workspace, "metrics", flight)
    run(workspace, "flag", flight, "--segment", "1.0")
    run(workspace, "report", flight)
    run(workspace, "terrain", flight)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, required=True,
                        help="the demo folder, e.g. examples\\demo_data_thermal")
    parser.add_argument("--flight", default=None, help="one flight, rather than every "
                                                       "upload waiting")
    parser.add_argument("--uploads-only", action="store_true",
                        help="only what farmers uploaded: never fall back on the public files")
    arguments = parser.parse_args()
    data = arguments.data.resolve()
    workspace = data / "dosojos_drone"

    waiting = [u for u in uploads(data)
               if arguments.flight in (None, u["flight_id"])
               and not (workspace / "out" / u["flight_id"] / "thermal.json").exists()
               and not (workspace / "out" / u["flight_id"] / "chm.tif").exists()
               and not (workspace / "out" / u["flight_id"] / "flags_colour.geojson").exists()]
    if not waiting and arguments.uploads_only:
        say("No finished upload is waiting. When the farmer texts DRONE, sends the files "
            "and presses I'm done, run this again.")
        return
    if not waiting:
        field, crop = a_field(data)
        if PUBLIC_THERMAL.is_dir() and (data / "sms" / "sms.env").read_text(
                encoding="utf-8").find("2018-05-20") >= 0:
            say("Nothing uploaded yet, so this runs on the public frames the demo is built "
                f"on:\n  {PUBLIC_THERMAL}")
            thermal(workspace, f"{field}-20180520", field, PUBLIC_THERMAL, date="2018-05-20",
                    crop=crop, source="public: TERRA-REF field scanner, Maricopa AZ "
                                      "(doi:10.5061/dryad.4b8gtht99, public domain)")
        elif PUBLIC_DRONE.is_dir():
            ortho = next(PUBLIC_DRONE.glob("rgb/*/*.tif"), None)
            clouds = sorted(PUBLIC_DRONE.glob("lidar/*/*.las"))
            if ortho is None or len(clouds) < 2:
                raise SystemExit(f"the public drone set is not in {PUBLIC_DRONE}")
            say("Nothing uploaded yet, so this runs on the public flight the demo is built "
                f"on:\n  {ortho.parent}")
            run(workspace, "register", f"{field}-20180710", "--field", field,
                "--date", "2018-07-10", "--crop", crop, "--force", "--row-spacing", "0.762",
                "--source", "public: Purdue University, PURR doi:10.4231/MY7W-FH43, CC0")
            products(workspace, f"{field}-20180710", ortho, clouds)
        else:
            raise SystemExit("nothing uploaded, and no public set to fall back on")
    for upload in waiting:
        folder = Path(upload["folder"])
        kind, detail = what_is_it(folder)
        say(f"\n{upload['flight_id']}: {upload['files']} files in {folder}")
        if kind == "thermal":
            say(f"  {detail['n']} single-band GeoTIFFs and no point cloud: a thermal scan.")
            thermal(workspace, upload["flight_id"], upload["field_id"], folder, source=None,
                    date="", crop=upload["crop"] or "sorghum")
        elif kind == "products":
            say("  a point cloud and an orthophoto: finished maps from a drone service.")
            products(workspace, upload["flight_id"], detail["ortho"], detail["clouds"])
        elif kind == "ortho":
            say("  one colour map that already knows where it lies, and no point cloud: a "
                "finished orthophoto from a drone service. Judged by colour; no heights.")
            target = workspace / "data" / "odm" / upload["flight_id"] / "odm_orthophoto"
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(detail["ortho"], target / "odm_orthophoto.tif")
            run(workspace, "colour", upload["flight_id"])
        elif kind == "thermal_photos":
            say(f"  {detail['n']} one-band pictures with no place on the map: a drone's "
                "thermal photos. Each is placed from its GPS and its neighbours first.")
            run(workspace, "thermal", upload["flight_id"], "--photos", folder, *THERMAL_ARGS)
        elif kind == "photos":
            ready, why_not = docker_ready()
            if detail["n"] >= MIN_3D_PHOTOS and ready:
                say(f"  {detail['n']} colour photos: built into a 3D model with OpenDroneMap, "
                    "then each plant's height and colour judged from it.")
                if three_d(workspace, upload["flight_id"], upload["crop"]):
                    continue
                say("  OpenDroneMap could not build it, so the photos are joined flat instead.")
            elif detail["n"] >= MIN_3D_PHOTOS:
                say(f"  no 3D model: {why_not}.")
            say(f"  {detail['n']} colour photos: joined into one map from their GPS and "
                "where they overlap, then judged square by square by how green they are.")
            stitched(workspace, upload["flight_id"], folder)
        else:
            say(f"  {detail['n']} files this script does not recognise; left alone.")
            continue

    # Joining the drone to the satellite's scores needs those scores; a live system
    # that has never run `dosojos-sat score` has none, and the farmer's page does not
    # need them, so skip it quietly rather than print an error that is not one.
    if (data / "dosojos_sat" / "out" / "flags.json").exists():
        run(workspace, "join", optional=True)
    say("\nDone. The farmer can now text PORQUE (or WHY) and get the page with it on.")


if __name__ == "__main__":
    main()
