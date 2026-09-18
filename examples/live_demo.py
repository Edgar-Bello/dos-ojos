"""Dos Ojos on real phones: a live system, a tunnel to it, and a Twilio number pointed at it.

    live.cmd            start: tunnel, webhook, server, and the daily run at 6 am
    live.cmd update     the daily run by hand (satellite, weather, soil, alerts)

The live system is empty and runs on the real date, in Dos_Ojos\\live_data, apart
from farm_data and from every demo. Whoever texts the Twilio number signs up the
way anyone would; on a trial account, only phones verified in the Twilio console
get answers.

The tunnel is a Cloudflare quick tunnel: free and with no account, but its
address changes every start. So each start reads the new address, points the
Twilio number's incoming texts at it, and hands it to the server for the links
in its texts. A quick tunnel refuses any single upload over 100 MB, which lets
photos and thermal frames through but not a laser scan of a whole field.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent                                  # Dos_Ojos/
LIVE = ROOT / "live_data"
ENV = LIVE / "sms" / "sms.env"
SMS_PYTHON = ROOT / "dosojos_sat" / ".venv" / "Scripts" / "python.exe"
DRONE_PYTHON = ROOT / "dosojos_drone" / ".venv" / "Scripts" / "python.exe"
PORT = 8100
#: The daily run: satellite, weather, soil and the alerts that go out.
DAILY_AT = 6
TUNNEL_WAIT_S = 60
#: `dosojos-sms webhook` exits with this when the number must be pointed by hand.
NOT_OWNED_EXIT = 3
QUICK_TUNNEL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

TEMPLATE = """\
# Dos Ojos live system. Fill in the three Twilio lines yourself, from the Twilio
# console (Account Info on the front page; the number under Phone Numbers).
# This file stays on this computer and is never committed.
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
# The number people text, as +1 and ten digits, e.g. +18885550123:
TWILIO_FROM=

# Put at the end of the HELP text, e.g. your name and a number to call:
# DOSOJOS_TEAM_CONTACT=Edgar 956-555-0100

# Filled in by live.cmd on every start; leave these as they are.
DOSOJOS_READ_NOW=1
DOSOJOS_ON_UPLOAD={team_step}
"""


def say(message: str) -> None:
    print(message, flush=True)


def prepare() -> bool:
    """The live folder and its settings file; False while the Twilio keys are missing."""
    if not ENV.exists():
        ENV.parent.mkdir(parents=True, exist_ok=True)
        team_step = (f'"{DRONE_PYTHON.as_posix()}" "{(HERE / "process_flight.py").as_posix()}" '
                     f'--data "{LIVE.as_posix()}" --uploads-only')
        ENV.write_text(TEMPLATE.format(team_step=team_step), encoding="utf-8")
        say(f"Made the live system in {LIVE}")
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    missing = [k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM")
               if not values.get(k)]
    if missing:
        say(f"\nThe Twilio keys are not in yet: {', '.join(missing)}.\n"
            f"Open this file, fill them in, save it, and run live.cmd again:\n    {ENV}")
        return False
    return True


def find_cloudflared() -> Path | None:
    for candidate in (ROOT / "tools" / "cloudflared.exe", shutil.which("cloudflared")):
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def open_tunnel(cloudflared: Path) -> tuple[subprocess.Popen, str]:
    """Start a quick tunnel to the server's port and wait for its address."""
    process = subprocess.Popen(
        [str(cloudflared), "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace")
    found: list[str] = []

    def read() -> None:
        for line in process.stdout:
            if not found:
                match = QUICK_TUNNEL.search(line)
                if match:
                    found.append(match.group(0))
            # Keep draining, or cloudflared blocks once the pipe fills.

    threading.Thread(target=read, daemon=True).start()
    deadline = time.time() + TUNNEL_WAIT_S
    while not found and time.time() < deadline and process.poll() is None:
        time.sleep(0.5)
    if not found:
        process.terminate()
        raise SystemExit("The tunnel did not give an address. Check the internet connection "
                         "and run live.cmd again.")
    return process, found[0]


def sms(*arguments: str, env: dict | None = None) -> int:
    command = [str(SMS_PYTHON), "-m", "dosojos_sms", "--data", str(LIVE), *arguments]
    return subprocess.run(command, cwd=ROOT, env=env).returncode


def update(env: dict | None = None) -> int:
    """The daily run: every field's satellite, weather and soil, then the alerts."""
    code = sms("daily", "--send", "--years", "1", "--skip-baseline", env=env)
    subprocess.run([str(DRONE_PYTHON), str(HERE / "process_flight.py"), "--data", str(LIVE),
                    "--uploads-only"], cwd=ROOT, env=env)
    return code


def every_morning(env: dict) -> None:
    """Run the daily run at 6 am, every day, for as long as the server runs."""
    while True:
        now = datetime.now()
        next_run = now.replace(hour=DAILY_AT, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        time.sleep((next_run - now).total_seconds())
        say(f"\n--- the daily run, {datetime.now():%a %d %b %H:%M} ---")
        update(env)


def start() -> int:
    if not prepare():
        return 1
    cloudflared = find_cloudflared()
    if cloudflared is None:
        say(f"\ncloudflared is not on this computer. Put cloudflared.exe in\n    {ROOT / 'tools'}\n"
            "and run live.cmd again.")
        return 1
    say("Opening the tunnel...")
    tunnel, address = open_tunnel(cloudflared)
    env = {**os.environ, "DOSOJOS_PUBLIC_URL": address}
    try:
        say(f"  public address  {address}")
        pointed = sms("webhook", env=env)
        if pointed == NOT_OWNED_EXIT:
            say("\nThe address changes every time live.cmd starts, so paste the new one each time.")
        elif pointed != 0:
            say("The Twilio number could not be pointed at the tunnel; see the message above.")
            return 1
        threading.Thread(target=every_morning, args=(env,), daemon=True).start()
        say(f"\nText the Twilio number from a verified phone. The pretend phone still works "
            f"here: http://localhost:{PORT}/sim\nCtrl+C stops it all.\n")
        return sms("serve", "--port", str(PORT), "--sim", env=env)
    except KeyboardInterrupt:
        return 0
    finally:
        tunnel.terminate()


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(line_buffering=True)
    if argv and argv[0] == "update":
        return update() if prepare() else 1
    if argv:
        print(__doc__)
        return 2
    return start()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
