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


def say(message: str) -> None:
    print(message, flush=True)


def run(workspace: Path, *arguments: str, optional: bool = False) -> None:
    """One drone command, printed before it runs so nothing here is a black box."""
    command = [str(DRONE_PYTHON), "-m", "dosojos_drone", "--workspace", str(workspace),
               *[str(a) for a in arguments]]
    say("\n$ dosojos-drone " + " ".join(str(a) for a in arguments))
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        if optional:
            say("  (that step is a nice-to-have here; the farmer's page does not need it)")
            return
        raise SystemExit(f"that step failed ({result.returncode}); nothing after it was run")


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


def what_is_it(folder: Path) -> tuple[str, dict]:
    """Recognise an upload by what is in it, the way a person would."""
    files = [p for p in folder.rglob("*") if p.is_file()]
    kinds: dict[str, list[Path]] = {}
    for path in files:
        kinds.setdefault(path.suffix.lower(), []).append(path)
    tiffs = kinds.get(".tif", []) + kinds.get(".tiff", [])
    clouds = kinds.get(".las", []) + kinds.get(".laz", [])
    photos = kinds.get(".jpg", []) + kinds.get(".jpeg", []) + kinds.get(".dng", [])
    if len(tiffs) > 50 and not clouds:
        return "thermal", {"frames": folder, "n": len(tiffs)}
    if clouds:
        return "products", {"ortho": max(tiffs, key=lambda p: p.stat().st_size) if tiffs else None,
                            "clouds": sorted(clouds, key=lambda p: p.name)}
    if photos:
        return "photos", {"n": len(photos)}
    return "unknown", {"n": len(files)}


def thermal(workspace: Path, flight: str, field: str, frames: Path, *, source: str | None,
            date: str, crop: str) -> None:
    """Stitch a thermal scan into one map and score its warm patches."""
    if source:
        run(workspace, "register", flight, "--field", field, "--date", date, "--crop", crop,
            "--force", "--source", source)
    run(workspace, "thermal", flight, "--frames", frames, *THERMAL_ARGS)


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
    arguments = parser.parse_args()
    data = arguments.data.resolve()
    workspace = data / "dosojos_drone"

    waiting = [u for u in uploads(data)
               if arguments.flight in (None, u["flight_id"])
               and not (workspace / "out" / u["flight_id"] / "thermal.json").exists()
               and not (workspace / "out" / u["flight_id"] / "chm.tif").exists()]
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
        elif kind == "photos":
            say(f"  {detail['n']} photos: this one needs photogrammetry first, which is a "
                f"team job with Docker running:\n"
                f"    dosojos-drone --workspace {workspace} odm {upload['flight_id']}\n"
                "  then chm, detect, metrics, flag, report, terrain.")
            continue
        else:
            say(f"  {detail['n']} files this script does not recognise; left alone.")
            continue

    run(workspace, "join")
    say("\nDone. The farmer can now text PORQUE (or WHY) and get the page with it on.")


if __name__ == "__main__":
    main()
