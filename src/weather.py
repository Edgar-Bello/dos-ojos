"""Daily weather for the water checkbook: reference evapotranspiration and rain.

The default source is gridMET (Climatology Lab, University of Idaho): a daily
grid of about 4 km over the contiguous US, a day or two behind real time. It is
read one point at a time through its THREDDS server's NetCDF Subset Service, so
a field-season costs a few kilobytes of CSV and no NetCDF library.

ETo is the short-grass reference evapotranspiration, how fast a well-watered
lawn would use water in that day's sun, heat, wind and humidity. A crop uses
some multiple of it, which is what :mod:`water` works out from the satellite.

A local station (a TexasET download, a farm's own) can be loaded from a CSV and
is preferred wherever it has a reading.
"""

from __future__ import annotations

import io
import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

GRIDMET_NCSS = (
    "http://thredds.northwestknowledge.net:8080/thredds/ncss/"
    "agg_met_{dataset}_1979_CurrentYear_CONUS.nc"
)
GRIDMET_DAS = (
    "http://thredds.northwestknowledge.net:8080/thredds/dodsC/"
    "agg_met_{dataset}_1979_CurrentYear_CONUS.nc.das"
)
#: Our column -> (gridMET dataset, variable name).
GRIDMET_VARIABLES: dict[str, tuple[str, str]] = {
    "eto_mm": ("pet", "daily_mean_reference_evapotranspiration_grass"),
    "rain_mm": ("pr", "precipitation_amount"),
}
GRIDMET_SOURCE = "gridmet"
GRIDMET_FIRST_DAY = date(1979, 1, 1)
#: gridMET covers the contiguous US and nothing else.
CONUS_BOUNDS = (-125.0, 24.0, -66.5, 49.5)   # west, south, east, north

MM_PER_INCH = 25.4
RETRIES = 3
TIMEOUT_S = 60


class WeatherError(RuntimeError):
    """Raised when weather cannot be fetched or a station file cannot be read."""


@dataclass(frozen=True)
class Packing:
    """How a gridMET variable is stored: ``value = raw * scale + offset``."""

    scale: float = 1.0
    offset: float = 0.0
    missing: float | None = None

    def unpack(self, raw: np.ndarray) -> np.ndarray:
        """Turn stored integers into physical values, with NaN for missing."""
        values = np.asarray(raw, dtype=float)
        if self.missing is not None:
            values = np.where(values == self.missing, np.nan, values)
        return values * self.scale + self.offset


# --------------------------------------------------------------------------- #
# gridMET
# --------------------------------------------------------------------------- #


def parse_das(text: str, variable: str) -> Packing:
    """Read a variable's scale, offset and missing value from an OPeNDAP DAS.

    The subset service hands back the stored integers (69 for 6.9 mm), so the
    packing has to be read from the dataset rather than assumed.
    """
    start = text.find(f"{variable} {{")
    if start < 0:
        raise WeatherError(f"gridMET metadata has no variable {variable!r}")
    block = text[start:text.find("}", start)]

    def number(name: str) -> float | None:
        match = re.search(rf"\b{name}\s+(-?[\d.eE+-]+);", block)
        return float(match.group(1)) if match else None

    return Packing(
        scale=number("scale_factor") or 1.0,
        offset=number("add_offset") or 0.0,
        missing=number("missing_value") if number("missing_value") is not None
        else number("_FillValue"),
    )


def parse_ncss_csv(text: str) -> pd.DataFrame:
    """Parse a point subset: ``time, latitude, longitude, <variable>`` rows.

    Returns ``date`` and the raw ``value`` column, still packed.
    """
    try:
        frame = pd.read_csv(io.StringIO(text))
    except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise WeatherError(f"gridMET returned something that is not CSV: {text[:200]!r}") from exc
    if frame.shape[1] < 4 or not str(frame.columns[0]).startswith("time"):
        raise WeatherError(f"unexpected gridMET columns: {list(frame.columns)}")
    return pd.DataFrame({
        "date": pd.to_datetime(frame.iloc[:, 0], utc=True).dt.date,
        "value": pd.to_numeric(frame.iloc[:, -1], errors="coerce"),
    })


def _in_conus(lat: float, lon: float) -> bool:
    west, south, east, north = CONUS_BOUNDS
    return west <= lon <= east and south <= lat <= north


def fetch_gridmet(
    lat: float,
    lon: float,
    start: date,
    end: date,
    *,
    get: Callable | None = None,
    retries: int = RETRIES,
) -> pd.DataFrame:
    """Daily ETo and rain in millimetres at one point, ``start`` to ``end`` inclusive.

    Days not yet published (gridMET runs a day or two behind) are simply absent.

    Args:
        get: ``requests.get``-compatible callable, replaceable in tests.

    Raises:
        WeatherError: outside the contiguous US, or when the server cannot be
            reached after ``retries`` attempts.
    """
    if not _in_conus(lat, lon):
        raise WeatherError(
            f"({lat:.3f}, {lon:.3f}) is outside the contiguous US, which is all gridMET "
            "covers. Load a local station instead: dosojos-sat weather --station <csv>"
        )
    if end < start:
        raise WeatherError(f"weather window ends ({end}) before it starts ({start})")
    start = max(start, GRIDMET_FIRST_DAY)
    if get is None:
        import requests

        get = requests.get

    columns: dict[str, pd.Series] = {}
    for column, (dataset, variable) in GRIDMET_VARIABLES.items():
        packing = parse_das(_request(get, GRIDMET_DAS.format(dataset=dataset), {}, retries),
                            variable)
        text = _request(get, GRIDMET_NCSS.format(dataset=dataset), {
            "var": variable, "latitude": f"{lat:.5f}", "longitude": f"{lon:.5f}",
            "time_start": f"{start.isoformat()}T00:00:00Z",
            "time_end": f"{end.isoformat()}T00:00:00Z",
            "accept": "csv",
        }, retries)
        raw = parse_ncss_csv(text)
        columns[column] = pd.Series(packing.unpack(raw["value"].to_numpy()),
                                    index=raw["date"])

    frame = pd.DataFrame(columns).sort_index()
    frame.index.name = "date"
    return frame.reset_index()


def _request(get: Callable, url: str, params: dict, retries: int) -> str:
    """GET with backoff, returning the body or raising an actionable error."""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = get(url, params=params, timeout=TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - any transport failure is retried
            last = exc
        else:
            if response.status_code == 200:
                return response.text
            if 400 <= response.status_code < 500:
                raise WeatherError(
                    f"gridMET refused the request ({response.status_code}): "
                    f"{response.text[:300].strip()}"
                )
            last = RuntimeError(f"HTTP {response.status_code}")
        if attempt < retries:
            time.sleep(2 ** (attempt - 1))
    raise WeatherError(
        f"could not reach gridMET at {url.split('/thredds')[0]} after {retries} tries "
        f"({last}). Check the connection, or load a station file with --station."
    )


# --------------------------------------------------------------------------- #
# Station files
# --------------------------------------------------------------------------- #

_DATE_NAMES = ("date", "day", "fecha")
_ETO_WORDS = ("eto", "et0", "reference", "ref_et", "pet")
_RAIN_WORDS = ("rain", "precip", "precipitation", "lluvia", "pr")


def _unit_of(header: str) -> str | None:
    """'mm' or 'in' from a header such as 'ETo (in)' or 'rain_mm', else None."""
    words = set(re.split(r"[^a-z]+", header.lower()))
    if words & {"mm", "millimeters", "millimetres"}:
        return "mm"
    if words & {"in", "inch", "inches"}:
        return "in"
    return None


def _find(headers: list[str], words: tuple[str, ...]) -> str | None:
    """First header naming one of ``words``, as a whole token or a long-enough prefix."""
    for header in headers:
        tokens = set(re.split(r"[^a-z0-9]+", header.lower()))
        # Prefixes only for longer words, so 'pr' cannot claim 'pressure'.
        if tokens & set(words) or any(
            header.lower().startswith(w) for w in words if len(w) >= 4
        ):
            return header
    return None


def read_station_csv(path: Path) -> pd.DataFrame:
    """Read a station's daily ETo and rain, converting inches to millimetres.

    Headers are matched loosely (``date``; ``ETo (in)`` or ``eto_mm``; ``rain``
    or ``precip`` with a unit), but the unit must be named: a bare ``ETo`` could
    be either, and a factor of 25 is not something to guess.

    Raises:
        WeatherError: if the file lacks a date or ETo column, or a unit.
    """
    path = Path(path)
    try:
        frame = pd.read_csv(path)
    except (OSError, pd.errors.ParserError) as exc:
        raise WeatherError(f"could not read {path}: {exc}") from exc
    headers = [str(h) for h in frame.columns]

    date_col = next((h for h in headers if h.strip().lower() in _DATE_NAMES), None)
    eto_col = _find(headers, _ETO_WORDS)
    if date_col is None or eto_col is None:
        raise WeatherError(
            f"{path.name} needs a 'date' column and an ETo column such as 'eto_mm' or "
            f"'ETo (in)'; found {headers}"
        )
    rain_col = _find([h for h in headers if h not in (date_col, eto_col)], _RAIN_WORDS)

    out = pd.DataFrame({"date": pd.to_datetime(frame[date_col], errors="coerce").dt.date})
    for column, header in (("eto_mm", eto_col), ("rain_mm", rain_col)):
        if header is None:
            out[column] = np.nan
            continue
        unit = _unit_of(header)
        if unit is None:
            raise WeatherError(
                f"{path.name}: column {header!r} does not say its unit. Rename it to "
                f"'{column.split('_')[0]}_mm' or '{column.split('_')[0]}_in'."
            )
        values = pd.to_numeric(frame[header], errors="coerce")
        out[column] = values * (MM_PER_INCH if unit == "in" else 1.0)

    bad = out["date"].isna().sum()
    if bad:
        log.warning("%s: skipped %d row(s) with an unreadable date", path.name, bad)
    out = out.dropna(subset=["date"]).drop_duplicates("date", keep="last")
    if out.empty:
        raise WeatherError(f"{path.name} holds no dated rows")
    if (out["eto_mm"] > 20).any():
        raise WeatherError(
            f"{path.name}: ETo above 20 mm/day; if the column is in inches, say so "
            "in its name, e.g. 'eto_in'"
        )
    return out.sort_values("date").reset_index(drop=True)


def station_source(path: Path) -> str:
    """The ``source`` label a station file's rows are stored under."""
    return f"station:{Path(path).name}"
