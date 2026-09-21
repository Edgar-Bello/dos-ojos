"""Tests for importing finished maps made outside the pipeline, against known input."""

from __future__ import annotations

import json
from pathlib import Path

import laspy
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from dosojos_drone.external import (
    PROVENANCE_NAME,
    ProductImportError,
    cloud_info,
    import_products,
    import_raster,
    provenance,
    rasterise_ground,
    rasterise_surface,
    resolve_crs,
)
from dosojos_drone.odm_runner import EXPECTED_OUTPUTS, verify_outputs

CRS = "EPSG:26916"
X0, Y1 = 500000.0, 4480100.0


def write_tif(path: Path, array: np.ndarray, *, crs: str | None = CRS, res: float = 0.05,
              origin: tuple[float, float] = (X0, Y1)) -> Path:
    """A GeoTIFF, single- or multi-band, optionally without a CRS."""
    data = array if array.ndim == 3 else array[None]
    with rasterio.open(
        path, "w", driver="GTiff", width=data.shape[2], height=data.shape[1],
        count=data.shape[0], dtype=data.dtype, crs=crs,
        transform=from_origin(origin[0], origin[1], res, res),
    ) as dst:
        dst.write(data)
    return path


def write_las(path: Path, x, y, z, *, classification=None) -> Path:
    """A LAS 1.2 point cloud with no CRS record, like the Purdue scans."""
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.scales = np.array([0.001, 0.001, 0.001])
    header.offsets = np.array([float(np.min(x)), float(np.min(y)), float(np.min(z))])
    cloud = laspy.LasData(header)
    cloud.x, cloud.y, cloud.z = x, y, z
    if classification is not None:
        cloud.classification = classification
    cloud.write(str(path))
    return path


def grid_points(width_m: float, height_m: float, spacing: float):
    """Points on a regular grid covering a rectangle from (X0, Y1 - height)."""
    xs = np.arange(X0 + spacing / 2, X0 + width_m, spacing)
    ys = np.arange(Y1 - height_m + spacing / 2, Y1, spacing)
    gx, gy = np.meshgrid(xs, ys)
    return gx.ravel(), gy.ravel()


# --------------------------------------------------------------------------- #
# Coordinate systems
# --------------------------------------------------------------------------- #


def test_a_file_without_a_crs_needs_one_declared() -> None:
    """An unplaced map cannot be tied to a field, so the tool must refuse to guess."""
    with pytest.raises(ProductImportError, match="--crs"):
        resolve_crs(None, None, "scan.las")


def test_a_declared_crs_fills_the_gap() -> None:
    assert resolve_crs(None, CRS, "scan.las").to_epsg() == 26916


def test_a_declared_crs_that_contradicts_the_file_is_refused() -> None:
    """Silently preferring either would put the field in the wrong place."""
    with pytest.raises(ProductImportError, match="says it is in"):
        resolve_crs(rasterio.crs.CRS.from_epsg(32614), CRS, "ortho.tif")


# --------------------------------------------------------------------------- #
# Rasters
# --------------------------------------------------------------------------- #


def test_a_complete_georeferenced_raster_is_copied_unchanged(tmp_path: Path) -> None:
    source = write_tif(tmp_path / "in.tif", np.full((3, 40, 60), 120, dtype="uint8"))
    done = import_raster(source, tmp_path / "out.tif", name="orthophoto")
    assert done.how == "copied unchanged"
    assert (tmp_path / "out.tif").read_bytes() == source.read_bytes()


def test_a_raster_is_cropped_to_the_requested_bounds(tmp_path: Path) -> None:
    source = write_tif(tmp_path / "in.tif", np.random.default_rng(0).random((200, 200)).astype("float32"))
    bounds = (X0 + 2.0, Y1 - 6.0, X0 + 5.0, Y1 - 1.0)
    done = import_raster(source, tmp_path / "out.tif", name="dsm", bounds=bounds)
    with rasterio.open(tmp_path / "out.tif") as out:
        assert out.width == 60 and out.height == 100
        assert out.bounds.left == pytest.approx(bounds[0])
        assert out.bounds.top == pytest.approx(bounds[3])
    assert "cropped" in done.how


def test_a_raster_missing_its_crs_gets_the_declared_one(tmp_path: Path) -> None:
    source = write_tif(tmp_path / "in.tif", np.ones((20, 20), dtype="float32"), crs=None)
    import_raster(source, tmp_path / "out.tif", name="dtm", crs=CRS)
    with rasterio.open(tmp_path / "out.tif") as out:
        assert out.crs.to_epsg() == 26916


# --------------------------------------------------------------------------- #
# Point clouds
# --------------------------------------------------------------------------- #


def test_the_surface_is_the_highest_return_in_each_cell(tmp_path: Path) -> None:
    """A canopy return above a ground return in every cell: the DSM is the canopy."""
    x, y = grid_points(4.0, 3.0, 0.05)
    cloud = write_las(
        tmp_path / "dsm.las", np.concatenate([x, x]), np.concatenate([y, y]),
        np.concatenate([np.full(x.size, 180.0), np.full(x.size, 182.4)]),
    )
    done = rasterise_surface(cloud, tmp_path / "dsm.tif", crs=CRS, resolution_m=0.1)
    with rasterio.open(tmp_path / "dsm.tif") as out:
        surface = out.read(1, masked=True)
        assert out.crs.to_epsg() == 26916
    assert float(surface.min()) == pytest.approx(182.4, abs=0.002)
    assert "100% of cells hit" in done.how


def test_the_grid_defaults_to_the_point_spacing(tmp_path: Path) -> None:
    """Finer than the points leaves holes; coarser throws detail away."""
    x, y = grid_points(3.0, 3.0, 0.041)
    cloud = write_las(tmp_path / "dsm.las", x, y, np.full(x.size, 180.0))
    assert cloud_info(cloud)["spacing_m"] == pytest.approx(0.041, rel=0.05)
    done = rasterise_surface(cloud, tmp_path / "dsm.tif", crs=CRS)
    assert done.resolution_m == pytest.approx(0.05)


def test_ground_comes_from_the_soil_between_the_plants(tmp_path: Path) -> None:
    """An early flight: plants on 0.76 m rows over a gently sloping field."""
    x, y = grid_points(8.0, 6.0, 0.04)
    ground = 180.0 + 0.01 * (x - X0)
    plant = np.where(np.mod(x - X0, 0.76) < 0.25, 0.3, 0.0)
    cloud = write_las(tmp_path / "early.las", x, y, ground + plant)

    transform = from_origin(X0, Y1, 0.05, 0.05)
    rasterise_ground(cloud, tmp_path / "dtm.tif", reference_transform=transform,
                     reference_shape=(120, 160), crs=CRS)
    with rasterio.open(tmp_path / "dtm.tif") as out:
        dtm = out.read(1)
    cols = X0 + (np.arange(160) + 0.5) * 0.05
    expected = 180.0 + 0.01 * (cols - X0)
    interior = dtm[20:100, 20:140] - expected[None, 20:140]
    assert np.abs(interior).max() < 0.03


def test_ground_classified_points_are_preferred(tmp_path: Path) -> None:
    """When the cloud says which returns are ground, only those are used."""
    x, y = grid_points(4.0, 4.0, 0.05)
    classes = np.where(np.arange(x.size) % 2 == 0, 2, 1).astype(np.uint8)
    z = np.where(classes == 2, 180.0, 181.5)
    cloud = write_las(tmp_path / "classified.las", x, y, z, classification=classes)
    done = rasterise_ground(cloud, tmp_path / "dtm.tif",
                            reference_transform=from_origin(X0, Y1, 0.1, 0.1),
                            reference_shape=(40, 40), crs=CRS, percentile=50.0)
    with rasterio.open(tmp_path / "dtm.tif") as out:
        assert float(np.nanmax(out.read(1))) == pytest.approx(180.0, abs=0.01)
    assert "ground-classified" in done.how


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def _products(tmp_path: Path) -> dict[str, Path]:
    """An orthophoto over a larger pair of scans, as a research group delivers them."""
    ortho = write_tif(tmp_path / "ortho.tif", np.full((3, 300, 400), 100, dtype="uint8"),
                      res=0.01, origin=(X0 + 1.0, Y1 - 1.0))
    x, y = grid_points(8.0, 6.0, 0.05)
    late = write_las(tmp_path / "late.las", x, y, 180.0 + np.where(np.mod(x - X0, 0.76) < 0.4, 2.0, 1.2))
    early = write_las(tmp_path / "early.las", x, y, np.full(x.size, 180.0))
    return {"ortho": ortho, "dsm": late, "dtm": early}


def test_products_land_where_odm_would_have_put_them(tmp_path: Path) -> None:
    """Every later step then runs unchanged, and the check ODM runs must pass."""
    project = tmp_path / "project"
    done = import_products(project, crs=CRS, **_products(tmp_path))
    assert [item.name for item in done] == ["orthophoto", "dsm", "dtm"]
    found = verify_outputs(project)
    assert {"orthophoto", "dsm", "dtm"} <= set(found)
    for name in ("orthophoto", "dsm", "dtm"):
        assert (project / EXPECTED_OUTPUTS[name]).exists()


def test_surfaces_are_cropped_to_the_orthophoto(tmp_path: Path) -> None:
    """Measuring height where there is no colour would give half-measured units."""
    project = tmp_path / "project"
    import_products(project, crs=CRS, **_products(tmp_path))
    with rasterio.open(project / EXPECTED_OUTPUTS["dsm"]) as dsm:
        # The orthophoto spans x 1-5 m; with a 2 m margin that is -1 to 7 m, cut
        # to the scan, which starts at 0: 7 m wide, and never gridded past its edge.
        assert dsm.width * dsm.res[0] == pytest.approx(7.0, abs=0.06)
        assert dsm.bounds.left == pytest.approx(X0, abs=0.06)
        assert dsm.bounds.top == pytest.approx(Y1, abs=0.06)


def test_an_import_leaves_a_record_that_odm_did_not_build_it(tmp_path: Path) -> None:
    project = tmp_path / "project"
    import_products(project, crs=CRS, **_products(tmp_path))
    record = provenance(project)
    assert record is not None and "not built by ODM" in record["note"]
    assert json.loads((project / PROVENANCE_NAME).read_text())["products"][1]["name"] == "dsm"


def test_an_import_will_not_overwrite_products_silently(tmp_path: Path) -> None:
    """A real ODM run's outputs are hours of work; replacing them takes --force."""
    project = tmp_path / "project"
    products = _products(tmp_path)
    import_products(project, crs=CRS, **products)
    with pytest.raises(ProductImportError, match="--force"):
        import_products(project, crs=CRS, **products)
    import_products(project, crs=CRS, overwrite=True, **products)


def test_both_surfaces_are_required(tmp_path: Path) -> None:
    products = _products(tmp_path)
    with pytest.raises(ProductImportError, match="--dtm"):
        import_products(tmp_path / "p", crs=CRS, ortho=products["ortho"], dsm=products["dsm"], dtm=None)


def test_the_imported_canopy_is_the_difference_of_the_two_flights(tmp_path: Path) -> None:
    """End to end: rows at 2.0 m and furrow canopy at 1.2 m over flat ground."""
    from dosojos_drone.chm import compute_chm, load_surface

    project = tmp_path / "project"
    import_products(project, crs=CRS, **_products(tmp_path))
    canopy, stats = compute_chm(
        load_surface(project / EXPECTED_OUTPUTS["dsm"]),
        load_surface(project / EXPECTED_OUTPUTS["dtm"]),
        clamp_negative=True, smooth_m=0.0,
    )
    assert stats.max_m == pytest.approx(2.0, abs=0.02)
    assert float(np.nanmin(canopy)) == pytest.approx(1.2, abs=0.02)
