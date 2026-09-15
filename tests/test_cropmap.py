"""Tracing real fields of a crop out of USDA's crop map, without the network."""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from click.testing import CliRunner
from rasterio.transform import from_origin

from dosojos_sat import cropmap
from dosojos_sat.cli import cli
from dosojos_sat.fields import load_fields

# A 6 x 6 km patch of 30 m cells in UTM 14N, near Weslaco.
ORIGIN = (590_000.0, 2_898_000.0)
TRANSFORM = from_origin(*ORIGIN, 30, 30)
GRASS, COTTON, SORGHUM = 176, 2, 4
CLASSES = {"176": {"Class_Names": "Grassland/Pasture", "Red": 232, "Green": 255, "Blue": 191},
           "2": {"Class_Names": "Cotton", "Red": 255, "Green": 38, "Blue": 38},
           "4": {"Class_Names": "Sorghum", "Red": 255, "Green": 158, "Blue": 11}}


def crop_map(folder: Path, *, noise: float = 0.0, seed: int = 1) -> Path:
    """A 2025 map: one 40-acre-ish cotton block, a sliver of cotton, a sorghum block."""
    values = np.full((200, 200), GRASS, dtype="uint8")
    values[20:45, 20:40] = COTTON          # 750 x 600 m, 111 acres before shrinking
    values[100:102, 100:103] = COTTON      # too small to be a field
    values[120:150, 150:175] = SORGHUM     # 900 x 750 m
    if noise:
        rng = np.random.default_rng(seed)
        values[rng.random(values.shape) < noise] = GRASS
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "cdl_2025_test.tif"
    with rasterio.open(path, "w", driver="GTiff", height=200, width=200, count=1,
                       dtype="uint8", crs="EPSG:32614", transform=TRANSFORM) as out:
        out.write(values, 1)
    (folder / "cdl_2025_classes.json").write_text(json.dumps({"year": 2025, "classes": CLASSES}),
                                                  encoding="utf-8")
    return path


def test_a_block_of_one_crop_becomes_a_field_inside_its_edge(tmp_path: Path) -> None:
    found = cropmap.trace(crop_map(tmp_path), "cotton")
    assert len(found) == 1                              # the sliver is too small
    field = found[0]
    assert 60 < field.acres < 111                       # shrunk inside the block
    assert field.purity == 1.0 and field.squareness > 0.95
    assert -98.2 < field.geometry.centroid.x < -97.7 and 26.0 < field.geometry.centroid.y < 26.3


def test_two_fields_joined_at_a_waist_are_cut_apart(tmp_path: Path) -> None:
    values = np.full((200, 200), GRASS, dtype="uint8")
    values[20:45, 20:40] = COTTON          # north field, 750 m north to south
    values[48:73, 20:40] = COTTON          # south field, across a road the map mostly lost
    values[45:48, 28:34] = COTTON          # where the road's cells came out as cotton
    path = year_map(tmp_path, 2025, values)
    (field,) = cropmap.trace(path, "cotton")
    south, north = field.geometry.bounds[1], field.geometry.bounds[3]
    assert (north - south) * 110_570 < 700                  # one field, not both
    (joined,) = cropmap.trace(path, "cotton", neck_m=0.0)
    assert (joined.geometry.bounds[3] - joined.geometry.bounds[1]) * 110_570 > 1_300


def test_a_speckled_block_is_still_found_but_a_mixed_one_is_not(tmp_path: Path) -> None:
    speckled = cropmap.trace(crop_map(tmp_path / "a", noise=0.03), "cotton")
    assert speckled and speckled[0].purity > 0.9
    mixed = cropmap.trace(crop_map(tmp_path / "b", noise=0.3), "cotton")
    assert mixed == []


def test_an_unknown_crop_is_refused(tmp_path: Path) -> None:
    with pytest.raises(cropmap.CropMapError, match="choose from"):
        cropmap.trace(crop_map(tmp_path), "kale")


def test_picked_fields_are_kept_apart(tmp_path: Path) -> None:
    path = crop_map(tmp_path)
    cotton, sorghum = cropmap.trace(path, "cotton")[0], cropmap.trace(path, "sorghum")[0]
    assert len(cropmap.pick([cotton, sorghum], 2, apart_km=1.0)) == 2
    assert len(cropmap.pick([cotton, sorghum], 2, apart_km=50.0)) == 1


def test_features_name_the_crop_and_the_town(tmp_path: Path) -> None:
    path = crop_map(tmp_path)
    chosen = cropmap.trace(path, "sorghum") + cropmap.trace(path, "cotton")
    collection = cropmap.features(chosen, year=2025, prefix="PUBLIC-rgv")
    props = [f["properties"] for f in collection["features"]]
    assert [p["id"] for p in props] == ["PUBLIC-rgv-sorghum-1", "PUBLIC-rgv-cotton-1"]
    assert props[0]["crop"] == "grain sorghum" and props[0]["name"].startswith("Grain sorghum near ")
    assert "not a survey" in props[0]["source"]


def test_the_nearest_town() -> None:
    assert cropmap.nearest_town(-97.99, 26.16)[0] == "Weslaco"


class Answer:
    def __init__(self, payload=None, content: bytes = b""):
        self.payload, self.content = payload, content

    def json(self):
        return self.payload

    def raise_for_status(self) -> None:
        pass


def test_fetch_asks_the_service_once_and_keeps_the_map(tmp_path: Path) -> None:
    source = crop_map(tmp_path / "source")
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(url)
        if url.endswith("/exportImage"):
            assert json.loads(params["mosaicRule"]) == {"where": "Year = 2025"}
            return Answer({"href": "https://example.invalid/out.tif"})
        if url.endswith("/rasterAttributeTable"):
            return Answer({"features": [{"attributes": {"Value": int(k), **v}}
                                        for k, v in CLASSES.items()]})
        return Answer(content=source.read_bytes())

    folder = tmp_path / "cache"
    bbox = (-98.0, 26.15, -97.98, 26.17)
    path = cropmap.fetch(bbox, 2025, folder, get=get)
    assert path.exists() and cropmap.class_table(path)[COTTON]["Class_Names"] == "Cotton"
    assert len(calls) == 3
    assert cropmap.fetch(bbox, 2025, folder, get=get) == path and len(calls) == 3


def test_fetch_refuses_a_box_bigger_than_one_image(tmp_path: Path) -> None:
    with pytest.raises(cropmap.CropMapError, match="smaller box"):
        cropmap.fetch((-100.0, 25.0, -96.0, 29.0), 2025, tmp_path, get=lambda *a, **k: None)


def test_the_cropmap_command_writes_fields_the_pipeline_accepts(tmp_path: Path,
                                                                 monkeypatch) -> None:
    path = crop_map(tmp_path / "workspace" / "cache" / "cdl")
    monkeypatch.setattr(cropmap, "fetch", lambda bbox, year, folder: path)
    out = tmp_path / "fields.geojson"
    result = CliRunner().invoke(cli, ["--workspace", str(tmp_path / "workspace"), "cropmap",
                                      "--crops", "cotton,sorghum,citrus", "--out", str(out),
                                      "--banner", "FREE PUBLIC DATA"])
    assert result.exit_code == 0, result.output
    assert "no citrus field" in result.output
    fields = load_fields(out)
    assert sorted(f.crop for f in fields) == ["cotton", "grain sorghum"]
    assert (tmp_path / "workspace" / "out" / "cropmap_2025.png").stat().st_size > 20_000


FALLOW = 61


def year_map(folder: Path, year: int, values: np.ndarray) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"cdl_{year}_test.tif"
    with rasterio.open(path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1],
                       count=1, dtype="uint8", crs="EPSG:32614", transform=TRANSFORM) as out:
        out.write(values, 1)
    (folder / f"cdl_{year}_classes.json").write_text(json.dumps({"year": year, "classes": CLASSES}),
                                                     encoding="utf-8")
    return path


def history_maps(folder: Path) -> dict[int, Path]:
    """Earlier years of the same patch: in 2023 the cotton block's east half grew
    cotton and its west half sorghum; in 2024 the sorghum block rested, half
    mapped fallow and half grass."""
    maps = {}
    for year in (2023, 2024):
        values = np.full((200, 200), GRASS, dtype="uint8")
        values[20:45, 20:40] = SORGHUM
        values[120:150, 150:175] = COTTON
        if year == 2023:
            values[20:45, 30:40] = COTTON
        else:
            values[120:150, 150:162] = FALLOW
            values[120:150, 162:175] = GRASS
        maps[year] = year_map(folder, year, values)
    return maps


def test_a_block_split_in_an_earlier_year_is_two_fields(tmp_path: Path, monkeypatch) -> None:
    path = crop_map(tmp_path / "now")
    maps = history_maps(tmp_path / "before")
    monkeypatch.setattr(cropmap, "fetch", lambda bbox, year, folder: maps[year])
    cotton, sorghum = cropmap.trace(path, "cotton")[0], cropmap.trace(path, "sorghum")[0]
    shares = cropmap.history(cotton, [2023, 2024], tmp_path)
    assert shares[2023] == pytest.approx(0.5, abs=0.1) and shares[2024] == 1.0
    # resting land is one class, whatever the map calls it that year
    assert cropmap.history(sorghum, [2023, 2024], tmp_path) == {2023: 1.0, 2024: 1.0}


def test_the_cropmap_command_passes_over_two_fields_in_one_block(tmp_path: Path,
                                                                 monkeypatch) -> None:
    path = crop_map(tmp_path / "workspace" / "cache" / "cdl")
    maps = history_maps(tmp_path / "before")
    monkeypatch.setattr(cropmap, "fetch", lambda bbox, year, folder: maps.get(year, path))
    out = tmp_path / "fields.geojson"
    result = CliRunner().invoke(cli, ["--workspace", str(tmp_path / "workspace"), "cropmap",
                                      "--crops", "cotton,sorghum", "--history", "2023-2024",
                                      "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "Passed over 1 block(s)" in result.output and "in 2023" in result.output
    assert "no cotton field of 15-250 acres on the 2025 crop map in this box that was one " \
           "crop in each of 2023-2024 too" in " ".join(result.output.split())
    assert [f.crop for f in load_fields(out)] == ["grain sorghum"]
    source = json.loads(out.read_text(encoding="utf-8"))["features"][0]["properties"]["source"]
    assert "one crop across it in each of 2023-2024" in source


def test_a_history_span_must_come_before_the_year(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["--workspace", str(tmp_path), "cropmap", "--history",
                                      "2024-2025", "--out", str(tmp_path / "f.geojson")])
    assert result.exit_code != 0 and "must be like 2021-2024" in result.output
