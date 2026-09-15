"""Tests for the ground model, spots, the stress-to-ground links, and the advice."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from click.testing import CliRunner
from rasterio.transform import from_origin
from shapely.geometry import box

from dosojos_drone import report as report_mod
from dosojos_drone import terrain
from dosojos_drone.cli import _public_banner, cli
from dosojos_drone.config import Flight

EPSG = 32614
RES = 0.5
SIZE = 120                                  # a 60 m square at 0.5 m
X0, Y0 = 600000.0, 2900000.0                # south-west corner
TRANSFORM = from_origin(X0, Y0 + SIZE * RES, RES, RES)
EXTENT = box(X0, Y0, X0 + SIZE * RES, Y0 + SIZE * RES)


def _surface(*, fall_north_to_south: float = 0.002, bump_cm: float = 0.0,
             pit_cm: float = 0.0, dome_cm: float = 0.0, noise_cm: float = 0.3,
             seed: int = 1) -> np.ndarray:
    """Ground rising to the north by ``fall``, with an optional bump, pit and dome."""
    rng = np.random.default_rng(seed)
    rows, cols = np.indices((SIZE, SIZE))
    x = (cols + 0.5) * RES
    y = (SIZE - rows - 0.5) * RES                    # metres north of the south edge
    z = 10.0 + fall_north_to_south * y
    z += bump_cm / 100 * np.exp(-((x - 18) ** 2 + (y - 40) ** 2) / (2 * 4.0 ** 2))
    z -= pit_cm / 100 * np.exp(-((x - 45) ** 2 + (y - 15) ** 2) / (2 * 4.0 ** 2))
    z += dome_cm / 100 * (1 - ((x - 30) ** 2 + (y - 30) ** 2) / 900)
    return z + rng.normal(0, noise_cm / 100, z.shape)


def _ground(**kwargs) -> terrain.Ground:
    return terrain.fit_ground(_surface(**kwargs), TRANSFORM, rasterio.crs.CRS.from_epsg(EPSG), RES)


def _rows(problem_at=None, seed: int = 3) -> gpd.GeoDataFrame:
    """Row pieces 1 m long in north-south rows 0.76 m apart, flagged by ``problem_at``."""
    rng = np.random.default_rng(seed)
    records, shapes = [], []
    for r, x in enumerate(np.arange(X0 + 2, X0 + 58, 0.76)):
        for s, y in enumerate(np.arange(Y0 + 1, Y0 + 59, 1.0)):
            problem = problem_at(x - X0, y - Y0, rng) if problem_at else rng.random() < 0.12
            records.append({"row": r, "segment": s,
                            "flag": "STRESSED" if problem else "HEALTHY"})
            shapes.append(box(x - 0.3, y, x + 0.3, y + 1.0))
    return gpd.GeoDataFrame(records, geometry=shapes, crs=EPSG)


def _analyse(ground, flags, **kwargs):
    args = dict(flight_id="t", field_id="f", extent=EXTENT, flags=flags, method="furrow",
                water_enters=None, ground_source="lidar")
    args.update(kwargs)
    return terrain.analyse(ground, **args)


# --------------------------------------------------------------------------- #
# Ground model
# --------------------------------------------------------------------------- #


def test_the_plane_recovers_the_field_s_grade() -> None:
    ground = _ground(fall_north_to_south=0.002)
    assert ground.plane[2] == pytest.approx(0.002, abs=1e-5)       # rises 0.2% northward
    assert abs(ground.plane[1]) < 1e-5
    assert np.nanstd(ground.relief_cm) < 0.5


def test_a_bump_is_a_high_spot_and_a_pit_a_low_one() -> None:
    ground = _ground(bump_cm=10, pit_cm=10)
    spots = terrain.find_spots(ground, EXTENT)
    kinds = {s.kind: s for s in spots}
    assert set(kinds) == {"high", "low"}
    assert kinds["high"].peak_cm == pytest.approx(10, abs=2.5)
    assert kinds["low"].peak_cm == pytest.approx(-10, abs=2.5)
    assert kinds["high"].where == "west side"
    assert [s.label for s in spots] == ["H1", "L1"]


def test_a_dome_is_measured_as_curvature() -> None:
    depth, kind = terrain.curvature(_ground(dome_cm=8))
    assert depth >= terrain.DOME_RELIEF_CM and kind == "dome"


def test_a_dtm_without_a_projected_crs_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "dtm.tif"
    with rasterio.open(path, "w", driver="GTiff", height=10, width=10, count=1,
                       dtype="float32", crs="EPSG:4326",
                       transform=from_origin(-97.9, 26.2, 1e-5, 1e-5)) as dataset:
        dataset.write(np.ones((1, 10, 10), dtype="float32"))
    with pytest.raises(terrain.TerrainError, match="projected"):
        terrain.load_ground(path)


def test_the_ground_is_read_inside_the_field_only(tmp_path: Path) -> None:
    path = tmp_path / "dtm.tif"
    with rasterio.open(path, "w", driver="GTiff", height=SIZE * 5, width=SIZE * 5, count=1,
                       dtype="float32", crs=f"EPSG:{EPSG}", nodata=-9999,
                       transform=from_origin(X0, Y0 + SIZE * RES, RES / 5, RES / 5)) as dataset:
        dataset.write(np.kron(_surface(), np.ones((5, 5))).astype("float32")[None])
    half = box(X0, Y0, X0 + 30, Y0 + 60)
    ground = terrain.load_ground(path, half)
    assert ground.cell_m == pytest.approx(RES)
    assert np.isfinite(ground.z).sum() == pytest.approx(SIZE * SIZE / 2, rel=0.05)


# --------------------------------------------------------------------------- #
# Direction of the water
# --------------------------------------------------------------------------- #

NORTH_SOUTH = np.array([0.0, 1.0])
RISES_NORTH = np.array([0.0, 0.002])


def test_water_runs_away_from_the_side_it_enters() -> None:
    flow, source, _ = terrain.flow_direction(NORTH_SOUTH, RISES_NORTH * 0, "N")
    assert flow == pytest.approx([0.0, -1.0]) and source == "given"


def test_without_an_entry_side_water_runs_downhill_along_the_rows() -> None:
    flow, source, _ = terrain.flow_direction(-NORTH_SOUTH, RISES_NORTH, None)
    assert flow == pytest.approx([0.0, -1.0]) and source == "slope"


def test_an_entry_side_across_the_rows_is_questioned() -> None:
    flow, source, notes = terrain.flow_direction(np.array([1.0, 0.0]), np.array([0.002, 0]), "N")
    assert source == "slope" and notes and "rows run" in notes[0]


def test_a_level_field_with_no_entry_side_has_no_flow() -> None:
    assert terrain.flow_direction(NORTH_SOUTH, np.zeros(2), None)[0] is None


def test_rows_are_found_from_how_their_pieces_line_up() -> None:
    axis = terrain.row_axis(_rows())
    assert abs(axis @ NORTH_SOUTH) == pytest.approx(1.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# Links between the stress and the ground
# --------------------------------------------------------------------------- #


def test_a_real_bunching_is_linked_and_chance_is_not() -> None:
    rng = np.random.default_rng(0)
    inside = np.arange(2000) < 500
    bunched = np.where(inside, rng.random(2000) < 0.35, rng.random(2000) < 0.10)
    scattered = rng.random(2000) < 0.12
    assert terrain.link(bunched, inside, "tail").linked
    assert not terrain.link(scattered, inside, "tail").linked


def test_stress_at_the_row_tails_is_found_and_explained() -> None:
    """Water that does not reach the ends leaves the southern third stunted."""
    flags = _rows(lambda x, y, rng: rng.random() < (0.40 if y < 20 else 0.08))
    report = _analyse(_ground(), flags, water_enters="N", intake="slow")
    tail = next(l for l in report.links if l.place == terrain.TAIL)
    assert tail.linked and report.flow == "N to S"
    advice = next(a for a in report.advice if a.topic == "row ends")
    assert advice.priority == 1 and "surge" in advice.advice and "clay" in advice.advice


def test_stress_on_a_high_spot_is_found_and_explained() -> None:
    flags = _rows(lambda x, y, rng: rng.random() < (0.9 if (x - 18) ** 2 + (y - 40) ** 2 < 25
                                                     else 0.08))
    report = _analyse(_ground(bump_cm=10), flags, water_enters="N")
    spot = report.spots[0]
    assert spot.kind == "high" and spot.linked
    assert report.advice[0].topic == "high spot" and report.advice[0].priority == 1


def test_scattered_stress_points_away_from_the_water() -> None:
    report = _analyse(_ground(bump_cm=10), _rows(), water_enters="N")
    assert not any(l.linked for l in report.links)
    cause = next(a for a in report.advice if a.topic == "cause")
    assert "pests" in cause.advice


def test_an_uneven_field_is_told_what_leveling_would_move() -> None:
    report = _analyse(_ground(noise_cm=5.0), _rows(), water_enters="N")
    leveling = next(a for a in report.advice if a.topic == "leveling")
    assert leveling.priority == 1 and "cubic yards per acre" in leveling.advice
    assert report.cut_m3 > 0


def test_a_rainfed_field_hears_about_drainage_not_irrigation() -> None:
    report = _analyse(_ground(noise_cm=5.0, pit_cm=12), _rows(), method="none")
    text = " ".join(a.advice for a in report.advice)
    assert "drainage" in text and "longer set" not in text
    assert all(not (a.topic == "high spot" and a.priority == 3) for a in report.advice)


def test_a_photogrammetry_dome_is_cautioned() -> None:
    report = _analyse(_ground(dome_cm=10), _rows(), ground_source="photogrammetry")
    assert any("ground control" in note for note in report.notes)


def test_the_cropped_area_leaves_out_borders() -> None:
    area, how = terrain.cropped_area(EXTENT, _rows())
    assert how == "the rows' footprint" and area.area < EXTENT.area
    inner, how = terrain.cropped_area(EXTENT, None)
    assert "headland" in how and inner.area < EXTENT.area


@pytest.mark.parametrize("payload, expected", [
    (None, "photogrammetry"),
    ({"products": [{"name": "dtm", "how": "p5 of all points per 0.5 m cell"}]}, "lidar"),
    ({"products": [{"name": "dtm", "how": "copied unchanged"}]}, "imported"),
])
def test_where_the_ground_model_came_from(tmp_path: Path, payload, expected) -> None:
    if payload is not None:
        (tmp_path / "imported.json").write_text(json.dumps(payload), encoding="utf-8")
    assert terrain.ground_source(tmp_path) == expected


def test_outputs_are_written(tmp_path: Path) -> None:
    ground = _ground(bump_cm=10)
    flags = _rows()
    report = _analyse(ground, flags, water_enters="N")
    json.dumps(report.to_dict())                                   # JSON-ready
    assert terrain.write_relief(ground, tmp_path / "relief.tif").exists()
    assert terrain.write_spots(report, EPSG, tmp_path / "spots.geojson").exists()
    path = report_mod.save_terrain_map(ground, report, flags, EXTENT, tmp_path / "t.png",
                                       title="f - ground and water", banner="TEST")
    assert path.exists() and path.stat().st_size > 10_000


# --------------------------------------------------------------------------- #
# Command line and the join
# --------------------------------------------------------------------------- #


def test_synthetic_data_is_never_labelled_public() -> None:
    flight = Flight("s", "f", source="Synthetic: tools/make_synthetic_field.py")
    assert _public_banner(flight).startswith("SYNTHETIC TEST DATA")
    assert "FREE PUBLIC" in _public_banner(Flight("p", "f", source="Purdue"))


def test_the_terrain_command_reads_the_field_s_settings(tmp_path: Path) -> None:
    workspace = tmp_path / "drone"
    project = workspace / "data" / "odm" / "t1" / "odm_dem"
    project.mkdir(parents=True)
    with rasterio.open(project / "dtm.tif", "w", driver="GTiff", height=SIZE, width=SIZE,
                       count=1, dtype="float32", crs=f"EPSG:{EPSG}", transform=TRANSFORM,
                       nodata=-9999) as dataset:
        dataset.write(_surface(bump_cm=10).astype("float32")[None])
    out = workspace / "out" / "t1"
    out.mkdir(parents=True)
    _rows(lambda x, y, rng: rng.random() < (0.4 if y < 20 else 0.08)).to_file(
        out / "flags_rows.geojson", driver="GeoJSON")
    outline = gpd.GeoSeries([EXTENT], crs=EPSG).to_crs(4326).iloc[0]
    (tmp_path / "dosojos_sat").mkdir()
    (tmp_path / "dosojos_sat" / "fields.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "features": [{
            "type": "Feature", "geometry": outline.__geo_interface__,
            "properties": {"id": "f1", "name": "F", "crop": "sorghum",
                           "irrigation": "furrow", "water_enters": "north"}}]}),
        encoding="utf-8")
    runner = CliRunner()
    runner.invoke(cli, ["--workspace", str(workspace), "register", "t1", "--field", "f1"])

    result = runner.invoke(cli, ["--workspace", str(workspace), "terrain", "t1"])

    assert result.exit_code == 0, result.output
    saved = json.loads((out / "terrain.json").read_text(encoding="utf-8"))
    assert saved["method"] == "furrow" and saved["flow"] == "N to S"
    assert saved["flow_source"] == "given"
    assert (out / "terrain.png").exists() and (out / "ground_relief.tif").exists()
    assert "What to do" in result.output


def test_the_join_carries_water_and_irrigation_advice() -> None:
    satellite = {"season": 2026, "fields": [
        {"field_id": "a", "name": "A", "flagged": False, "score": 1},
        {"field_id": "b", "name": "B", "flagged": True, "score": 70}]}
    water = {"a": {"field_id": "a", "rank": 2, "status": "ok for now", "days_left": 12,
                   "as_of": "2026-09-12"},
             "b": {"field_id": "b", "rank": 1, "status": "water now", "days_left": 0,
                   "as_of": "2026-09-12"}}
    terrain_reports = {"b": {"flight_id": "x", "flow": "N to S", "links": [
        {"place": terrain.TAIL, "linked": True}], "spots": [],
        "advice": [{"topic": "row ends", "finding": "f", "advice": "surge", "priority": 1},
                   {"topic": "grade", "finding": "g", "advice": "fine", "priority": 3}]}}
    joined = report_mod.join_with_satellite(satellite, [], water=water,
                                            terrain=terrain_reports)
    b = next(f for f in joined["fields"] if f["field_id"] == "b")
    assert b["water"]["status"] == "water now"
    assert b["irrigation"]["linked"] == [terrain.TAIL]
    assert [a["topic"] for a in b["irrigation"]["advice"]] == ["row ends"]
    assert joined["water_as_of"] == "2026-09-12"


def test_a_ground_without_flags_gets_a_single_panel_map(tmp_path: Path) -> None:
    """Lidar or a bare-soil flight: no flags, so no empty bar panel beside the map."""
    ground = _ground(bump_cm=10)
    report = _analyse(ground, None)
    path = report_mod.save_terrain_map(ground, report, None, EXTENT, tmp_path / "t.png",
                                       title="f - ground and water", banner="TEST")
    from PIL import Image
    width, height = Image.open(path).size
    assert path.stat().st_size > 10_000 and width < 1900      # one panel, not two
