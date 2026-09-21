"""Shared configuration, constants and logging setup for the Dos Ojos satellite pipeline."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

# --------------------------------------------------------------------------- #
# STAC / imagery constants
# --------------------------------------------------------------------------- #

STAC_URL: Final[str] = "https://earth-search.aws.element84.com/v1"
COLLECTION: Final[str] = "sentinel-2-l2a"

#: Earth Search publishes Sentinel-2 assets under common names rather than the
#: ``B03``-style granule keys. We speak band numbers internally and translate at
#: the STAC boundary.
BAND_ASSETS: Final[dict[str, str]] = {
    "B03": "green",     # 10 m
    "B04": "red",       # 10 m
    "B08": "nir",       # 10 m
    "B11": "swir16",    # 20 m, resampled to 10 m
    "SCL": "scl",       # 20 m, resampled to 10 m with nearest only
}

#: Scene classification classes that make a pixel unusable: 0 nodata,
#: 1 saturated/defective, 3 cloud shadow, 8 cloud medium probability,
#: 9 cloud high probability, 10 thin cirrus, 11 snow/ice.
MASK_CLASSES: Final[frozenset[int]] = frozenset({0, 1, 3, 8, 9, 10, 11})

INDEX_NAMES: Final[tuple[str, ...]] = ("NDVI", "NDMI", "NDWI")

#: Fallback reflectance transform, used only when an item omits ``raster:bands``.
#: Sentinel-2 L2A carries a -0.1 offset that does *not* cancel out of a
#: normalised difference, so it has to be applied rather than assumed away.
DEFAULT_SCALE: Final[float] = 0.0001
DEFAULT_OFFSET: Final[float] = -0.1
DEFAULT_NODATA: Final[int] = 0

SQ_M_PER_ACRE: Final[float] = 4046.8564224


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration for a single CLI invocation."""

    root: Path
    db_path: Path
    clips_dir: Path
    out_dir: Path
    offline: bool = False
    min_valid_fraction: float = 0.70
    target_res_m: int = 10
    max_scene_cloud: float = 80.0
    doy_window: int = 12
    history_years: int = 4
    smooth_window: int = 15
    stac_url: str = STAC_URL
    collection: str = COLLECTION

    @classmethod
    def from_root(cls, root: Path, **overrides: object) -> "Settings":
        """Build settings rooted at ``root``, with ``None`` overrides ignored.

        Lets the CLI pass optional flags straight through without each call site
        having to filter out the ones the user did not supply.
        """
        base = cls(
            root=root,
            db_path=root / "cache" / "dosojos.sqlite",
            clips_dir=root / "cache" / "clips",
            out_dir=root / "out",
        )
        supplied = {k: v for k, v in overrides.items() if v is not None}
        return replace(base, **supplied) if supplied else base

    def ensure_dirs(self) -> None:
        """Create the cache and output directories if they do not exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def clip_path(self, field_id: str, date_iso: str) -> Path:
        """Return the on-disk location of the cached clip for one field and day."""
        return self.clips_dir / field_id / f"{date_iso}.tif"


def default_root() -> Path:
    """Return the project root (the directory holding ``src/``, ``cache/``, ``out/``)."""
    return Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def setup_logging(verbosity: int = 0) -> None:
    """Configure stderr logging; ``verbosity`` >= 1 turns on DEBUG."""
    level = logging.DEBUG if verbosity else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # These libraries are chatty at DEBUG and drown out our own fetch log.
    for noisy in ("urllib3", "rasterio", "botocore", "fiona", "matplotlib"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
