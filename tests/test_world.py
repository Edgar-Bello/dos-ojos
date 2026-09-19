"""Fields outside the US: NASA POWER weather and SoilGrids soil.

gridMET and the USDA survey stop at the border, so a field in Veracruz is read
from the world grids instead. The answers here are recorded from the real
services, so these run with no network.
"""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest
from shapely.geometry import Point, Polygon

from dosojos_sat import soils, weather

# Two days near Veracruz, as NASA POWER answered on 2026-09-19; the second day is
# one POWER has not published yet, which it marks -999.
POWER_JSON = json.dumps({
    "geometry": {"type": "Point", "coordinates": [-96.9, 19.5, 1133.17]},
    "properties": {"parameter": {
        "T2M_MAX": {"20260910": 27.29, "20260911": 27.22, "20260912": -999.0},
        "T2M_MIN": {"20260910": 18.62, "20260911": 18.9, "20260912": -999.0},
        "T2MDEW": {"20260910": 18.1, "20260911": 18.4, "20260912": -999.0},
        "PRECTOTCORR": {"20260910": 6.3, "20260911": 2.18, "20260912": -999.0},
        "ALLSKY_SFC_SW_DWN": {"20260910": 23.09, "20260911": 21.56, "20260912": -999.0},
        "RH2M": {"20260910": 82.58, "20260911": 80.41, "20260912": -999.0},
        "WS2M": {"20260910": 0.97, "20260911": 1.11, "20260912": -999.0},
    }},
})

# SoilGrids at the same point: the top three layers, as it answered.
SOILGRIDS_JSON = {
    "properties": {"layers": [
        {"name": "clay", "depths": [{"label": "0-5cm", "values": {"mean": 276}},
                                    {"label": "5-15cm", "values": {"mean": 275}},
                                    {"label": "15-30cm", "values": {"mean": 328}}]},
        {"name": "sand", "depths": [{"label": "0-5cm", "values": {"mean": 462}},
                                    {"label": "5-15cm", "values": {"mean": 455}},
                                    {"label": "15-30cm", "values": {"mean": 414}}]},
        {"name": "soc", "depths": [{"label": "0-5cm", "values": {"mean": 523}},
                                   {"label": "5-15cm", "values": {"mean": 378}},
                                   {"label": "15-30cm", "values": {"mean": 230}}]},
        {"name": "bdod", "depths": [{"label": "0-5cm", "values": {"mean": 97}},
                                    {"label": "5-15cm", "values": {"mean": 96}},
                                    {"label": "15-30cm", "values": {"mean": 100}}]},
        {"name": "cfvo", "depths": [{"label": "0-5cm", "values": {"mean": 105}},
                                    {"label": "5-15cm", "values": {"mean": 116}},
                                    {"label": "15-30cm", "values": {"mean": 139}}]},
    ]},
}


class Answer:
    def __init__(self, text: str = "", payload: dict | None = None, status: int = 200):
        self.text, self._payload, self.status_code = text, payload, status

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload

    def raise_for_status(self):
        pass


# --------------------------------------------------------------------------- #
# Which grid covers a field
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("lat, lon, source", [
    (26.1484, -97.9940, "gridmet"),      # Weslaco, in the Valley
    (19.5000, -96.9000, "power"),        # Veracruz
    (41.8800, -87.6300, "gridmet"),      # Chicago
    (20.6700, -103.3500, "power"),       # Guadalajara
    (19.4326, -99.1332, "power"),        # Mexico City
])
def test_the_grid_that_covers_the_field_is_the_one_used(lat, lon, source) -> None:
    assert weather.source_for(lat, lon) == source


@pytest.mark.parametrize("name, lat, lon, source", [
    ("McAllen", 26.20, -98.23, "gridmet"), ("Reynosa", 26.05, -98.28, "power"),
    ("Brownsville", 25.95, -97.48, "gridmet"), ("Matamoros", 25.87, -97.50, "power"),
    ("El Paso", 31.80, -106.44, "gridmet"), ("Ciudad Juarez", 31.69, -106.42, "power"),
    ("Tucson", 32.22, -110.97, "gridmet"), ("Nogales, Sonora", 31.31, -110.94, "power"),
])
def test_twin_cities_across_the_river_get_different_grids(name, lat, lon, source) -> None:
    """A box alone would hand Reynosa to gridMET, which has nothing south of the line."""
    assert weather.source_for(lat, lon) == source, name


def test_a_field_gridmet_cannot_answer_for_falls_back_to_the_world_grid() -> None:
    def get(url, params=None, timeout=None):
        if url.startswith(weather.POWER_URL):
            return Answer(POWER_JSON)
        raise RuntimeError("gridMET is not answering")

    frame, source = weather.fetch(26.15, -97.99, date(2026, 9, 10), date(2026, 9, 12),
                                  get=get, retries=1)
    assert source == "power" and not frame.empty


# --------------------------------------------------------------------------- #
# NASA POWER
# --------------------------------------------------------------------------- #


def test_power_gives_the_columns_the_checkbook_reads() -> None:
    frame = weather.parse_power(POWER_JSON, 19.5)
    assert list(frame.columns) == ["date", "eto_mm", "rain_mm", "tmax_c", "tmin_c"]
    assert list(frame["date"]) == [date(2026, 9, 10), date(2026, 9, 11)]   # -999 day left out
    assert frame["rain_mm"].tolist() == [6.3, 2.18]
    assert 2.0 < frame["eto_mm"].mean() < 6.0          # a humid highland September day


def test_power_is_asked_for_the_right_point_and_days() -> None:
    asked = {}

    def get(url, params=None, timeout=None):
        asked.update(url=url, params=params)
        return Answer(POWER_JSON)

    weather.fetch_power(19.5, -96.9, date(2026, 9, 10), date(2026, 9, 12), get=get)
    assert asked["url"] == weather.POWER_URL
    assert asked["params"]["start"] == "20260910" and asked["params"]["end"] == "20260912"
    assert "T2MDEW" in asked["params"]["parameters"]


def test_a_window_power_has_not_reached_yet_says_so() -> None:
    empty = json.dumps({"geometry": {"type": "Point", "coordinates": [-96.9, 19.5, 1133.0]},
                        "properties": {"parameter": {
                            name: {"20260919": -999.0} for name in weather.POWER_PARAMETERS}}})
    with pytest.raises(weather.WeatherError, match="no published days"):
        weather.parse_power(empty, 19.5)


def test_rubbish_from_power_is_a_weather_error() -> None:
    with pytest.raises(weather.WeatherError, match="unexpected"):
        weather.parse_power("<html>maintenance</html>", 19.5)


def test_the_reference_evaporation_follows_fao56() -> None:
    """FAO-56 Example 18: Brussels, 6 July, ETo = 3.9 mm a day."""
    eto = weather.eto_penman_monteith(tmax_c=[21.5], tmin_c=[12.3], dew_c=[10.9],
                                      wind_2m_ms=[2.078], solar_mj=[22.07], lat_deg=50.8,
                                      elevation_m=100.0, doy=[187])
    assert float(eto[0]) == pytest.approx(3.9, abs=0.2)


def test_a_hot_dry_day_asks_for_more_water_than_a_cool_wet_one() -> None:
    hot = weather.eto_penman_monteith([40], [25], [10], [3], [28], 26.1, 20.0, [200])
    mild = weather.eto_penman_monteith([25], [18], [17], [1], [14], 26.1, 20.0, [200])
    assert float(hot[0]) > 2 * float(mild[0])


# --------------------------------------------------------------------------- #
# SoilGrids
# --------------------------------------------------------------------------- #


def test_soilgrids_makes_a_profile_the_checkbook_can_use() -> None:
    profile = soils.profile_from_soilgrids(SOILGRIDS_JSON)
    assert profile.source == "soilgrids" and profile.name == "clay loam soil"
    assert profile.clay_pct == pytest.approx(27.6) and profile.sand_pct == pytest.approx(46.2)
    assert 0.8 < profile.awc_in_per_ft < 2.5          # a believable clay loam
    assert profile.storage_cm[0] < profile.storage_cm[-1]      # deeper holds more
    assert profile.taw_mm(1.0) > profile.taw_mm(0.5)
    assert profile.intake == "moderate" and profile.map_units[0]["layer"] == "0-5cm"


@pytest.mark.parametrize("sand, clay, organic, expected", [
    # Saxton & Rawls (2006), table 1: sand holds little, clay loam holds much more.
    (88, 5, 0.5, 0.08), (40, 20, 1.5, 0.16), (30, 35, 2.5, 0.16),
])
def test_available_water_from_what_the_soil_is_made_of(sand, clay, organic, expected) -> None:
    assert soils.available_water_fraction(sand, clay, organic) == pytest.approx(expected,
                                                                                abs=0.05)


def test_a_point_with_no_soil_says_what_to_do() -> None:
    with pytest.raises(soils.SoilError, match="water, rock or city"):
        soils.profile_from_soilgrids({"properties": {"layers": [
            {"name": "clay", "depths": [{"label": "0-5cm", "values": {"mean": None}}]}]}})


def test_soilgrids_is_asked_at_the_middle_of_the_field() -> None:
    asked = {}

    def get(url, params=None, timeout=None):
        asked.update(url=url, params=dict(params))
        return Answer(payload=SOILGRIDS_JSON)

    field = Polygon([(-96.91, 19.49), (-96.89, 19.49), (-96.89, 19.51), (-96.91, 19.51)])
    profile = soils.fetch_soilgrids(field, get=get)
    assert asked["url"] == soils.SOILGRIDS_URL
    assert asked["params"]["lat"] == "19.50000" and asked["params"]["lon"] == "-96.90000"
    assert profile.source == "soilgrids"


def test_soilgrids_being_busy_is_waited_out_then_explained(monkeypatch) -> None:
    monkeypatch.setattr(soils, "RETRY_WAIT_S", 0)
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(1)
        return Answer(payload={}, status=429)

    with pytest.raises(soils.SoilError, match="slow down"):
        soils.fetch_soilgrids(Point(-96.9, 19.5), get=get, attempts=2)
    assert len(calls) == 2


def test_a_profile_survives_the_cache(tmp_path) -> None:
    profile = soils.profile_from_soilgrids(SOILGRIDS_JSON)
    back = soils.SoilProfile.from_dict(json.loads(json.dumps(profile.to_dict())))
    assert back.storage_cm == profile.storage_cm and back.name == profile.name
    assert np.isclose(back.taw_mm(1.2), profile.taw_mm(1.2))


def test_a_field_on_the_world_grids_says_so_and_claims_less() -> None:
    """The answer is real, but a 50 km grid and a soil worked out from its make-up
    are a step further from measurement than gridMET and the USDA survey."""
    from tests.test_water import DAY0, SOIL, _checkbook, _event, _weather

    weather_frame = _weather(90)
    weather_frame["source"] = "power"
    status, _, _ = _checkbook(weather=weather_frame,
                              soil_note={"name": "clay loam soil", "source": "soilgrids"},
                              events=[_event(DAY0, "planted")])
    assert status.confidence != "high"
    assert any("NASA POWER" in note and "SoilGrids" in note for note in status.notes)


def test_a_us_field_says_nothing_of_the_world_grids() -> None:
    from tests.test_water import DAY0, _checkbook, _event

    status, _, _ = _checkbook(events=[_event(DAY0, "planted")])
    assert not any("NASA POWER" in note for note in status.notes)
