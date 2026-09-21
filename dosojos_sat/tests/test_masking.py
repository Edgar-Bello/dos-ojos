"""Tests for SCL masking, reflectance scaling and tile selection.

All inputs are synthetic arrays and hand-built footprints with known answers, so
nothing here touches the network.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pystac
import pytest
from affine import Affine
from pyproj import Transformer
from shapely.geometry import Polygon, box, mapping

from dosojos_sat import stac
from dosojos_sat.config import BAND_ASSETS, MASK_CLASSES
from dosojos_sat.fields import Field
from dosojos_sat.indices import (
    class_fraction,
    observation_mask,
    scl_valid_mask,
    valid_fraction,
)

_UTM_TO_WGS84 = Transformer.from_crs("EPSG:32614", "EPSG:4326", always_xy=True)


def _to_wgs84(geom: Polygon) -> Polygon:
    """Move a UTM 14N polygon into WGS84."""
    return Polygon([_UTM_TO_WGS84.transform(x, y) for x, y in geom.exterior.coords])


def _settings() -> "Settings":
    """Default settings rooted at the current directory; no I/O is performed."""
    from dosojos_sat.config import Settings

    return Settings.from_root(Path("."))


def _field(geometry: Polygon, field_id: str = "t1") -> Field:
    """Build a Field around a WGS84 geometry, skipping area computation."""
    return Field(
        field_id=field_id,
        name="Test",
        crop="sorghum",
        geometry=geometry,
        utm_epsg=32614,
        acres_computed=1.0,
    )


def _scene(
    scene_id: str,
    footprint: Polygon,
    *,
    nodata: float = 0.0,
    cloud: float = 10.0,
    jp2: bool = False,
    boa_applied: bool = True,
) -> stac.SceneRef:
    """Build a SceneRef wrapping a minimal STAC item with a given footprint."""
    moment = datetime(2026, 6, 1, 17, 0, tzinfo=timezone.utc)
    item = pystac.Item(
        id=scene_id,
        geometry=mapping(footprint),
        bbox=list(footprint.bounds),
        datetime=moment,
        properties={
            "s2:nodata_pixel_percentage": nodata,
            "eo:cloud_cover": cloud,
            "earthsearch:boa_offset_applied": boa_applied,
        },
    )
    for band, asset_key in BAND_ASSETS.items():
        href = (
            f"s3://sentinel-s2-l2a/tiles/{scene_id}/{band}.jp2"
            if jp2
            else f"https://sentinel-cogs.s3.us-west-2.amazonaws.com/{scene_id}/{band}.tif"
        )
        item.add_asset(
            asset_key,
            pystac.Asset(
                href=href,
                extra_fields={
                    "raster:bands": [
                        {"nodata": 0, "scale": 0.0001, "offset": -0.1}
                    ]
                },
            ),
        )
    return stac.SceneRef(
        scene_id=scene_id,
        solar_date=date(2026, 6, 1),
        datetime_utc=moment,
        platform="sentinel-2a",
        mgrs_tile=scene_id,
        epsg=32614,
        cloud_cover=cloud,
        processing_baseline="05.12",
        boa_offset_applied=boa_applied,
        item=item,
    )


# --------------------------------------------------------------------------- #
# SCL masking
# --------------------------------------------------------------------------- #


def test_masked_classes_are_exactly_the_specified_set() -> None:
    """Classes 0,1,3,8,9,10,11 are rejected; every other class is kept."""
    scl = np.arange(12, dtype=np.uint8)
    valid = scl_valid_mask(scl)
    assert set(scl[~valid].tolist()) == set(MASK_CLASSES)
    assert set(scl[valid].tolist()) == {2, 4, 5, 6, 7}


def test_valid_fraction_counts_only_inside_the_polygon() -> None:
    """The denominator is in-polygon pixels, not the whole clip grid."""
    poly = np.zeros((10, 10), dtype=bool)
    poly[:5, :] = True                       # 50 pixels inside the field
    valid = np.zeros((10, 10), dtype=bool)
    valid[:4, :] = True                      # 40 of them usable
    assert valid_fraction(valid, poly) == pytest.approx(0.8)


def test_valid_fraction_of_empty_polygon_is_zero() -> None:
    """An empty outline reports an unusable observation rather than dividing by zero."""
    empty = np.zeros((4, 4), dtype=bool)
    assert valid_fraction(np.ones((4, 4), dtype=bool), empty) == 0.0


def test_observation_mask_requires_polygon_scl_and_finite_bands() -> None:
    """A pixel counts only if it is inside, SCL-clean, and present in every band."""
    poly = np.ones((2, 3), dtype=bool)
    poly[0, 0] = False                       # outside the field
    scl = np.full((2, 3), 4, dtype=np.uint8)
    scl[0, 1] = 9                            # high probability cloud
    band_a = np.ones((2, 3), dtype=np.float32)
    band_b = np.ones((2, 3), dtype=np.float32)
    band_b[1, 2] = np.nan                    # nodata gap in one band only

    mask = observation_mask(scl, [band_a, band_b], poly)
    assert mask.tolist() == [[False, False, True], [True, True, False]]
    assert valid_fraction(mask, poly) == pytest.approx(3 / 5)


def test_class_fraction_separates_nodata_from_cloud() -> None:
    """Nodata coverage is measured against in-polygon pixels only."""
    poly = np.ones((2, 2), dtype=bool)
    poly[1, 1] = False
    scl = np.array([[0, 0], [4, 0]], dtype=np.uint8)
    assert class_fraction(scl, poly, 0) == pytest.approx(2 / 3)
    assert class_fraction(scl, poly, 4) == pytest.approx(1 / 3)


# --------------------------------------------------------------------------- #
# Reflectance scaling
# --------------------------------------------------------------------------- #


def test_reflectance_applies_scale_and_offset() -> None:
    """DN 3000 with scale 1e-4 and offset -0.1 is 0.2 reflectance."""
    raw = np.array([[3000, 1000]], dtype=np.uint16)
    out = stac._to_reflectance(raw, 0.0001, -0.1, 0)
    assert out[0, 0] == pytest.approx(0.2, abs=1e-6)
    assert out[0, 1] == pytest.approx(0.0, abs=1e-6)  # float32, so not bit-exact zero


def test_reflectance_nodata_becomes_nan_not_a_plausible_value() -> None:
    """Nodata is knocked out before scaling, so 0 never becomes -0.1."""
    raw = np.array([[0, 5000]], dtype=np.uint16)
    out = stac._to_reflectance(raw, 0.0001, -0.1, 0)
    assert np.isnan(out[0, 0])
    assert out[0, 1] == pytest.approx(0.4)


def test_declared_offset_is_ignored_when_the_mirror_already_applied_it() -> None:
    """``earthsearch:boa_offset_applied`` overrides the declared raster:bands offset.

    Earth Search harmonises the pixels when it builds the COGs but still declares
    ESA's -0.1 offset. Re-applying it drove roughly 70% of a field's green and red
    reflectance negative, which is physically impossible and quietly wrecked NDVI.
    """
    field_scene = _scene("cog", _to_wgs84(box(540000, 2850000, 660000, 2960000)))
    scale, offset, nodata = stac._reflectance_transform([field_scene], "red")
    assert (scale, offset, nodata) == (0.0001, 0.0, 0)


def test_offset_is_applied_when_the_mirror_has_not_done_it() -> None:
    """An item without the flag still needs ESA's offset subtracted."""
    raw_scene = _scene(
        "raw", _to_wgs84(box(540000, 2850000, 660000, 2960000)), boa_applied=False
    )
    _, offset, _ = stac._reflectance_transform([raw_scene], "red")
    assert offset == pytest.approx(-0.1)


def test_mostly_negative_band_is_flagged(caplog: pytest.LogCaptureFixture) -> None:
    """A misapplied offset must announce itself instead of corrupting indices."""
    poly = np.ones((10, 10), dtype=bool)
    bands = {"B04": np.full((10, 10), -0.02, dtype=np.float32)}
    with caplog.at_level("WARNING"):
        stac.warn_if_implausible(bands, poly, "f1", date(2026, 6, 1))
    assert "negative reflectance" in caplog.text


def test_a_few_negative_pixels_are_not_flagged(caplog: pytest.LogCaptureFixture) -> None:
    """Genuine correction artifacts over dark canopy stay quiet."""
    poly = np.ones((10, 10), dtype=bool)
    values = np.full((10, 10), 0.2, dtype=np.float32)
    values[0, :5] = -0.01                     # 5% of pixels
    with caplog.at_level("WARNING"):
        stac.warn_if_implausible({"B04": values}, poly, "f1", date(2026, 6, 1))
    assert caplog.text == ""


def test_harmonised_dn_gives_physical_reflectance() -> None:
    """Real December digital numbers must yield positive reflectance and sane NDVI.

    Regression for the corrupted run: DN 888 red and 2858 NIR over a partly
    harvested field are ordinary values, not a negative-reflectance anomaly.
    """
    red = stac._to_reflectance(np.array([888.0]), 0.0001, 0.0, 0)
    nir = stac._to_reflectance(np.array([2858.0]), 0.0001, 0.0, 0)
    assert red[0] == pytest.approx(0.0888)
    assert nir[0] == pytest.approx(0.2858)

    ndvi = float((nir[0] - red[0]) / (nir[0] + red[0]))
    assert ndvi == pytest.approx(0.526, abs=1e-3)


# --------------------------------------------------------------------------- #
# Polygon rasterisation
# --------------------------------------------------------------------------- #


def test_polygon_mask_selects_interior_pixels() -> None:
    """A 100 m square on a 10 m grid covers exactly 100 pixels."""
    field = _field(_to_wgs84(box(600000, 2899000, 600100, 2899100)))
    transform = Affine(10, 0, 599980, 0, -10, 2899120)
    mask = stac.polygon_mask(field, transform, (14, 14))
    assert mask.sum() == 100


def test_polygon_mask_falls_back_for_subpixel_field() -> None:
    """A field smaller than one pixel still yields data instead of an empty mask."""
    field = _field(_to_wgs84(box(600002, 2899002, 600005, 2899005)))
    transform = Affine(10, 0, 600000, 0, -10, 2899010)
    mask = stac.polygon_mask(field, transform, (1, 1))
    assert mask.sum() == 1


# --------------------------------------------------------------------------- #
# Tile selection
# --------------------------------------------------------------------------- #


def test_overlapping_tiles_collapse_to_the_cleanest_one() -> None:
    """Tiles that each contain the whole field are redundant; read only the best.

    Sentinel-2 MGRS tiles overlap by about 10 km, so this is the common case for a
    small field, and reading all four would cost four times the requests.
    """
    field = _field(_to_wgs84(box(600000, 2899000, 600100, 2899100)))
    big = _to_wgs84(box(540000, 2850000, 660000, 2960000))
    scenes = [
        _scene("tile-a", big, nodata=18.8, cloud=29.6),
        _scene("tile-b", big, nodata=0.0, cloud=53.2),
        _scene("tile-c", big, nodata=2.2, cloud=48.3),
    ]
    chosen = stac.select_covering_items(field, scenes)
    assert [s.scene_id for s in chosen] == ["tile-b"]


def test_field_on_a_seam_keeps_every_tile_it_needs() -> None:
    """When no single tile covers the field, enough are kept to span it."""
    field = _field(_to_wgs84(box(600000, 2899000, 600200, 2899100)))
    west = _to_wgs84(box(590000, 2890000, 600100, 2910000))
    east = _to_wgs84(box(600100, 2890000, 610000, 2910000))
    chosen = stac.select_covering_items(field, [_scene("w", west), _scene("e", east)])
    assert {s.scene_id for s in chosen} == {"w", "e"}


def test_read_order_keeps_other_covering_tiles_as_fallbacks() -> None:
    """Redundant tiles are demoted to fallbacks, not discarded.

    Overlapping tiles of one pass share the same weather but differ in where
    their data ends, so a second tile is worth trying if the first is empty here.
    """
    field = _field(_to_wgs84(box(600000, 2899000, 600100, 2899100)))
    big = _to_wgs84(box(540000, 2850000, 660000, 2960000))
    scenes = [
        _scene("tile-a", big, nodata=18.8),
        _scene("tile-b", big, nodata=0.0),
        _scene("tile-c", big, nodata=2.2),
    ]
    order = stac._read_order(field, scenes)
    assert [[s.scene_id for s in group] for group in order] == [
        ["tile-b"], ["tile-c"], ["tile-a"],
    ]


def test_requester_pays_jp2_scenes_are_skipped() -> None:
    """Items pointing at the JP2 originals cannot be read anonymously.

    Another tile of the same pass covers identical ground, so preferring the COG
    one turns an unreadable day into a usable observation.
    """
    field = _field(_to_wgs84(box(600000, 2899000, 600100, 2899100)))
    big = _to_wgs84(box(540000, 2850000, 660000, 2960000))
    jp2_tile = _scene("jp2-tile", big, nodata=0.0, jp2=True)
    cog_tile = _scene("cog-tile", big, nodata=5.0)

    assert stac.is_readable(jp2_tile) is False
    assert stac.is_readable(cog_tile) is True
    # The JP2 tile has less nodata and would otherwise win on preference.
    assert [s.scene_id for s in stac.select_covering_items(field, [jp2_tile, cog_tile])] == [
        "cog-tile"
    ]


def test_unreadable_day_still_attempts_a_read() -> None:
    """If every tile is JP2 there is nothing better, so try rather than skip."""
    field = _field(_to_wgs84(box(600000, 2899000, 600100, 2899100)))
    big = _to_wgs84(box(540000, 2850000, 660000, 2960000))
    only_jp2 = [_scene("a", big, jp2=True), _scene("b", big, jp2=True)]
    assert len(stac.select_covering_items(field, only_jp2)) == 1


def test_day_with_only_jp2_tiles_is_dropped_not_errored() -> None:
    """An unreadable day must be marked resolved so later runs stop retrying it.

    Recording it as an error would leave it permanently outstanding, and every
    subsequent fetch would pay four backing-off retries to fail the same way.
    """
    field = _field(_to_wgs84(box(600000, 2899000, 600100, 2899100)))
    big = _to_wgs84(box(540000, 2850000, 660000, 2960000))
    settings = _settings()
    outcome = stac.load_and_mask(
        field, [_scene("only-jp2", big, jp2=True)], settings
    )
    assert outcome.status == "dropped"
    assert "requester-pays" in outcome.reason
    assert outcome.duration_ms == 0        # nothing was attempted


@pytest.mark.parametrize(
    "message",
    [
        "'/vsis3/x/B03.jp2' does not exist in the file system",
        "not recognized as a supported dataset name",
        "Access Denied",
    ],
)
def test_permanent_failures_are_not_retried(message: str) -> None:
    """A missing or private object will not appear on the fourth attempt."""
    calls = []

    def always_fails() -> None:
        calls.append(1)
        raise RuntimeError(message)

    with pytest.raises(stac.StacUnavailableError):
        stac.with_retry(always_fails, what="test read", attempts=4, base_delay=0.01)
    assert len(calls) == 1


def test_transient_failures_are_retried() -> None:
    """A genuine blip still gets its retries."""
    calls = []

    def fails_twice() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("connection reset by peer")
        return "ok"

    assert stac.with_retry(fails_twice, what="test", attempts=4, base_delay=0.01) == "ok"
    assert len(calls) == 3


def test_nodata_dominated_distinguishes_missing_data_from_cloud() -> None:
    """Only a dropped observation that is mostly nodata warrants another tile."""

    def outcome(status: str, nodata: float) -> stac.ClipOutcome:
        return stac.ClipOutcome(
            field_id="t1", solar_date=date(2026, 6, 1), scene_ids=("s",),
            status=status, reason="", valid_fraction=0.1, n_valid_px=1,
            n_total_px=10, duration_ms=1, nodata_fraction=nodata,
        )

    assert stac._nodata_dominated(outcome("dropped", 0.9)) is True
    assert stac._nodata_dominated(outcome("dropped", 0.05)) is False   # cloudy, not empty
    assert stac._nodata_dominated(outcome("kept", 0.9)) is False


def test_single_scene_day_is_passed_through() -> None:
    """A day with one item needs no selection work."""
    field = _field(_to_wgs84(box(600000, 2899000, 600100, 2899100)))
    only = _scene("solo", _to_wgs84(box(540000, 2850000, 660000, 2960000)))
    assert stac.select_covering_items(field, [only]) == [only]


# --------------------------------------------------------------------------- #
# Offline policy
# --------------------------------------------------------------------------- #


def test_assert_online_raises_only_when_offline() -> None:
    """The guard is a no-op online and a clear refusal offline."""
    from dosojos_sat.config import Settings
    from pathlib import Path

    online = Settings.from_root(Path("."))
    stac.assert_online(online, "do a thing")

    offline = Settings.from_root(Path("."), offline=True)
    with pytest.raises(stac.OfflineViolation, match="--offline is set"):
        stac.assert_online(offline, "do a thing")


class _Band:
    def __init__(self, values: np.ndarray):
        self.values = values


def _day(green: int, red: int, nir: int = 3500, swir: int = 2000) -> dict:
    """One solar day's raw bands, as odc-stac hands them back, uniform over the field."""
    shape = (6, 6)
    return {BAND_ASSETS[b]: _Band(np.full(shape, dn, dtype=np.uint16))
            for b, dn in (("B03", green), ("B04", red), ("B08", nir), ("B11", swir))}


FIELD_MASK = np.ones((6, 6), dtype=bool)
FOOTPRINT = box(540000, 2850000, 660000, 2960000)


def test_a_mislabelled_item_is_read_without_the_offset_it_already_carries() -> None:
    """Flag false, yet the bytes already carry the offset (2025 baseline 05.11 items).

    Green 0.06 and red 0.04 are a leafy crop; subtracting the offset again makes
    them -0.04 and -0.06, and NDVI pins at 1.
    """
    scene = _scene("S2C_14RPQ_20250224_0_L2A", _to_wgs84(FOOTPRINT), boa_applied=False)
    day = _day(green=600, red=400)
    bands = stac._read_bands(day, [scene])
    assert stac.negative_share(bands["B04"], FIELD_MASK) == 1.0
    fixed = stac._without_double_offset(day, [scene], bands, FIELD_MASK)
    assert fixed is not None
    assert fixed["B03"][0, 0] == pytest.approx(0.06) and fixed["B04"][0, 0] == pytest.approx(0.04)


def test_a_correctly_labelled_raw_item_keeps_its_offset() -> None:
    scene = _scene("raw", _to_wgs84(FOOTPRINT), boa_applied=False)
    day = _day(green=1600, red=1400)            # raw bytes, offset still to subtract
    bands = stac._read_bands(day, [scene])
    assert bands["B04"][0, 0] == pytest.approx(0.04)
    assert stac._without_double_offset(day, [scene], bands, FIELD_MASK) is None


def test_items_flagged_as_harmonised_are_never_second_guessed() -> None:
    scene = _scene("cog", _to_wgs84(FOOTPRINT), boa_applied=True)
    day = _day(green=600, red=400)
    assert stac._without_double_offset(day, [scene], stac._read_bands(day, [scene]),
                                       FIELD_MASK) is None


def test_bands_that_stay_negative_either_way_are_left_for_the_warning() -> None:
    """Dropping the offset must clear the negatives, or it is not the explanation."""
    scene = _scene("odd", _to_wgs84(FOOTPRINT), boa_applied=False)
    day = _day(green=600, red=400)
    bands = stac._read_bands(day, [scene])
    bands = {k: v - 0.5 for k, v in bands.items()}          # something else is wrong
    plain_read = stac._read_bands

    def still_negative(d, s, *, offset=None):
        return {k: v - 0.5 for k, v in plain_read(d, s, offset=offset).items()}

    stac._read_bands = still_negative
    try:
        assert stac._without_double_offset(day, [scene], bands, FIELD_MASK) is None
    finally:
        stac._read_bands = plain_read


def test_an_item_flagged_raw_whose_pixels_are_harmonised_gets_no_offset(monkeypatch) -> None:
    """Dark water and canopy below DN 1000 cannot exist in a file still carrying +1000."""
    monkeypatch.setattr(stac, "_dark_red_dn", lambda scene: 387.0)
    scene = _scene("S2C_14RPQ_20250224_0_L2A", _to_wgs84(FOOTPRINT), boa_applied=False)
    assert stac.harmonised(scene)
    assert stac._reflectance_transform([scene], "red")[1] == 0.0


def test_an_item_flagged_raw_whose_pixels_are_raw_keeps_the_offset(monkeypatch) -> None:
    monkeypatch.setattr(stac, "_dark_red_dn", lambda scene: 1075.0)
    scene = _scene("raw", _to_wgs84(FOOTPRINT), boa_applied=False)
    assert not stac.harmonised(scene)
    assert stac._reflectance_transform([scene], "red")[1] == pytest.approx(-0.1)


def test_each_scene_is_sampled_once(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(stac, "_dark_red_dn", lambda scene: calls.append(scene.scene_id) or 400.0)
    scene = _scene("once", _to_wgs84(FOOTPRINT), boa_applied=False)
    for _ in range(3):
        stac.harmonised(scene)
    assert calls == ["once"]


def test_items_from_before_the_offset_era_are_not_checked(monkeypatch) -> None:
    """Baseline < 04.00 declares no offset; dark pixels there are just dark pixels."""
    calls = []
    monkeypatch.setattr(stac, "_dark_red_dn", lambda scene: calls.append(1) or 5.0)
    scene = _scene("old", _to_wgs84(FOOTPRINT), boa_applied=False)
    for asset in scene.item.assets.values():
        asset.extra_fields["raster:bands"] = [{"nodata": 0, "scale": 0.0001, "offset": 0}]
    assert not stac.harmonised(scene) and calls == []
    assert stac._reflectance_transform([scene], "red")[1] == 0.0
