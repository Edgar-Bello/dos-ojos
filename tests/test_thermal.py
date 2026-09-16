"""Tests for the optional thermal step: units, canopy-only masking, patches and the score."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from click.testing import CliRunner
from rasterio.transform import from_origin
from shapely.geometry import Point, box

from dosojos_drone import thermal
from dosojos_drone.cli import cli

EPSG = 32614
RES = 0.5
SIZE = 120                                  # a 60 m square at 0.5 m
X0, Y0 = 600000.0, 2900000.0
TRANSFORM = from_origin(X0, Y0 + SIZE * RES, RES, RES)
FRAME = box(X0, Y0, X0 + SIZE * RES, Y0 + SIZE * RES)


def _canopy(height_m: float = 0.9) -> np.ndarray:
    """A full canopy everywhere, the simplest case for the mask."""
    return np.full((SIZE, SIZE), height_m, dtype=np.float64)


def _field(*, hot_c: float = 0.0, centre=(15.0, 45.0), radius_m: float = 6.0,
           base_c: float = 28.0, noise_c: float = 0.25, seed: int = 3) -> np.ndarray:
    """An even canopy at ``base_c`` with one round patch ``hot_c`` degrees warmer."""
    rng = np.random.default_rng(seed)
    rows, cols = np.indices((SIZE, SIZE))
    x = (cols + 0.5) * RES
    y = (SIZE - rows - 0.5) * RES
    heat = np.full((SIZE, SIZE), base_c, dtype=np.float64)
    if hot_c:
        inside = (x - centre[0]) ** 2 + (y - centre[1]) ** 2 <= radius_m ** 2
        heat[inside] += hot_c
    return heat + rng.normal(0, noise_c, heat.shape)


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("values, unit, expected_c", [
    (np.array([28.0, 30.0, 32.0]), "celsius", 30.0),
    (np.array([301.15, 303.15, 305.15]), "kelvin", 30.0),
    (np.array([30115.0, 30315.0, 30515.0]), "centi-kelvin", 30.0),
])
def test_every_way_a_camera_writes_temperature_becomes_celsius(values, unit, expected_c):
    celsius, found = thermal.to_celsius(values)
    assert found == unit
    assert float(np.median(celsius)) == pytest.approx(expected_c, abs=0.01)


def test_a_picture_that_only_looks_like_heat_is_refused():
    """An 8-bit greyscale JPEG has pixel values, not temperatures."""
    grey = np.linspace(0, 255, 400).reshape(20, 20)
    with pytest.raises(thermal.ThermalError, match="radiometric"):
        thermal.to_celsius(grey)


def test_an_empty_raster_is_refused():
    with pytest.raises(thermal.ThermalError, match="no values"):
        thermal.to_celsius(np.full((4, 4), np.nan))


# --------------------------------------------------------------------------- #
# Patches
# --------------------------------------------------------------------------- #


def _find(heat, canopy, **kwargs):
    return thermal.find_patches(heat, canopy, TRANSFORM, cell_m=RES, frame=FRAME, **kwargs)


def test_an_even_canopy_reports_no_patch():
    patches, stats = _find(_field(), _canopy())
    assert patches == []
    assert stats["canopy_median_c"] == pytest.approx(28.0, abs=0.2)
    assert stats["warm_share"] < 0.02


def test_a_hot_patch_is_found_where_it_was_put():
    patches, _ = _find(_field(hot_c=3.0), _canopy())
    assert len(patches) == 1
    found = patches[0]
    assert found.above_c == pytest.approx(3.0, abs=0.4)
    assert found.where == "north-west corner"
    assert found.area_m2 == pytest.approx(np.pi * 36, rel=0.15)


def test_soil_between_the_rows_is_not_measured():
    """Bare ground bakes 20 C above the crop; on temperature alone it would be every patch."""
    heat = _field()
    canopy = _canopy()
    canopy[:, ::4] = 0.0                  # the furrows
    heat[:, ::4] += 20.0
    patches, stats = _find(heat, canopy)
    assert patches == []
    assert stats["canopy_median_c"] == pytest.approx(28.0, abs=0.2)


def test_a_bare_field_says_so_rather_than_guessing():
    with pytest.raises(thermal.ThermalError, match="nothing transpiring"):
        _find(_field(hot_c=3.0), _canopy(height_m=0.05))


def test_a_patch_under_the_size_floor_is_left_out():
    patches, _ = _find(_field(hot_c=3.0, radius_m=1.5), _canopy())
    assert patches == []


def test_warm_trees_across_an_orchard_s_gaps_count_as_one_patch():
    """An orchard's canopy is disconnected, so every warm tree would stand alone."""
    rows, cols = np.indices((SIZE, SIZE))
    x = (cols + 0.5) * RES
    y = (SIZE - rows - 0.5) * RES
    # Round crowns 2 m across on a 5 m grid, like a grove seen from above.
    to_trunk = np.hypot((x % 5.0) - 2.5, (y % 5.0) - 2.5)
    canopy = np.where(to_trunk <= 2.0, 1.5, 0.0)
    heat = _field()
    heat[(x - 15) ** 2 + (y - 45) ** 2 <= 10 ** 2] += 3.5

    patches, _ = _find(heat, canopy)
    assert len(patches) == 1
    found = patches[0]
    assert found.area_m2 > 200                    # the ground it spans
    assert found.canopy_m2 < found.area_m2        # only the leaves in it are warm
    assert found.above_c == pytest.approx(3.5, abs=0.4)


def test_a_streak_along_the_rows_is_not_mistaken_for_a_blob():
    rows, cols = np.indices((SIZE, SIZE))
    heat = _field()
    heat[(rows > 40) & (rows < 44)] += 3.0        # a 2 m band the length of the field
    patches, _ = _find(heat, _canopy())
    assert len(patches) == 1
    assert patches[0].compact < 0.15


# --------------------------------------------------------------------------- #
# The scorecard
# --------------------------------------------------------------------------- #


def _patch(**kwargs) -> thermal.Patch:
    values = {"label": 1, "area_m2": 300.0, "canopy_m2": 300.0, "mean_c": 31.0,
              "peak_c": 33.0, "above_c": 3.0, "z": 3.0, "where": "north-west corner",
              "compact": 0.8, "geometry": Point(X0 + 10, Y0 + 10).buffer(9.8)}
    values.update(kwargs)
    return thermal.Patch(**values)


def test_the_score_never_reaches_certainty_either_way():
    """No arrangement of evidence is allowed to sound like a diagnosis."""
    best = thermal.score(_patch(problem_share=0.9),
                         thermal.Evidence(water_days=10, field_problem_share=0.05, spots=(1,)))
    worst = thermal.score(_patch(above_c=1.1, area_m2=30.0, compact=0.05, ground="high",
                                 problem_share=0.0),
                          thermal.Evidence(water_days=0, field_problem_share=0.5))
    assert best.chance <= thermal.CHANCE_CEILING
    assert worst.chance >= thermal.CHANCE_FLOOR
    assert worst.chance < best.chance


def test_a_high_spot_under_the_patch_argues_against_a_pest():
    """Water not reaching a rise explains heat without anything living."""
    plain = thermal.score(_patch(), thermal.Evidence(spots=(1,)))
    on_a_rise = thermal.score(_patch(ground="high"), thermal.Evidence(spots=(1,)))
    assert on_a_rise.chance < plain.chance
    assert any("high spot" in s for s in on_a_rise.signs)


def test_a_thirsty_field_argues_against_a_pest():
    watered = thermal.score(_patch(), thermal.Evidence(water_days=9))
    dry = thermal.score(_patch(), thermal.Evidence(water_days=0))
    assert dry.chance < watered.chance


def test_damage_the_colour_camera_also_sees_argues_for_one():
    quiet = thermal.score(_patch(problem_share=0.05),
                          thermal.Evidence(field_problem_share=0.10))
    damaged = thermal.score(_patch(problem_share=0.60),
                            thermal.Evidence(field_problem_share=0.10))
    assert damaged.chance > quiet.chance
    assert any("60%" in s for s in damaged.signs)


def test_every_score_carries_the_signs_it_was_built_from():
    scored = thermal.score(_patch(), thermal.Evidence(water_days=9, spots=(1,)))
    assert scored.signs
    assert all(isinstance(s, str) and s for s in scored.signs)


def test_a_field_running_hot_all_over_is_called_out_as_such():
    report = thermal.build_report(
        "F1-20250520", "F001", unit="celsius", cell_m=RES, patches=[],
        stats={"canopy_m2": 1000.0, "canopy_median_c": 30.0, "canopy_spread_c": 0.5,
               "warm_share": 0.6},
        evidence=thermal.Evidence(water_days=5),
    )
    assert any("too much of the field" in n for n in report.notes)


def test_without_a_checkbook_the_report_says_it_could_not_tell_thirst_apart():
    report = thermal.build_report(
        "F1-20250520", "F001", unit="celsius", cell_m=RES, patches=[],
        stats={"canopy_m2": 1000.0, "canopy_median_c": 30.0, "canopy_spread_c": 0.5,
               "warm_share": 0.01},
    )
    assert any("water-days" in n for n in report.notes)


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


def _write(path: Path, data: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", height=SIZE, width=SIZE, count=1,
                       dtype="float32", crs=rasterio.crs.CRS.from_epsg(EPSG),
                       transform=TRANSFORM, nodata=-9999.0) as dataset:
        dataset.write(data.astype(np.float32), 1)
    return path


def test_the_command_writes_a_map_a_json_and_the_patches(tmp_path: Path):
    workspace = tmp_path / "workspace"
    out_dir = workspace / "out" / "F001-20250520"
    _write(out_dir / "chm.tif", _canopy())
    heat_path = _write(tmp_path / "thermal.tif", _field(hot_c=3.5))

    result = CliRunner().invoke(cli, ["--workspace", str(workspace), "thermal",
                                      "F001-20250520", "--thermal", str(heat_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads((out_dir / "thermal.json").read_text(encoding="utf-8"))
    assert len(payload["patches"]) == 1
    assert 0 < payload["patches"][0]["chance"] < 1
    assert payload["patches"][0]["signs"]
    assert (out_dir / "thermal.png").exists()
    assert (out_dir / "thermal_patches.geojson").exists()
    assert "the camera cannot name what it is" in result.output


def test_the_command_refuses_a_flight_with_no_canopy_model(tmp_path: Path):
    workspace = tmp_path / "workspace"
    heat_path = _write(tmp_path / "thermal.tif", _field(hot_c=3.5))
    result = CliRunner().invoke(cli, ["--workspace", str(workspace), "thermal",
                                      "F001-20250520", "--thermal", str(heat_path)])
    assert result.exit_code != 0
    assert "chm" in result.output
