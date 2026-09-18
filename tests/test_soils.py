"""Tests for combining soil survey map units into one field's water profile."""

from __future__ import annotations

import pytest
from shapely.geometry import box

from dosojos_sat import soils

AREAS = {"1": 3.0, "2": 1.0, "3": 0.5}
ATTRIBUTES = [
    {"mukey": "1", "muname": "Hidalgo sandy clay loam", "aws025wta": 5, "aws050wta": 10,
     "aws0100wta": 20, "aws0150wta": 30, "hydgrpdcd": "B", "drclassdcd": "Well drained"},
    {"mukey": "2", "muname": "Mercedes clay", "aws025wta": 4, "aws050wta": 8,
     "aws0100wta": 16, "aws0150wta": 24, "hydgrpdcd": "D", "drclassdcd": "Moderately well"},
    {"mukey": "3", "muname": "Water", "aws025wta": None, "aws050wta": None,
     "aws0100wta": None, "aws0150wta": None, "hydgrpdcd": None, "drclassdcd": None},
]
SURFACE = [
    {"mukey": "1", "comppct_r": 80, "claytotal_r": 30, "sandtotal_r": 30, "ksat_r": 5},
    {"mukey": "2", "comppct_r": 90, "claytotal_r": 50, "sandtotal_r": 10, "ksat_r": 1},
]


def test_map_units_count_by_the_share_of_field_they_cover() -> None:
    """Open water drops out; the rest is weighted by area, named after the largest."""
    profile = soils.combine_map_units(AREAS, ATTRIBUTES, SURFACE)
    assert profile.storage_cm[2] == pytest.approx((3 * 20 + 1 * 16) / 4)
    assert profile.name == "Hidalgo sandy clay loam"
    assert profile.hydgrp == "B"
    w1, w2 = 3 / 4.5 * 80, 1 / 4.5 * 90
    assert profile.clay_pct == pytest.approx((30 * w1 + 50 * w2) / (w1 + w2), abs=0.01)
    assert [u["mukey"] for u in profile.map_units] == ["1", "2", "3"]


def test_a_field_with_no_water_storage_says_how_to_proceed() -> None:
    with pytest.raises(soils.SoilError, match="soil_awc_in_ft"):
        soils.combine_map_units({"3": 1.0}, [ATTRIBUTES[2]], [])


def test_root_zone_water_follows_the_survey_curve() -> None:
    profile = soils.combine_map_units({"1": 1.0}, [ATTRIBUTES[0]], SURFACE[:1])
    assert profile.taw_mm(0.75) == pytest.approx(150.0)        # halfway from 10 to 20 cm
    assert profile.taw_mm(2.0) == pytest.approx(400.0)         # 150 cm on at the last rate


def test_a_measured_capacity_gives_the_same_number_back() -> None:
    profile = soils.manual_profile(1.8)
    assert profile.awc_in_per_ft == pytest.approx(1.8)
    assert profile.taw_mm(0.3048) == pytest.approx(1.8 * 25.4)     # one foot


@pytest.mark.parametrize("hydgrp, clay, sand, intake", [
    ("D", None, None, "slow"), ("C/D", None, None, "slow"), ("A", None, None, "fast"),
    ("B", None, None, "moderate"), (None, 45, 10, "slow"), (None, 5, 80, "fast"),
])
def test_intake_speed_comes_from_the_group_or_the_texture(hydgrp, clay, sand, intake) -> None:
    profile = soils.SoilProfile(storage_cm=(1, 2, 3, 4), name="x", source="ssurgo",
                                hydgrp=hydgrp, clay_pct=clay, sand_pct=sand)
    assert profile.intake == intake


def test_a_profile_survives_the_cache() -> None:
    profile = soils.combine_map_units(AREAS, ATTRIBUTES, SURFACE)
    assert soils.SoilProfile.from_dict(profile.to_dict()) == profile


class _Response:
    def __init__(self, table: list[list]) -> None:
        self.status_code, self._table = 200, table
        self.text = "table" if table else ""

    def json(self) -> dict:
        return {"Table": self._table}


def _table(rows: list[dict]) -> list[list]:
    header = list(rows[0])
    return [header] + [[row[h] for h in header] for row in rows]


def test_the_survey_is_asked_three_questions_about_the_outline() -> None:
    questions = []

    def post(url, json=None, timeout=None):
        sql = json["query"]
        questions.append(sql)
        if "mupolygon" in sql:
            return _Response([["mukey", "area"], ["1", "3.0"], ["2", "1.0"]])
        if "muaggatt" in sql:
            return _Response(_table(ATTRIBUTES[:2]))
        return _Response(_table(SURFACE))

    profile = soils.fetch_ssurgo(box(-97.91, 26.21, -97.90, 26.22), post=post)
    assert len(questions) == 3 and "POLYGON" in questions[0]
    assert profile.source == "ssurgo" and profile.name == "Hidalgo sandy clay loam"


def test_outside_the_survey_says_how_to_proceed() -> None:
    with pytest.raises(soils.SoilError, match="soil_awc_in_ft"):
        soils.fetch_ssurgo(box(-99.2, 19.4, -99.1, 19.5), post=lambda *a, **k: _Response([]))


class _Page:
    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text

    def json(self) -> dict:
        raise ValueError("Expecting value")


def test_the_nightly_maintenance_is_said_plainly() -> None:
    page = _Page("<html><body><h1>Site is under daily maintenance from 12:30 AM CST to "
                 "12:45 AM CST.</h1></body></html>")
    with pytest.raises(soils.SoilError, match="daily maintenance"):
        soils.fetch_ssurgo(box(-97.91, 26.21, -97.90, 26.22), post=lambda *a, **k: page)


def test_a_stray_page_is_tried_again() -> None:
    answers = [_Page("<html>busy</html>"), _Response([["mukey", "area"], ["1", "3.0"]])]
    waits = []
    rows = soils._query(lambda *a, **k: answers.pop(0), "SELECT 1", sleep=waits.append)
    assert rows == [{"mukey": "1", "area": "3.0"}] and waits == [soils.RETRY_WAIT_S]


def test_a_page_that_never_turns_into_data_fails_in_the_end() -> None:
    with pytest.raises(soils.SoilError, match="not data, 3 times"):
        soils._query(lambda *a, **k: _Page("<html>busy</html>"), "SELECT 1",
                     sleep=lambda s: None)
