"""Dos Ojos on real phones: a live system, a tunnel to it, and a Twilio number pointed at it.

    live.cmd            start: tunnel, webhook, server, and the daily run at 6 am
    live.cmd update     the daily run by hand (satellite, weather, soil, alerts)

The live system is empty and runs on the real date, in Dos_Ojos\\live_data, apart
from farm_data and from every demo. Whoever texts the Twilio number signs up the
way anyone would; on a trial account, only phones verified in the Twilio console
get answers.

Farmers can also message a Telegram bot (TELEGRAM_BOT_TOKEN, from @BotFather), a
stand-in while no number can text yet. With the bot set up, the Twilio step is
skipped.

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
#: A quick tunnel's address is several words joined by hyphens
#: (https://sie-reasons-prominent-valued.trycloudflare.com). cloudflared also names
#: its own API host, api.trycloudflare.com, while it starts: taking that one put
#: Cloudflare's "Method Not Allowed" page in farmers' map and upload links.
QUICK_TUNNEL = re.compile(
    r"https://(?!api\.)[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")

TELEGRAM = """\
# Or instead: a Telegram bot. In Telegram, message @BotFather, send /newbot, and
# paste the token it gives you here:
TELEGRAM_BOT_TOKEN=
"""

TEMPLATE = """\
# Dos Ojos live system. Fill in the three Twilio lines yourself, from the Twilio
# console (Account Info on the front page; the number under Phone Numbers).
# This file stays on this computer and is never committed.
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
# The number people text, as +1 and ten digits, e.g. +18885550123:
TWILIO_FROM=

{telegram}
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
        ENV.write_text(TEMPLATE.format(team_step=team_step, telegram=TELEGRAM), encoding="utf-8")
        say(f"Made the live system in {LIVE}")
    elif "TELEGRAM_BOT_TOKEN" not in ENV.read_text(encoding="utf-8"):
        with ENV.open("a", encoding="utf-8") as file:
            file.write("\n" + TELEGRAM)
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    missing = [k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM")
               if not values.get(k)]
    if missing and not values.get("TELEGRAM_BOT_TOKEN"):
        say(f"\nThe Twilio keys are not in yet: {', '.join(missing)}.\n"
            f"Open this file, fill them in (or the Telegram bot's token), save it, and run "
            f"live.cmd again:\n    {ENV}")
        return False
    return True


def telegram_only() -> bool:
    """True when the bot is set up: then the Twilio step is skipped."""
    for line in ENV.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "TELEGRAM_BOT_TOKEN" and value.strip():
            return True
    return False


def find_cloudflared() -> Path | None:
    for candidate in (ROOT / "tools" / "cloudflared.exe", shutil.which("cloudflared")):
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def tunnel_reaches_us(address: str, timeout: float = 20.0) -> bool:
    """True when that address answers as this server, not as something of Cloudflare's.

    A tunnel takes a moment to come up, so this keeps asking until it answers or
    the time runs out.
    """
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{address}/sim", timeout=10) as answer:
                if "DosOjos" in (answer.headers.get("Server") or ""):
                    return True
        except urllib.error.HTTPError as exc:      # our own 404 is still our server
            if "DosOjos" in (exc.headers.get("Server") or ""):
                return True
        except OSError:
            pass
        time.sleep(2)
    return False


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


def check_tunnel(address: str) -> None:
    """Say plainly whether farmers' links will work, before any go out."""
    if tunnel_reaches_us(address):
        say(f"  links checked   {address}/f/... opens this server")
        return
    say(f"\nWARNING: {address} did not answer as this server. Map and upload links sent to "
        f"farmers may not open. Stop live.cmd (Ctrl+C) and start it again to get a new "
        f"address.")


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
        pointed = 0 if telegram_only() else sms("webhook", env=env)
        if pointed == NOT_OWNED_EXIT:
            env["DOSOJOS_REPLY_BY_API"] = "1"      # that number drops answers sent back in the webhook
            say("\nThe address changes every time live.cmd starts, so paste the new one each time.")
        elif pointed != 0:
            say("The Twilio number could not be pointed at the tunnel; see the message above.")
            return 1
        threading.Thread(target=every_morning, args=(env,), daemon=True).start()
        # Once the server is up, make sure the address in farmers' links really opens it.
        threading.Thread(target=check_tunnel, args=(address,), daemon=True).start()
        say(f"\nMessage the Telegram bot, or text the Twilio number from a verified phone. The pretend phone still works "
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
