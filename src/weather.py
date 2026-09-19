"""Daily weather for the water checkbook: reference evapotranspiration and rain.

Inside the contiguous US the source is gridMET (Climatology Lab, University of
Idaho): a daily grid of about 4 km, a day or two behind real time. It is read one
point at a time through its THREDDS server's NetCDF Subset Service, so a
field-season costs a few kilobytes of CSV and no NetCDF library.

Anywhere else on Earth (a field in Veracruz, in Tamaulipas) the source is NASA
POWER: a daily grid of about 50 km, free and with no key, from satellite sun and
reanalysis weather. POWER publishes the ingredients rather than ETo, so the
reference evapotranspiration is worked out here with the FAO-56 Penman-Monteith
equation, the same one gridMET's ETo follows. A coarser grid and our own sum make
it a weaker number than gridMET's; every page and text that uses it says so.

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
    # Daily highs and lows, for heat units: sorghum's growth stages follow the
    # temperature it has had, not the calendar. gridMET stores them in kelvin.
    "tmax_c": ("tmmx", "daily_maximum_temperature"),
    "tmin_c": ("tmmn", "daily_minimum_temperature"),
}
#: Columns the water checkbook cannot run without. Temperatures only add growth
#: stages, so a failure fetching them leaves them empty instead of failing the day.
REQUIRED_COLUMNS = ("eto_mm", "rain_mm")
KELVIN_ZERO = 273.15
GRIDMET_SOURCE = "gridmet"
POWER_SOURCE = "power"
#: NASA POWER: daily, global, no key. Their -999 marks a day not published yet.
POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
POWER_PARAMETERS = ("T2M_MAX", "T2M_MIN", "T2MDEW", "PRECTOTCORR", "ALLSKY_SFC_SW_DWN",
                    "RH2M", "WS2M")
POWER_MISSING = -999.0
POWER_FIRST_DAY = date(1981, 1, 1)
#: A hole this many days wide between published days is filled from its neighbours.
POWER_BRIDGE_DAYS = 2
GRIDMET_FIRST_DAY = date(1979, 1, 1)
#: gridMET covers the contiguous US and nothing else.
CONUS_BOUNDS = (-125.0, 24.0, -66.5, 49.5)   # west, south, east, north
#: The US-Mexico border, west to east, as (longitude, latitude) turning points: the
#: land line across California, Arizona and New Mexico, then the Rio Grande down to
#: the Gulf. A box alone would hand Reynosa, Matamoros and Ciudad Juarez to gridMET,
#: which has no data south of the line.
BORDER: tuple[tuple[float, float], ...] = (
    (-117.13, 32.53), (-114.72, 32.72), (-111.07, 31.33), (-108.21, 31.33),
    (-106.53, 31.78), (-105.60, 31.09), (-104.98, 30.62), (-104.68, 29.92),
    (-103.10, 28.98), (-102.39, 29.77), (-101.40, 29.77), (-100.65, 28.71),
    (-99.53, 27.60), (-99.10, 26.42), (-98.35, 26.15), (-97.40, 25.88), (-97.14, 25.95),
)

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


def _border_lat(lon: float) -> float | None:
    """How far north the US-Mexico border runs at this longitude, if it runs there."""
    if not BORDER[0][0] <= lon <= BORDER[-1][0]:
        return None
    lons = [point[0] for point in BORDER]
    lats = [point[1] for point in BORDER]
    return float(np.interp(lon, lons, lats))


def _in_conus(lat: float, lon: float) -> bool:
    """Inside the contiguous US, which is where gridMET has numbers.

    South of the border line there is no gridMET, even where the box says yes.
    """
    west, south, east, north = CONUS_BOUNDS
    if not (west <= lon <= east and south <= lat <= north):
        return False
    line = _border_lat(lon)
    return line is None or lat >= line


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
        try:
            packing = parse_das(
                _request(get, GRIDMET_DAS.format(dataset=dataset), {}, retries), variable)
            text = _request(get, GRIDMET_NCSS.format(dataset=dataset), {
                "var": variable, "latitude": f"{lat:.5f}", "longitude": f"{lon:.5f}",
                "time_start": f"{start.isoformat()}T00:00:00Z",
                "time_end": f"{end.isoformat()}T00:00:00Z",
                "accept": "csv",
            }, retries)
            raw = parse_ncss_csv(text)
        except WeatherError as exc:
            if column in REQUIRED_COLUMNS:
                raise
            log.warning("gridMET %s unavailable, growth stages will wait for it: %s",
                        dataset, exc)
            continue
        values = packing.unpack(raw["value"].to_numpy())
        if column.endswith("_c"):
            values = values - KELVIN_ZERO
        columns[column] = pd.Series(values, index=raw["date"])

    frame = pd.DataFrame(columns).sort_index()
    for column in GRIDMET_VARIABLES:
        if column not in frame:
            frame[column] = np.nan
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


# --------------------------------------------------------------------------- #
# NASA POWER, for fields outside the contiguous US
# --------------------------------------------------------------------------- #


def vapour_kpa(t_c):
    """Saturation vapour pressure at a temperature, kPa (FAO-56 eq. 11)."""
    t = np.asarray(t_c, float)
    return 0.6108 * np.exp(17.27 * t / (t + 237.3))


def eto_penman_monteith(tmax_c, tmin_c, dew_c, wind_2m_ms, solar_mj, lat_deg: float,
                        elevation_m: float, doy):
    """FAO-56 Penman-Monteith reference evapotranspiration, mm a day.

    The standard short-grass reference (Allen et al., FAO Irrigation and Drainage
    Paper 56, 1998), computed from what NASA POWER publishes. Arrays in, array out.
    Humidity enters as the dew point, which FAO-56 prefers: a day's mean relative
    humidity against its mean saturation pressure reads the air too wet and the
    demand about a tenth too low.
    """
    tmax_c, tmin_c = np.asarray(tmax_c, float), np.asarray(tmin_c, float)
    wind = np.clip(np.asarray(wind_2m_ms, float), 0.5, None)     # FAO-56 floor
    solar = np.clip(np.asarray(solar_mj, float), 0, None)
    doy = np.asarray(doy, float)
    tmean = (tmax_c + tmin_c) / 2
    vapour = vapour_kpa

    slope = 4098 * vapour(tmean) / (tmean + 237.3) ** 2
    pressure = 101.3 * ((293 - 0.0065 * elevation_m) / 293) ** 5.26
    gamma = 0.000665 * pressure
    es = (vapour(tmax_c) + vapour(tmin_c)) / 2
    ea = np.minimum(vapour(np.asarray(dew_c, float)), es)
    # Sun at the top of the atmosphere for this day and latitude.
    phi = np.radians(lat_deg)
    dr = 1 + 0.033 * np.cos(2 * np.pi * doy / 365)
    declination = 0.409 * np.sin(2 * np.pi * doy / 365 - 1.39)
    sunset = np.arccos(np.clip(-np.tan(phi) * np.tan(declination), -1, 1))
    ra = (24 * 60 / np.pi) * 0.0820 * dr * (
        sunset * np.sin(phi) * np.sin(declination)
        + np.cos(phi) * np.cos(declination) * np.sin(sunset))
    rso = (0.75 + 2e-5 * elevation_m) * ra
    net_short = (1 - 0.23) * solar
    ratio = np.clip(np.divide(solar, np.where(rso > 0, rso, np.nan)), 0.3, 1.0)
    net_long = (4.903e-9 * ((tmax_c + 273.16) ** 4 + (tmin_c + 273.16) ** 4) / 2
                * (0.34 - 0.14 * np.sqrt(np.clip(ea, 0, None)))
                * (1.35 * ratio - 0.35))
    net = net_short - net_long
    eto = ((0.408 * slope * net + gamma * 900 / (tmean + 273) * wind * (es - ea))
           / (slope + gamma * (1 + 0.34 * wind)))
    return np.clip(eto, 0, None)


def fetch_power(lat: float, lon: float, start: date, end: date, *,
                get: Callable | None = None, retries: int = RETRIES) -> pd.DataFrame:
    """Daily ETo, rain and temperatures at one point anywhere on Earth.

    NASA POWER gives sun, temperature, humidity and wind; ETo is worked out from
    them with FAO-56. Days POWER has not published yet are left out.

    Raises:
        WeatherError: when POWER cannot be reached or answers with nothing usable.
    """
    if end < start:
        raise WeatherError(f"weather window ends ({end}) before it starts ({start})")
    start = max(start, POWER_FIRST_DAY)
    if get is None:
        import requests

        get = requests.get
    text = _request(get, POWER_URL, {
        "parameters": ",".join(POWER_PARAMETERS), "community": "AG",
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"), "format": "JSON",
    }, retries)
    return parse_power(text, lat)


def parse_power(text: str, lat: float) -> pd.DataFrame:
    """POWER's JSON into the columns the checkbook reads."""
    import json

    try:
        payload = json.loads(text)
        values = payload["properties"]["parameter"]
        elevation = float(payload["geometry"]["coordinates"][2])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise WeatherError(f"NASA POWER answered with something unexpected: {text[:200]!r}") \
            from exc
    missing = [name for name in POWER_PARAMETERS if name not in values]
    if missing:
        raise WeatherError(f"NASA POWER left out {', '.join(missing)}")
    frame = pd.DataFrame({name: pd.Series(values[name]) for name in POWER_PARAMETERS})
    frame = frame.replace(POWER_MISSING, np.nan)
    # A missing dew point can be stood in for by the day's relative humidity; a day
    # missing anything else cannot be turned into an ETo at all.
    dew = frame["T2MDEW"]
    if dew.isna().any():
        es = (vapour_kpa(frame["T2M_MAX"]) + vapour_kpa(frame["T2M_MIN"])) / 2
        ea = es * np.clip(frame["RH2M"], 1, 100) / 100
        stood_in = 237.3 * np.log(ea / 0.6108) / (17.27 - np.log(ea / 0.6108))
        frame["T2MDEW"] = dew.fillna(stood_in)
    # POWER drops a day here and there in one measurement or another. A hole of a
    # day or two between published days is bridged from its neighbours; the balance
    # refuses a gap it cannot see, and a week of missing sun is not bridgeable.
    frame.index = pd.to_datetime(frame.index, format="%Y%m%d")
    frame = frame.reindex(pd.date_range(frame.index.min(), frame.index.max(), freq="D"))
    frame = frame.interpolate(limit=POWER_BRIDGE_DAYS, limit_area="inside").dropna()
    if frame.empty:
        raise WeatherError("NASA POWER has no published days in that window yet")
    days = frame.index
    eto = eto_penman_monteith(frame["T2M_MAX"], frame["T2M_MIN"], frame["T2MDEW"],
                              frame["WS2M"], frame["ALLSKY_SFC_SW_DWN"], lat, elevation,
                              days.dayofyear)
    return pd.DataFrame({
        "date": days.date, "eto_mm": np.round(eto, 3),
        "rain_mm": frame["PRECTOTCORR"].to_numpy(),
        "tmax_c": frame["T2M_MAX"].to_numpy(), "tmin_c": frame["T2M_MIN"].to_numpy(),
    }).sort_values("date").reset_index(drop=True)


def source_for(lat: float, lon: float) -> str:
    """Which grid covers this field: gridMET in the lower 48, NASA POWER elsewhere."""
    return GRIDMET_SOURCE if _in_conus(lat, lon) else POWER_SOURCE


def fetch(lat: float, lon: float, start: date, end: date, *, get: Callable | None = None,
          retries: int = RETRIES) -> tuple[pd.DataFrame, str]:
    """The daily weather for one field, from whichever grid covers it."""
    source = source_for(lat, lon)
    if source == GRIDMET_SOURCE:
        try:
            frame = fetch_gridmet(lat, lon, start, end, get=get, retries=retries)
            if not frame.empty and frame["eto_mm"].notna().any():
                return frame, source
            why = "gridMET has no numbers at that point"
        except WeatherError as exc:
            why = str(exc)
        # A field on the river can sit a cell outside the grid: the world one covers it.
        log.info("%s; falling back to NASA POWER", why)
    return fetch_power(lat, lon, start, end, get=get, retries=retries), POWER_SOURCE
