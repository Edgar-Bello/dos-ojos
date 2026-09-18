"""Try Dos Ojos as a farmer would the first time: an empty system and a pretend phone.

Three scenes, one per service, each pinned to the day its data was taken and each
on its own port, so all three can be open at once and none clashes with the
example demos on 8080-8082:

    satellite   port 8090   Tue 20 May 2025   any real Rio Grande Valley field
    drone       port 8091   Wed 11 Jul 2018   the day after Purdue flew its sorghum field 54
    thermal     port 8092   Sun 20 May 2018   the morning a thermal camera ran over TERRA-REF

Nobody is signed up in any of them. You text in, draw your own field, and send
your own flight; ``update`` then does what happens overnight in the real
product: the satellite, the weather and the soil for every field drawn so far,
and the drone half on every flight uploaded so far.

    try.cmd thermal            start it (and open the phone in the browser)
    try.cmd thermal update     the overnight step
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


def prepare(scene: str) -> Path:
    """An empty system for this scene, pinned to its day, if there is none yet."""
    data, settings = folder(scene), SCENES[scene]
    env = data / "sms" / "sms.env"
    if not env.exists():
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text(f"DOSOJOS_BANNER={settings['banner']}\n"
                       f"DOSOJOS_AS_OF={settings['as_of']}\n", encoding="utf-8")
        print(f"A new, empty {scene} system in {data}")
    return data


def start(scene: str) -> int:
    settings = SCENES[scene]
    prepare(scene)
    print(f"\nThe phone opens at http://localhost:{settings['port']}/sim  -  "
          "Ctrl+C here stops it.")
    print(f"Text 'hola' (or 'hello') to sign up. After you draw your field, run:\n"
          f"    {HERE / 'try.cmd'} {scene} update\n")
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
