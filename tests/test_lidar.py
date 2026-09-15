"""Ground from public airborne LiDAR, read without the network, and judged like a drone's."""

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

from dosojos_drone import lidar, terrain
from dosojos_drone.cli import cli

EPSG = 32614
TRANSFORM = from_origin(590_000.0, 2_898_000.0, 2.0, 2.0)
SIZE = 400                       # 800 m of 2 m cells
FIELD = box(590_200.0, 2_897_400.0, 590_600.0, 2_897_700.0)    # 400 x 300 m, in UTM


def tile(folder: Path, name: str, *, canopy_share: float = 0.0) -> Path:
    """A 2 m lidar tile: a gently sloping field; the surface adds 2 m trees on a share."""
    rows, cols = np.mgrid[0:SIZE, 0:SIZE]
    ground = 10.0 + rows * 0.002 * 2.0                      # 0.2% fall toward the south
    if name == "dsm":
        ground = ground + np.where(cols < canopy_share * SIZE, 2.0, 0.0)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.tif"
    with rasterio.open(path, "w", driver="GTiff", height=SIZE, width=SIZE, count=1,
                       dtype="float32", crs=f"EPSG:{EPSG}", transform=TRANSFORM) as out:
        out.write(ground.astype("float32"), 1)
    return path


def outline() -> object:
    return gpd.GeoSeries([FIELD], crs=EPSG).to_crs(4326).iloc[0]


def fake_service(folder: Path, *, items: bool = True):
    """The Planetary Computer's search and token calls, answered from local tiles."""
    calls = []

    def fetch(url: str, body: bytes | None) -> dict:
        calls.append(url)
        if "token" in url:
            return {"token": "sv=test&sig=x"}
        collection = json.loads(body)["collections"][0]
        kind = "dtm" if collection.endswith("dtm") else "dsm"
        if not items:
            return {"features": []}
        return {"features": [{
            "id": f"USGS_LPC_TX_South_B8_2018_LAS_2019-{kind}-2m-5-7",
            "properties": {"start_datetime": "2019-01-01T00:00:00Z"},
            "assets": {"data": {"href": str(folder / f"{kind}.tif")}},
        }]}

    return fetch, calls


def open_local(href: str):
    assert href.endswith("?sv=test&sig=x")                 # the token was attached
    return rasterio.open(href.split("?")[0])


def test_the_field_window_is_read_and_written(tmp_path: Path) -> None:
    tiles = tmp_path / "tiles"
    tile(tiles, "dtm")
    tile(tiles, "dsm")
    fetch, calls = fake_service(tiles)

    ground = lidar.fetch_ground(outline(), tmp_path / "out", fetch=fetch, opener=open_local)

    assert (ground.project, ground.year, ground.resolution_m) == ("TX_South_B8_2018", 2019, 2.0)
    with rasterio.open(ground.dtm) as dtm:
        assert dtm.crs.to_epsg() == EPSG
        left, bottom, right, top = dtm.bounds
        # The field plus the margin, not the whole tile.
        assert left < FIELD.bounds[0] <= left + 2 * lidar.MARGIN_M + 4
        assert right - left < 400 + 2 * lidar.MARGIN_M + 8
    assert json.loads((tmp_path / "out" / "source.json").read_text())["project"] == "TX_South_B8_2018"
    assert sum("token" in c for c in calls) == 2


def test_no_survey_over_the_field_says_what_else_to_do(tmp_path: Path) -> None:
    fetch, _ = fake_service(tmp_path, items=False)
    with pytest.raises(lidar.LidarError, match="bare-soil drone flight"):
        lidar.fetch_ground(outline(), tmp_path / "out", fetch=fetch, opener=open_local)


def test_trees_hiding_the_ground_are_measured(tmp_path: Path) -> None:
    tiles = tmp_path / "tiles"
    tile(tiles, "dtm")
    tile(tiles, "dsm", canopy_share=0.6)
    fetch, _ = fake_service(tiles)
    ground = lidar.fetch_ground(outline(), tmp_path / "out", fetch=fetch, opener=open_local)
    # Trees stand west of x = 590 480, which is 300 m of the 440 m window read.
    assert lidar.covered_share(ground) == pytest.approx(300 / 440, abs=0.03)


def test_a_lidar_raster_is_named_lidar_not_imported(tmp_path: Path) -> None:
    """3DEP arrives as a GeoTIFF, but the ground in it was measured by laser."""
    (tmp_path / "imported.json").write_text(json.dumps({
        "lidar": True, "products": [{"name": "dtm", "how": "copied unchanged"}]}),
        encoding="utf-8")
    assert terrain.ground_source(tmp_path) == "lidar"


def test_the_lidar_command_imports_the_ground_for_the_terrain_step(tmp_path: Path,
                                                                    monkeypatch) -> None:
    tiles = tmp_path / "tiles"
    tile(tiles, "dtm")
    tile(tiles, "dsm")
    fetch, _ = fake_service(tiles)
    real = lidar.fetch_ground
    monkeypatch.setattr(lidar, "fetch_ground",
                        lambda shape, folder: real(shape, folder, fetch=fetch, opener=open_local))
    workspace = tmp_path / "drone"
    (tmp_path / "dosojos_sat").mkdir()
    (tmp_path / "dosojos_sat" / "fields.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "features": [{
            "type": "Feature", "geometry": outline().__geo_interface__,
            "properties": {"id": "PUBLIC-f1", "name": "F", "crop": "cotton"}}]}),
        encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(cli, ["--workspace", str(workspace), "lidar", "L1", "--field",
                                 "PUBLIC-f1"])
    assert result.exit_code == 0, result.output
    manifest = json.loads((workspace / "flights.json").read_text())["flights"]["L1"]
    assert manifest["field_id"] == "PUBLIC-f1" and "USGS 3DEP lidar" in manifest["source"]
    assert terrain.ground_source(workspace / "data" / "odm" / "L1") == "lidar"

    judged = runner.invoke(cli, ["--workspace", str(workspace), "terrain", "L1"])
    assert judged.exit_code == 0, judged.output
    assert "ground measured every 2 m" in judged.output and "lidar, 2 m grid" in judged.output
    assert "Falls" in judged.output or "slope" in judged.output
    report = json.loads((workspace / "out" / "L1" / "terrain.json").read_text())
    assert report["ground_source"] == "lidar"
    assert (workspace / "out" / "L1" / "terrain.png").stat().st_size > 20_000


def test_the_command_refuses_a_field_the_satellite_half_does_not_know(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["--workspace", str(tmp_path / "drone"), "lidar", "L1",
                                      "--field", "nope"])
    assert result.exit_code != 0 and "satellite half must know the field" in result.output
