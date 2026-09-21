"""Tests for gridMET parsing, the fetch's failure paths, and station files."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pytest

from dosojos_sat import weather

DAS = """Attributes {
    daily_mean_reference_evapotranspiration_grass {
        Int16 _FillValue 32767;
        String units "mm";
        Int16 missing_value 32767;
        Float64 scale_factor 0.1;
        Float64 add_offset 0.0;
    }
    precipitation_amount {
        Int16 _FillValue 32767;
        Int16 missing_value 32767;
        Float64 scale_factor 0.1;
        Float64 add_offset 0.0;
    }
}"""

HEADER = 'time,latitude[unit="degrees_north"],longitude[unit="degrees_east"],{var}[unit="mm"]\n'
ETO_CSV = (HEADER.format(var="daily_mean_reference_evapotranspiration_grass")
           + "2018-05-01T00:00:00Z,40.4784,-86.9897,69.0\n"
           + "2018-05-02T00:00:00Z,40.4784,-86.9897,32767.0\n")
RAIN_CSV = (HEADER.format(var="precipitation_amount")
            + "2018-05-01T00:00:00Z,40.4784,-86.9897,0.0\n"
            + "2018-05-02T00:00:00Z,40.4784,-86.9897,125.0\n")


class _Response:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text, self.status_code = text, status_code


def _fake_get(url: str, params=None, timeout=None) -> _Response:
    if url.endswith(".das"):
        return _Response(DAS)
    return _Response(ETO_CSV if "_pet_" in url else RAIN_CSV)


def test_the_packing_is_read_from_the_dataset() -> None:
    packing = weather.parse_das(DAS, "daily_mean_reference_evapotranspiration_grass")
    assert (packing.scale, packing.offset, packing.missing) == (0.1, 0.0, 32767.0)


def test_packed_values_are_unpacked_and_fill_becomes_missing() -> None:
    """The subset service returns 69 for 6.9 mm; a factor of ten is not guessed."""
    frame = weather.fetch_gridmet(40.4784, -86.9897, date(2018, 5, 1), date(2018, 5, 2),
                                  get=_fake_get)
    assert list(frame["date"]) == [date(2018, 5, 1), date(2018, 5, 2)]
    assert frame["eto_mm"].iloc[0] == pytest.approx(6.9)
    assert np.isnan(frame["eto_mm"].iloc[1])
    assert frame["rain_mm"].iloc[1] == pytest.approx(12.5)


def test_outside_the_contiguous_us_says_what_to_do_instead() -> None:
    with pytest.raises(weather.WeatherError, match="station"):
        weather.fetch_gridmet(19.43, -99.13, date(2026, 1, 1), date(2026, 1, 2), get=_fake_get)


def test_an_unreachable_server_is_retried_then_reported(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(weather.time, "sleep", lambda _: None)

    def refuse(url, params=None, timeout=None):
        calls.append(url)
        raise ConnectionError("no route")

    with pytest.raises(weather.WeatherError, match="after 3 tries"):
        weather.fetch_gridmet(26.2, -97.9, date(2026, 1, 1), date(2026, 1, 2), get=refuse)
    assert len(calls) == 3


def test_a_refused_request_is_not_retried() -> None:
    with pytest.raises(weather.WeatherError, match="refused"):
        weather.fetch_gridmet(26.2, -97.9, date(2026, 1, 1), date(2026, 1, 2),
                              get=lambda *a, **k: _Response("bad point", 400))


def test_a_station_file_in_inches_is_converted(tmp_path: Path) -> None:
    path = tmp_path / "mcallen.csv"
    path.write_text("Date,ETo (in),Rain (in),Pressure (mb)\n"
                    "2026-07-01,0.30,0.00,1010\n2026-07-02,0.25,0.50,1008\n", encoding="utf-8")
    frame = weather.read_station_csv(path)
    assert frame["eto_mm"].tolist() == pytest.approx([7.62, 6.35])
    assert frame["rain_mm"].tolist() == pytest.approx([0.0, 12.7])
    assert weather.station_source(path) == "station:mcallen.csv"


def test_a_station_file_must_name_its_units(tmp_path: Path) -> None:
    path = tmp_path / "farm.csv"
    path.write_text("date,ETo,rain_mm\n2026-07-01,7.1,0\n", encoding="utf-8")
    with pytest.raises(weather.WeatherError, match="does not say its unit"):
        weather.read_station_csv(path)


TEMP_DAS = """Attributes {
    daily_maximum_temperature {
        Int16 _FillValue 32767;
        Int16 missing_value 32767;
        Float64 scale_factor 0.1;
        Float64 add_offset 220.0;
    }
    daily_minimum_temperature {
        Int16 _FillValue 32767;
        Int16 missing_value 32767;
        Float64 scale_factor 0.1;
        Float64 add_offset 210.0;
    }
}"""


def test_highs_and_lows_arrive_in_celsius() -> None:
    """gridMET packs temperatures as kelvin; the checkbook's heat units want Celsius."""
    tmax = HEADER.format(var="daily_maximum_temperature") + "2018-05-01T00:00:00Z,40.4784,-86.9897,882.0\n"
    tmin = HEADER.format(var="daily_minimum_temperature") + "2018-05-01T00:00:00Z,40.4784,-86.9897,835.0\n"

    def get(url: str, params=None, timeout=None) -> _Response:
        if url.endswith(".das"):
            return _Response(TEMP_DAS if ("_tmmx_" in url or "_tmmn_" in url) else DAS)
        if "_tmmx_" in url:
            return _Response(tmax)
        if "_tmmn_" in url:
            return _Response(tmin)
        return _Response(ETO_CSV if "_pet_" in url else RAIN_CSV)

    frame = weather.fetch_gridmet(40.4784, -86.9897, date(2018, 5, 1), date(2018, 5, 1), get=get)
    assert frame.loc[0, "tmax_c"] == pytest.approx(308.2 - 273.15)     # 882 * 0.1 + 220
    assert frame.loc[0, "tmin_c"] == pytest.approx(293.5 - 273.15)


def test_the_water_checkbook_does_not_wait_on_temperatures() -> None:
    """If the temperature service fails, ETo and rain still arrive; stages simply wait."""
    def get(url: str, params=None, timeout=None) -> _Response:
        if "_tmmx_" in url or "_tmmn_" in url:
            return _Response("server exploded", 404)
        return _fake_get(url, params, timeout)

    frame = weather.fetch_gridmet(40.4784, -86.9897, date(2018, 5, 1), date(2018, 5, 2), get=get)
    assert frame["eto_mm"].notna().any()
    assert frame["tmax_c"].isna().all() and frame["tmin_c"].isna().all()
