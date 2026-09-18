"""Try Dos Ojos as a farmer would the first time: an empty system and a pretend phone.

Three scenes, one per service, each pinned to the day its data was taken and each
on its own port, so all three can be open at once and none clashes with the
example demos on 8080-8082:

    satellite   port 8090   Tue 20 May 2025   any real Rio Grande Valley field
    drone       port 8091   Wed 11 Jul 2018   the day after Purdue flew its sorghum field 54
    thermal     port 8092   Sun 20 May 2018   the morning a thermal camera ran over TERRA-REF

Nobody is signed up in any of them. You text in, draw your own field, and send
your own flight. Nothing waits for the night: the moment a field has its map,
crop and planting date, the server reads it from the satellite (only the
pictures since planting, so it takes a minute or two) and texts the answer; the
moment an upload is finished, it runs the drone half on it and texts what it
found, with the picture. Watch the window the system runs in to see it work.

    try.cmd thermal            start it (and open the phone in the browser)
    try.cmd thermal update     the whole overnight run by hand, if you want it
    try.cmd thermal reset      start that one over; the old one goes to old_demos
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent                                  # Dos_Ojos/
SMS_PYTHON = ROOT / "dosojos_sat" / ".venv" / "Scripts" / "python.exe"
DRONE_PYTHON = ROOT / "dosojos_drone" / ".venv" / "Scripts" / "python.exe"

SCENES = {
    "satellite": {
        "port": 8090, "as_of": "2025-05-20",
        "banner": "TRY IT YOURSELF: a pretend phone, on real Rio Grande Valley fields. The "
                  "satellite, weather and soil are real public data, as of Tue 20 May 2025.",
    },
    "drone": {
        "port": 8091, "as_of": "2018-07-11",
        "banner": "TRY IT YOURSELF: a pretend phone, as of Wed 11 July 2018, the day after "
                  "Purdue University flew its sorghum field 54 in Indiana (public data, CC0). "
                  "Not the Rio Grande Valley.",
    },
    "thermal": {
        "port": 8092, "as_of": "2018-05-20",
        "banner": "TRY IT YOURSELF: a pretend phone, as of Sun 20 May 2018, the morning a "
                  "thermal camera ran over the TERRA-REF sorghum at Maricopa, Arizona (public "
                  "domain). Not the Rio Grande Valley.",
    },
}


def folder(scene: str) -> Path:
    return HERE / f"try_{scene}"


def sms(scene: str, *arguments: str) -> int:
    """One dosojos-sms command on this scene's folder."""
    command = [str(SMS_PYTHON), "-m", "dosojos_sms", "--data", str(folder(scene)), *arguments]
    return subprocess.run(command, cwd=ROOT).returncode


def settings_for(scene: str) -> dict[str, str]:
    """What a try-it system's sms.env says: its day, its banner, and no waiting."""
    data = folder(scene)
    team_step = (f'"{DRONE_PYTHON.as_posix()}" "{(HERE / "process_flight.py").as_posix()}" '
                 f'--data "{data.as_posix()}" --uploads-only')
    return {"DOSOJOS_BANNER": SCENES[scene]["banner"],
            "DOSOJOS_AS_OF": SCENES[scene]["as_of"],
            "DOSOJOS_READ_NOW": "1",
            "DOSOJOS_ON_UPLOAD": team_step}


def prepare(scene: str) -> Path:
    """An empty system for this scene, pinned to its day, if there is none yet.

    One made before reading right away existed gets the settings it lacks, and
    keeps everything already in it.
    """
    data = folder(scene)
    env = data / "sms" / "sms.env"
    fresh = not env.exists()
    env.parent.mkdir(parents=True, exist_ok=True)
    text = "" if fresh else env.read_text(encoding="utf-8")
    have = {line.split("=", 1)[0].strip() for line in text.splitlines() if "=" in line}
    missing = {k: v for k, v in settings_for(scene).items() if k not in have}
    if missing:
        if text and not text.endswith("\n"):
            text += "\n"
        env.write_text(text + "".join(f"{k}={v}\n" for k, v in missing.items()),
                       encoding="utf-8")
    if fresh:
        print(f"A new, empty {scene} system in {data}")
    return data


def start(scene: str) -> int:
    settings = SCENES[scene]
    prepare(scene)
    print(f"\nThe phone opens at http://localhost:{settings['port']}/sim  -  "
          "Ctrl+C here stops it.")
    print("Text 'hola' (or 'hello') to sign up. Once the field has its map, its crop and "
          "its planting date, this window shows the satellite being read, and the phone "
          "gets the answer a minute or two later.\n")
    return sms(scene, "serve", "--sim", "--open", "--port", str(settings["port"]))


def update(scene: str) -> int:
    data = folder(scene)
    if not (data / "sms" / "sms.sqlite").exists():
        print(f"Nothing to update yet: start it with  try.cmd {scene}  and sign up first.")
        return 1
    print("1. The satellite, the weather and the soil for every field drawn so far, and the "
          "texts that would go out overnight.\n")
    code = sms(scene, "daily", "--send", "--years", "1", "--skip-baseline")
    if code != 0:
        return code
    print("\n2. The drone half, on every flight uploaded and finished so far.\n")
    return subprocess.run([str(DRONE_PYTHON), str(HERE / "process_flight.py"),
                           "--data", str(data), "--uploads-only"], cwd=ROOT).returncode


def reset(scene: str) -> int:
    data = folder(scene)
    if not data.exists():
        print(f"There is no {scene} system to start over.")
        return 0
    moved = ROOT / "old_demos" / f"try-{scene}-{datetime.now():%Y%m%d-%H%M%S}"
    moved.parent.mkdir(parents=True, exist_ok=True)
    try:
        data.rename(moved)
    except OSError as exc:
        print(f"Could not move it ({exc}). Is its simulator still running? Stop it with Ctrl+C "
              "in its window and try again.")
        return 1
    print(f"Started over. The one you had is in {moved}")
    return 0


def main(argv: list[str]) -> int:
    # Line by line, so these notes land between the steps they belong to rather
    # than after everything the commands underneath printed.
    sys.stdout.reconfigure(line_buffering=True)
    if not argv or argv[0] not in SCENES or (len(argv) > 1 and argv[1] not in
                                            ("start", "update", "reset")):
        print(__doc__)
        return 2
    scene, verb = argv[0], (argv[1] if len(argv) > 1 else "start")
    return {"start": start, "update": update, "reset": reset}[verb](scene)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
