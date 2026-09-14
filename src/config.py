"""Where the SMS front door keeps its data, and how it reaches the phone network.

Farmers' data (phone numbers, field outlines, water records, photos) never lives
in a repository. It goes to ``Dos_Ojos/farm_data`` unless ``--data`` says
otherwise, laid out so the two halves can run on it unchanged::

    farm_data/
      sms/sms.sqlite      conversations, fields, events: the record of what was said
      sms/media/          photos sent by text (water tickets, problems)
      sms/sms.env         Twilio keys, written by you, never by this program
      dosojos_sat/        satellite workspace: fields.geojson, field_log.csv, cache/, out/
      dosojos_drone/      drone workspace: flights.json, data/raw/<flight>/ uploads

Nothing is sent to a phone until Twilio keys are present. Without them every
outgoing text is stored and shown in the simulator or the terminal instead.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

#: Relative to the project root: Dos_Ojos/farm_data, beside the two halves.
DEFAULT_DATA = Path("../farm_data")
TIMEZONE = "America/Chicago"
#: Texts nobody asked for go out between 8 am and 8 pm, farm time.
QUIET_START_HOUR, QUIET_END_HOUR = 8, 20
#: How long a map or upload link keeps working.
LINK_DAYS = 14
DEFAULT_PORT = 8080

ENV_FILE = "sms.env"


class ConfigError(RuntimeError):
    """Raised when the settings cannot work, with what to change."""


@dataclass(frozen=True)
class Settings:
    """Resolved paths and keys for one run."""

    data_dir: Path
    public_url: str
    team_contact: str | None = None
    twilio_sid: str | None = None
    twilio_token: str | None = None
    twilio_from: str | None = None
    twilio_service: str | None = None
    timezone: str = TIMEZONE
    #: A band on every page, e.g. to mark a demo's made-up farmers.
    banner: str | None = None

    @property
    def sms_dir(self) -> Path:
        return self.data_dir / "sms"

    @property
    def db_path(self) -> Path:
        return self.sms_dir / "sms.sqlite"

    @property
    def media_dir(self) -> Path:
        return self.sms_dir / "media"

    @property
    def sat_workspace(self) -> Path:
        return self.data_dir / "dosojos_sat"

    @property
    def drone_workspace(self) -> Path:
        return self.data_dir / "dosojos_drone"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def twilio_ready(self) -> bool:
        """True when texts can really be sent: an account, its token and a sender."""
        return bool(self.twilio_sid and self.twilio_token
                    and (self.twilio_from or self.twilio_service))

    def now(self) -> datetime:
        """The time on the farm."""
        return datetime.now(self.tz)

    def link(self, path: str) -> str:
        """A link a farmer can open, on the public address when one is set."""
        return self.public_url.rstrip("/") + "/" + path.lstrip("/")

    def ensure_dirs(self) -> None:
        for path in (self.sms_dir, self.media_dir, self.sat_workspace, self.drone_workspace):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, data_dir: Path | None = None, *, env: Mapping[str, str] | None = None,
             port: int = DEFAULT_PORT) -> "Settings":
        """Settings from ``data_dir`` plus keys in its ``sms/sms.env``, then the environment.

        Real environment variables win over the file, so a key can be tried out
        for one run without editing it.
        """
        root = (data_dir or (default_root() / DEFAULT_DATA)).resolve()
        values = {**read_env_file(root / "sms" / ENV_FILE),
                  **dict(os.environ if env is None else env)}

        def get(key: str) -> str | None:
            value = (values.get(key) or "").strip()
            return value or None

        sid, token = get("TWILIO_ACCOUNT_SID"), get("TWILIO_AUTH_TOKEN")
        sender, service = get("TWILIO_FROM"), get("TWILIO_MESSAGING_SERVICE_SID")
        partial = [k for k, v in (("TWILIO_ACCOUNT_SID", sid), ("TWILIO_AUTH_TOKEN", token))
                   if not v]
        if (sid or token or sender or service) and (partial or not (sender or service)):
            missing = partial + ([] if (sender or service) else
                                 ["TWILIO_FROM (or TWILIO_MESSAGING_SERVICE_SID)"])
            raise ConfigError(
                f"Twilio is half set up: {', '.join(missing)} missing. Put all of them in "
                f"{root / 'sms' / ENV_FILE}, or none to keep texts in the simulator."
            )
        return cls(
            data_dir=root,
            public_url=get("DOSOJOS_PUBLIC_URL") or f"http://localhost:{port}",
            team_contact=get("DOSOJOS_TEAM_CONTACT"),
            twilio_sid=sid, twilio_token=token, twilio_from=sender, twilio_service=service,
            timezone=get("DOSOJOS_TIMEZONE") or TIMEZONE, banner=get("DOSOJOS_BANNER"),
        )


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines; blank lines and ``#`` comments skipped, quotes trimmed."""
    path = Path(path)
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path} line {number}: expected KEY=VALUE, got {line!r}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def default_root() -> Path:
    """Project root, the directory holding ``src/``."""
    return Path(__file__).resolve().parent.parent


def setup_logging(verbosity: int = 0) -> None:
    """Configure stderr logging; ``verbosity`` >= 1 turns on DEBUG."""
    level = logging.DEBUG if verbosity else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("urllib3", "rasterio", "matplotlib", "fiona"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
