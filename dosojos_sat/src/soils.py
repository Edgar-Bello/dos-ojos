"""How much water each field's soil can hold: the USDA survey, or SoilGrids abroad.

The checkbook needs one number above all: available water, the water a soil
holds between full (field capacity) and dry enough to wilt a plant. SSURGO
publishes it per map unit already summed to 25, 50, 100 and 150 cm, so the water
in any root zone is an interpolation along that curve, and shallow or layered
soils are handled by the survey rather than by us.

Queried through Soil Data Access with the field outline itself; where several
soils share a field, each counts by the share of the field it covers. The
surface texture and hydrologic group come along too, because how fast water
soaks in decides how a field should be watered.

SSURGO stops at the US border. A field in Mexico is read from **SoilGrids**
instead (ISRIC, CC-BY 4.0): a 250 m global map of what the soil is made of, layer
by layer. It does not publish available water, so the water each layer holds is
worked out here from its sand, clay and organic matter with the Saxton & Rawls
(2006) equations, the standard way to get it from a soil's make-up. That is a
step further from measurement than SSURGO's own figure, and every page says so.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

import numpy as np
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)

SDA_URL = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"
SOILGRIDS_URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"
SOILGRIDS_SOURCE = "soilgrids"
#: SoilGrids layers, top down, with the depth each one ends at, in metres.
SOILGRIDS_DEPTHS: tuple[tuple[str, float], ...] = (
    ("0-5cm", 0.05), ("5-15cm", 0.15), ("15-30cm", 0.30),
    ("30-60cm", 0.60), ("60-100cm", 1.00), ("100-200cm", 2.00),
)
#: What each layer is made of: SoilGrids name -> what to divide its value by.
SOILGRIDS_PROPERTIES: dict[str, float] = {
    "sand": 10.0,      # g/kg -> %
    "clay": 10.0,      # g/kg -> %
    "soc": 10.0,       # dg/kg -> g/kg
    "bdod": 100.0,     # cg/cm3 -> kg/dm3
    "cfvo": 10.0,      # cm3/dm3 -> % of the volume that is stones
}
#: SSURGO's pre-summed available water storage, cm of water to each depth.
STORAGE_DEPTHS_M: tuple[float, ...] = (0.25, 0.5, 1.0, 1.5)
STORAGE_COLUMNS: tuple[str, ...] = ("aws025wta", "aws050wta", "aws0100wta", "aws0150wta")
CM_PER_FOOT = 30.48
TIMEOUT_S = 90
#: A page where data should be is tried this many times, this far apart.
QUERY_ATTEMPTS = 3
RETRY_WAIT_S = 10


class SoilError(RuntimeError):
    """Raised when a field's soil cannot be read from the survey."""


@dataclass(frozen=True)
class SoilProfile:
    """Water a field's soil holds, and how quickly water gets into it.

    ``storage_cm`` is the available water, in cm, held from the surface down to
    each of :data:`STORAGE_DEPTHS_M`.
    """

    storage_cm: tuple[float, ...]
    name: str
    source: str                      # "ssurgo" or "manual"
    hydgrp: str | None = None        # hydrologic group, A (fast) to D (slow)
    drainage: str | None = None
    clay_pct: float | None = None    # surface horizon
    sand_pct: float | None = None
    ksat_um_s: float | None = None
    map_units: tuple[dict, ...] = field(default_factory=tuple)

    def taw_mm(self, depth_m: float) -> float:
        """Available water, in mm, held from the surface down to ``depth_m``.

        Past the survey's 150 cm the curve continues at its 100-150 cm rate.
        """
        depths = (0.0, *STORAGE_DEPTHS_M)
        storage = (0.0, *self.storage_cm)
        if depth_m <= depths[-1]:
            return float(np.interp(depth_m, depths, storage)) * 10.0
        rate = (storage[-1] - storage[-2]) / (depths[-1] - depths[-2])
        return (storage[-1] + rate * (depth_m - depths[-1])) * 10.0

    @property
    def awc_in_per_ft(self) -> float:
        """Inches of water a foot of soil holds, over the top metre."""
        return self.storage_cm[2] / 100.0 * 12.0

    @property
    def intake(self) -> str:
        """How fast irrigation water soaks in: ``slow``, ``moderate`` or ``fast``.

        Hydrologic group when the survey gives one; otherwise surface texture.
        """
        group = (self.hydgrp or "").upper()
        if group.startswith("D") or group.startswith("C/D") or (
            not group and self.clay_pct is not None and self.clay_pct >= 40
        ):
            return "slow"
        if group.startswith("A") or (
            not group and self.sand_pct is not None and self.sand_pct >= 70
        ):
            return "fast"
        return "moderate"

    @property
    def texture(self) -> str:
        """A plain word for the surface soil: clay, clay loam, loam or sandy."""
        clay, sand = self.clay_pct, self.sand_pct
        if clay is None or sand is None:
            return "unknown"
        if clay >= 40:
            return "clay"
        if clay >= 27:
            return "clay loam"
        if sand >= 52:
            return "sandy"
        return "loam"

    def to_dict(self) -> dict:
        """JSON-ready form for the cache and the output file."""
        payload = asdict(self)
        payload["storage_cm"] = list(self.storage_cm)
        payload["map_units"] = list(self.map_units)
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "SoilProfile":
        """Rebuild a profile stored by :meth:`to_dict`."""
        values = dict(payload)
        values["storage_cm"] = tuple(values["storage_cm"])
        values["map_units"] = tuple(values.get("map_units") or ())
        return cls(**values)


def manual_profile(awc_in_per_ft: float) -> SoilProfile:
    """A uniform soil from one measured number, inches of water per foot."""
    per_cm = awc_in_per_ft / 12.0          # cm of water per cm of soil
    return SoilProfile(
        storage_cm=tuple(per_cm * depth * 100.0 for depth in STORAGE_DEPTHS_M),
        name=f"given: {awc_in_per_ft:.2f} in/ft",
        source="manual",
    )


# --------------------------------------------------------------------------- #
# Soil Data Access
# --------------------------------------------------------------------------- #


def _rows(payload: dict) -> list[dict]:
    """SDA's ``JSON+COLUMNNAME`` table as a list of dicts (first row is the header)."""
    table = payload.get("Table") or []
    if not table:
        return []
    header, *body = table
    return [dict(zip(header, row)) for row in body]


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _query(post: Callable, sql: str, *, attempts: int = QUERY_ATTEMPTS,
           sleep: Callable[[float], None] = time.sleep) -> list[dict]:
    """Run one Soil Data Access query, raising :class:`SoilError` on failure.

    The service goes down for maintenance every night and then answers 200 with
    a web page instead of data. That is said plainly rather than retried, since
    it lasts a quarter of an hour; any other page that is not data is tried
    again a couple of times first.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = post(SDA_URL, json={"query": sql, "format": "JSON+COLUMNNAME"},
                            timeout=TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - any transport failure is fatal here
            raise SoilError(
                f"could not reach USDA Soil Data Access ({exc}). Check the connection, or "
                "set soil_awc_in_ft on the field in fields.geojson."
            ) from exc
        if response.status_code != 200:
            raise SoilError(
                f"Soil Data Access refused the query ({response.status_code}): "
                f"{response.text[:300].strip()}"
            )
        text = response.text.strip()
        if not text:
            return []
        try:
            return _rows(response.json())
        except ValueError:
            if "maintenance" in text.lower():
                raise SoilError(
                    "USDA Soil Data Access is down for its daily maintenance (12:30 to "
                    "12:45 AM Central). Try again after that."
                ) from None
            if attempt == attempts:
                raise SoilError(
                    f"Soil Data Access answered with something that is not data, "
                    f"{attempts} times: {text[:200]}"
                ) from None
            log.info("Soil Data Access answered with a page, not data; trying again")
            sleep(RETRY_WAIT_S)
    return []


def fetch_ssurgo(geometry: BaseGeometry, *, post: Callable | None = None) -> SoilProfile:
    """Read the soils under a WGS84 field outline and combine them by area.

    Args:
        post: ``requests.post``-compatible callable, replaceable in tests.

    Raises:
        SoilError: when the survey cannot be reached, has no soils there (outside
            the US), or none of them carries water storage (water, urban land).
    """
    if post is None:
        import requests

        post = requests.post

    wkt = geometry.wkt
    shape = f"geometry::STGeomFromText('{wkt}', 4326).MakeValid()"
    units = _query(post, f"""
        SELECT mp.mukey, SUM(mp.mupolygongeo.STIntersection({shape}).STArea()) AS area
        FROM mupolygon AS mp
        WHERE mp.mupolygongeo.STIntersects({shape}) = 1
        GROUP BY mp.mukey
    """)
    areas = {str(u["mukey"]): _number(u["area"]) or 0.0 for u in units}
    total = sum(areas.values())
    if not areas or total <= 0:
        raise SoilError(
            "the soil survey has no soils under this outline (SSURGO covers the US "
            "only). Set soil_awc_in_ft on the field instead."
        )
    keys = ",".join(f"'{key}'" for key in areas)

    attributes = _query(post, f"""
        SELECT mu.mukey, mu.muname, {', '.join('ma.' + c for c in STORAGE_COLUMNS)},
               ma.hydgrpdcd, ma.drclassdcd
        FROM mapunit AS mu JOIN muaggatt AS ma ON ma.mukey = mu.mukey
        WHERE mu.mukey IN ({keys})
    """)
    surface = _query(post, f"""
        SELECT c.mukey, c.comppct_r, ch.claytotal_r, ch.sandtotal_r, ch.ksat_r
        FROM component AS c JOIN chorizon AS ch ON ch.cokey = c.cokey
        WHERE c.mukey IN ({keys}) AND c.majcompflag = 'Yes' AND ch.hzdept_r = 0
    """)
    return combine_map_units(areas, attributes, surface)


def combine_map_units(
    areas: dict[str, float], attributes: list[dict], surface: list[dict]
) -> SoilProfile:
    """Weight each map unit's storage and texture by the share of field it covers.

    Map units with no water storage (open water, urban land, pits) drop out and
    the rest are re-weighted; the field is named after its largest soil.
    """
    total = sum(areas.values())
    units = []
    for row in attributes:
        key = str(row["mukey"])
        storage = [_number(row.get(c)) for c in STORAGE_COLUMNS]
        units.append({
            "mukey": key, "name": row.get("muname") or key,
            "share": areas.get(key, 0.0) / total if total else 0.0,
            "storage_cm": storage, "hydgrp": row.get("hydgrpdcd"),
            "drainage": row.get("drclassdcd"),
        })
    units.sort(key=lambda u: -u["share"])
    usable = [u for u in units if all(s is not None for s in u["storage_cm"])]
    if not usable:
        names = ", ".join(u["name"] for u in units) or "none"
        raise SoilError(
            f"none of the soils under this outline ({names}) has water storage in the "
            "survey. Set soil_awc_in_ft on the field instead."
        )
    weight = sum(u["share"] for u in usable)
    storage = tuple(
        sum(u["share"] * u["storage_cm"][i] for u in usable) / weight
        for i in range(len(STORAGE_COLUMNS))
    )

    texture: dict[str, list[float]] = {"clay": [], "sand": [], "ksat": [], "w": []}
    share_of = {u["mukey"]: u["share"] for u in units}
    for row in surface:
        w = share_of.get(str(row["mukey"]), 0.0) * (_number(row.get("comppct_r")) or 0.0)
        clay, sand, ksat = (_number(row.get(k)) for k in ("claytotal_r", "sandtotal_r", "ksat_r"))
        if w > 0 and clay is not None and sand is not None:
            texture["clay"].append(clay)
            texture["sand"].append(sand)
            texture["ksat"].append(ksat if ksat is not None else np.nan)
            texture["w"].append(w)

    def weighted(key: str) -> float | None:
        values = np.array(texture[key], dtype=float)
        weights = np.array(texture["w"], dtype=float)
        ok = np.isfinite(values)
        if not ok.any():
            return None
        return round(float(np.average(values[ok], weights=weights[ok])), 2)

    dominant = usable[0]
    return SoilProfile(
        storage_cm=tuple(round(s, 3) for s in storage),
        name=dominant["name"],
        source="ssurgo",
        hydgrp=dominant["hydgrp"],
        drainage=dominant["drainage"],
        clay_pct=weighted("clay"),
        sand_pct=weighted("sand"),
        ksat_um_s=weighted("ksat"),
        map_units=tuple(
            {"mukey": u["mukey"], "name": u["name"], "share": round(u["share"], 4)}
            for u in units
        ),
    )


# --------------------------------------------------------------------------- #
# SoilGrids, for fields outside the USDA survey
# --------------------------------------------------------------------------- #


def available_water_fraction(sand_pct: float, clay_pct: float, organic_pct: float) -> float:
    """Water a soil holds between field capacity and wilting, as a fraction of its volume.

    Saxton, K. E., and Rawls, W. J. (2006), *Soil water characteristic estimates by
    texture and organic matter for hydrologic solutions*, Soil Science Society of
    America Journal 70(5):1569-1578. Their equations 1 and 2, with the corrections
    that follow them; organic matter is held to the 8% their fit covers.
    """
    sand, clay = sand_pct / 100.0, clay_pct / 100.0
    organic = min(max(organic_pct, 0.0), 8.0)
    wilt_t = (-0.024 * sand + 0.487 * clay + 0.006 * organic + 0.005 * sand * organic
              - 0.013 * clay * organic + 0.068 * sand * clay + 0.031)
    wilt = wilt_t + (0.14 * wilt_t - 0.02)
    capacity_t = (-0.251 * sand + 0.195 * clay + 0.011 * organic + 0.006 * sand * organic
                  - 0.027 * clay * organic + 0.452 * sand * clay + 0.299)
    capacity = capacity_t + (1.283 * capacity_t ** 2 - 0.374 * capacity_t - 0.015)
    return float(max(capacity - wilt, 0.0))


def _soilgrids_layers(payload: dict) -> dict[str, dict[str, float]]:
    """``{"0-5cm": {"sand": 46.2, "clay": 27.6, ...}, ...}`` from SoilGrids' JSON."""
    layers: dict[str, dict[str, float]] = {}
    for layer in (payload.get("properties") or {}).get("layers") or []:
        name = layer.get("name")
        divide = SOILGRIDS_PROPERTIES.get(name)
        if divide is None:
            continue
        for depth in layer.get("depths") or []:
            value = (depth.get("values") or {}).get("mean")
            if value is not None:
                layers.setdefault(depth.get("label"), {})[name] = float(value) / divide
    return layers


def profile_from_soilgrids(payload: dict, *, place: str = "") -> SoilProfile:
    """A soil profile from one SoilGrids point answer."""
    layers = _soilgrids_layers(payload)
    usable = [(label, ends) for label, ends in SOILGRIDS_DEPTHS
              if len(layers.get(label, {})) >= 4]
    if not usable:
        raise SoilError(
            "SoilGrids has no soil at that point (it may be water, rock or city). Check the "
            "field's map, or give the soil by hand: dosojos-sat soil --awc <inches per foot>")
    storage, top = [], 0.0
    running, edges = 0.0, []
    for label, ends in usable:
        made_of = layers[label]
        awc = available_water_fraction(made_of.get("sand", 45.0), made_of.get("clay", 20.0),
                                       made_of.get("soc", 10.0) / 10.0 * 1.724)
        stones = min(max(made_of.get("cfvo", 0.0), 0.0), 90.0) / 100.0
        running += awc * (1 - stones) * (ends - top) * 100.0      # cm of water
        edges.append((ends, running))
        top = ends
    for depth in STORAGE_DEPTHS_M:
        if depth <= edges[-1][0]:
            storage.append(float(np.interp(depth, [0.0] + [e for e, _ in edges],
                                           [0.0] + [c for _, c in edges])))
        else:                                   # deeper than SoilGrids goes: keep the rate
            last_depth, last_cm = edges[-1]
            rate = last_cm / last_depth
            storage.append(last_cm + rate * (depth - last_depth))
    surface = layers[usable[0][0]]
    clay, sand = surface.get("clay"), surface.get("sand")
    profile = SoilProfile(
        storage_cm=tuple(round(value, 2) for value in storage),
        name="", source=SOILGRIDS_SOURCE, clay_pct=clay, sand_pct=sand,
        map_units=({"label": label, **{k: round(v, 2) for k, v in layers[label].items()}}
                   for label, _ in usable) and tuple(
            {"layer": label, **{k: round(v, 2) for k, v in layers[label].items()}}
            for label, _ in usable),
    )
    named = f"{profile.texture} soil" + (f" near {place}" if place else "")
    return SoilProfile(**{**profile.to_dict(), "storage_cm": profile.storage_cm,
                          "map_units": profile.map_units, "name": named})


def fetch_soilgrids(geometry: BaseGeometry, *, get: Callable | None = None,
                    attempts: int = QUERY_ATTEMPTS) -> SoilProfile:
    """The soil under a field anywhere on Earth, from SoilGrids' 250 m maps.

    Read at the field's middle: a 250 m cell is wider than most fields here, so
    reading every corner would return the same numbers.

    Raises:
        SoilError: when SoilGrids cannot be reached or has no soil at that point.
    """
    if get is None:
        import requests

        get = requests.get
    point = geometry.centroid
    params = [("lat", f"{point.y:.5f}"), ("lon", f"{point.x:.5f}"), ("value", "mean")]
    params += [("property", name) for name in SOILGRIDS_PROPERTIES]
    params += [("depth", label) for label, _ in SOILGRIDS_DEPTHS]
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            response = get(SOILGRIDS_URL, params=params, timeout=TIMEOUT_S)
            if response.status_code == 429:               # ISRIC asks for five a minute
                last = "SoilGrids asked us to slow down"
                log.info("SoilGrids is rate limiting; waiting %s s", RETRY_WAIT_S)
                time.sleep(RETRY_WAIT_S)
                continue
            payload = response.json()
        except Exception as exc:                          # requests, JSON, anything
            last = f"could not reach SoilGrids ({type(exc).__name__})"
            log.info("%s; try %s of %s", last, attempt, attempts)
            time.sleep(RETRY_WAIT_S if attempt < attempts else 0)
            continue
        return profile_from_soilgrids(payload)
    raise SoilError(f"{last}. Try again, or give the soil by hand: "
                    "dosojos-sat soil --awc <inches per foot>")
