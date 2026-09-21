"""Tests for the block summary, the satellite join, and the demo figures."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point, box

from dosojos_drone.flags import classify_units
from dosojos_drone.report import (
    ReportError,
    agreement,
    block_summary,
    join_with_satellite,
    load_satellite_flags,
    read_ortho_preview,
    save_flag_histogram,
    save_flag_overlay,
)

UTM = "EPSG:32614"

#: The satellite pipeline's own output shape, trimmed to the fields that matter.
SATELLITE = {
    "season": 2026,
    "generated": "2026-09-09T15:36:26+00:00",
    "fields": [
        {"field_id": "rgv-003", "name": "Citrus C", "crop": "citrus",
         "score": 85.4, "flagged": True, "water_stress": True},
        {"field_id": "rgv-001", "name": "Llano West", "crop": "sugarcane",
         "score": 74.4, "flagged": True, "water_stress": True},
        {"field_id": "rgv-002", "name": "Mercedes 40", "crop": "grain sorghum",
         "score": 5.7, "flagged": False, "water_stress": False},
    ],
}


def _flagged(flags: list[str], *, volume: float = 1.5, method: str = "rows") -> gpd.GeoDataFrame:
    """Flagged units with the columns the summary reads."""
    n = len(flags)
    return gpd.GeoDataFrame(
        {
            "unit_id": [f"u{i}" for i in range(n)],
            "flag": flags,
            "volume_m3": [volume] * n,
            "height_mean_m": [1.0] * n,
            "exg_mean": [0.28] * n,
            "area_m2": [1.0] * n,
        },
        geometry=[box(600000 + i, 2900000, 600001 + i, 2900001) for i in range(n)],
        crs=UTM,
    )


def _summary(field_id: str, share: float, flown_on: str = "2026-09-05", **extra) -> dict:
    """A drone block summary with a chosen problem share."""
    return {
        "field_id": field_id, "flight_id": f"f-{field_id}-{flown_on}",
        "flown_on": flown_on, "unit_type": "crown", "n_trees": 100,
        "n_healthy": int(100 * (1 - share)), "n_stressed": int(100 * share),
        "n_dead": 0, "n_missing": 0, "share_problem": share,
        "median_canopy_volume": 23.0, "mean_ExG": 0.48, **extra,
    }


# --------------------------------------------------------------------------- #
# Block summary
# --------------------------------------------------------------------------- #


def test_summary_carries_every_key_the_join_needs() -> None:
    """The requested keys, named exactly, since other code may already read them."""
    summary = block_summary(
        _flagged(["HEALTHY", "STRESSED", "DEAD"]),
        flight_id="f1", field_id="rgv-002", method="watershed",
    )
    for key in ("field_id", "n_trees", "n_dead", "n_missing", "n_stressed",
                "median_canopy_volume", "mean_ExG"):
        assert key in summary
    assert summary["field_id"] == "rgv-002"


def test_summary_says_what_a_tree_is_for_row_crops() -> None:
    """n_trees counts row segments on sorghum, and unit_type says so."""
    rows = block_summary(_flagged(["HEALTHY"]), flight_id="f", field_id="x", method="rows")
    crowns = block_summary(_flagged(["HEALTHY"]), flight_id="f", field_id="x", method="watershed")
    assert rows["unit_type"] == "row_segment"
    assert crowns["unit_type"] == "crown"


def test_orchard_gaps_count_as_missing() -> None:
    """Empty grid positions are missing trees, though they are not units."""
    summary = block_summary(
        _flagged(["HEALTHY"] * 8), flight_id="f", field_id="x",
        method="watershed", n_missing_positions=2,
    )
    assert summary["n_missing"] == 2
    assert summary["share_missing"] == pytest.approx(0.2)


def test_edge_units_do_not_dilute_the_problem_share() -> None:
    """Shares are over units actually judged.

    Counting edge slivers in the denominator would lower every rate by however
    much boundary a particular flight happened to have.
    """
    summary = block_summary(
        _flagged(["STRESSED"] * 2 + ["HEALTHY"] * 8 + ["EDGE"] * 10),
        flight_id="f", field_id="x", method="rows",
    )
    assert summary["n_not_assessed"] == 10
    assert summary["share_problem"] == pytest.approx(0.2)


# --------------------------------------------------------------------------- #
# EDGE
# --------------------------------------------------------------------------- #


def test_clipped_row_segment_is_edge_not_missing() -> None:
    """A segment clipped across its row keeps the furrow and loses the ridge.

    Its cover collapses and it reads as missing plants. On the synthetic field
    that put nine false MISSING flags down the east edge, one per row.
    """
    n = 60
    rng = np.random.default_rng(0)
    frame = gpd.GeoDataFrame(
        {
            "unit_id": [f"u{i}" for i in range(n)],
            "n_pixels": [100] * n,
            "area_m2": [1.53] * (n - 1) + [0.5],
            "canopy_cover": list(0.57 + rng.normal(0, 0.02, n - 1)) + [0.05],
            "height_mean_m": list(1.0 + rng.normal(0, 0.05, n - 1)) + [0.4],
            "volume_m3": [1.5] * n,
            "exg_mean": [0.28] * n,
            "row": [0] * n, "segment": list(range(n)),
        },
        geometry=[box(i, 0, i + 1, 1) for i in range(n)], crs=UTM,
    )
    flagged = classify_units(frame, method="rows")
    assert flagged["flag"].iloc[-1] == "EDGE"
    assert "clipped" in flagged["reason"].iloc[-1]


def test_orchard_crowns_are_never_edge() -> None:
    """For crowns, a small area is the stress signal, not a clipping artifact."""
    frame = _flagged(["HEALTHY"] * 20)
    frame["area_m2"] = [10.0] * 19 + [2.0]
    frame["n_pixels"] = 100
    frame["canopy_cover"] = 0.9
    flagged = classify_units(frame.drop(columns="flag"), method="watershed")
    assert "EDGE" not in set(flagged["flag"])


# --------------------------------------------------------------------------- #
# Agreement
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("flagged", "share", "expected"),
    [
        (True, 0.25, "confirmed"),
        (True, 0.02, "not_confirmed"),
        (False, 0.25, "drone_only"),
        (False, 0.02, "both_clear"),
    ],
)
def test_agreement_between_the_two_eyes(flagged: bool, share: float, expected: str) -> None:
    """Every combination of satellite alarm and drone finding has a name."""
    satellite = {"field_id": "x", "flagged": flagged}
    assert agreement(satellite, _summary("x", share), concern=0.10) == expected


def test_satellite_alarm_the_drone_cannot_confirm_suggests_a_harvest() -> None:
    """The case that matters most: an orbital alarm with healthy plants below.

    A harvested field crashes NDVI exactly like a stressed one; only the drone
    can tell them apart, and this verdict is where it does.
    """
    satellite = {"field_id": "x", "flagged": True}
    assert agreement(satellite, _summary("x", 0.01), concern=0.10) == "not_confirmed"


def test_one_eye_alone_is_reported_as_such() -> None:
    """No flight yet, or no satellite record, is not the same as agreement."""
    assert agreement({"field_id": "x", "flagged": True}, None, concern=0.1) == "satellite_only"
    assert agreement(None, _summary("x", 0.5), concern=0.1) == "drone_only_no_satellite"


# --------------------------------------------------------------------------- #
# The join
# --------------------------------------------------------------------------- #


def test_join_keeps_the_satellite_ranking_intact() -> None:
    """Order and every original key survive; the drone only adds to them."""
    joined = join_with_satellite(SATELLITE, [_summary("rgv-003", 0.21)])
    assert [f["field_id"] for f in joined["fields"]] == ["rgv-003", "rgv-001", "rgv-002"]
    first = joined["fields"][0]
    for key in ("name", "crop", "score", "flagged", "water_stress"):
        assert first[key] == SATELLITE["fields"][0][key]


def test_join_attaches_drone_results_on_field_id() -> None:
    """The drone record lands on the field it was flown over, and nowhere else."""
    joined = join_with_satellite(SATELLITE, [_summary("rgv-003", 0.21)])
    by_id = {f["field_id"]: f for f in joined["fields"]}
    assert by_id["rgv-003"]["drone"]["share_problem"] == pytest.approx(0.21)
    assert by_id["rgv-003"]["agreement"] == "confirmed"
    assert by_id["rgv-001"]["drone"] is None
    assert by_id["rgv-001"]["agreement"] == "satellite_only"


def test_the_most_recent_flight_wins() -> None:
    """Two flights over one field: the latest is current, the other is counted."""
    joined = join_with_satellite(SATELLITE, [
        _summary("rgv-003", 0.40, flown_on="2026-08-01"),
        _summary("rgv-003", 0.05, flown_on="2026-09-06"),
    ])
    drone = next(f for f in joined["fields"] if f["field_id"] == "rgv-003")["drone"]
    assert drone["flown_on"] == "2026-09-06"
    assert drone["n_other_flights"] == 1


def test_a_flight_over_an_unscored_field_is_still_reported() -> None:
    """A drone flight with no matching satellite field must not vanish."""
    joined = join_with_satellite(SATELLITE, [_summary("rgv-999", 0.3)])
    extra = next(f for f in joined["fields"] if f["field_id"] == "rgv-999")
    assert extra["agreement"] == "drone_only_no_satellite"


def test_summaries_without_a_field_are_ignored() -> None:
    """An unregistered flight has nothing to join on."""
    joined = join_with_satellite(SATELLITE, [{**_summary("x", 0.3), "field_id": None}])
    assert len(joined["fields"]) == 3


def test_missing_satellite_file_names_the_command(tmp_path: Path) -> None:
    """The error is the instruction for producing the file."""
    with pytest.raises(ReportError, match="dosojos-sat score"):
        load_satellite_flags(tmp_path / "absent.json")


def test_wrong_shaped_satellite_file_is_rejected(tmp_path: Path) -> None:
    """Some other JSON must not be joined as though it were satellite flags."""
    path = tmp_path / "flags.json"
    path.write_text(json.dumps({"hello": 1}), encoding="utf-8")
    with pytest.raises(ReportError, match="fields"):
        load_satellite_flags(path)


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #


def _ortho(path: Path, size: int = 400, res: float = 0.02) -> Path:
    """A green orthophoto covering the test units."""
    rgb = np.zeros((3, size, size), dtype="uint8")
    rgb[1] = 140
    rgb[0], rgb[2] = 60, 40
    with rasterio.open(
        path, "w", driver="GTiff", height=size, width=size, count=3, dtype="uint8",
        crs=UTM, transform=from_origin(600000, 2900008, res, res),
    ) as dataset:
        dataset.write(rgb)
    return path


def test_ortho_preview_is_decimated_and_windowed(tmp_path: Path) -> None:
    """A real mosaic is too large to read whole just to draw a slide."""
    path = _ortho(tmp_path / "o.tif", size=400)
    rgb, transform = read_ortho_preview(
        path, (600000, 2900000, 600004, 2900004), max_px=50
    )
    assert max(rgb.shape[:2]) <= 50
    assert rgb.shape[2] == 3
    assert 0.0 <= rgb.min() and rgb.max() <= 1.0


def test_units_outside_the_orthophoto_fail_clearly(tmp_path: Path) -> None:
    """Drawing on nothing is a sign the flight and the units do not match."""
    path = _ortho(tmp_path / "o.tif")
    with pytest.raises(ReportError, match="do not overlap"):
        read_ortho_preview(path, (700000, 3000000, 700010, 3000010))


def test_overlay_and_histogram_are_written(tmp_path: Path) -> None:
    """Both demo figures render to real PNGs."""
    flagged = _flagged(["HEALTHY"] * 5 + ["STRESSED", "MISSING", "DEAD"])
    missing = gpd.GeoDataFrame(geometry=[Point(600003.5, 2900000.5)], crs=UTM)

    overlay = save_flag_overlay(
        flagged, _ortho(tmp_path / "o.tif"), tmp_path / "overlay.png",
        title="rgv-003 - citrus", subtitle="3 of 8 need a look",
        missing_points=missing,
    )
    flagged["volume_m3"] = np.linspace(1, 20, len(flagged))
    histogram = save_flag_histogram(
        flagged, "watershed", tmp_path / "hist.png", title="rgv-003"
    )
    for path in (overlay, histogram):
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert path.stat().st_size > 5_000


def test_overlay_renders_without_an_orthophoto(tmp_path: Path) -> None:
    """A run with no mosaic still produces a usable map of the flags."""
    path = save_flag_overlay(
        _flagged(["STRESSED", "HEALTHY"]), None, tmp_path / "o.png", title="t"
    )
    assert path.exists()


def test_histogram_needs_values(tmp_path: Path) -> None:
    """An empty measurement is an error, not a blank chart."""
    flagged = _flagged(["HEALTHY"])
    flagged["height_mean_m"] = np.nan
    with pytest.raises(ReportError, match="no finite"):
        save_flag_histogram(flagged, "rows", tmp_path / "h.png", title="t")


# --------------------------------------------------------------------------- #
# Borrowed data and blocks
# --------------------------------------------------------------------------- #


def test_public_data_carries_its_source_into_the_joined_record() -> None:
    """Whatever reads triage.json must be able to tell borrowed data from ours."""
    source = "Purdue University, PURR doi:10.4231/MY7W-FH43, CC0"
    summary = block_summary(_flagged(["HEALTHY"] * 9 + ["MISSING"]), flight_id="p",
                            field_id="PUBLIC-x", method="rows", source=source)
    assert summary["source"] == source
    joined = join_with_satellite({"fields": [{"field_id": "PUBLIC-x", "flagged": False}]},
                                 [summary])
    assert joined["fields"][0]["drone"]["source"] == source


def test_shares_state_how_many_units_were_actually_judged() -> None:
    """Edge pieces are not judged, so '14% of all units' would overstate the base."""
    summary = block_summary(_flagged(["HEALTHY"] * 6 + ["EDGE"] * 3 + ["STRESSED"]),
                            flight_id="f", field_id="x", method="rows")
    assert summary["n_trees"] == 10 and summary["n_judged"] == 7
    assert summary["share_problem"] == pytest.approx(1 / 7, abs=1e-4)


def test_figures_for_public_data_carry_a_banner(tmp_path: Path) -> None:
    flagged = _flagged(["HEALTHY"] * 8 + ["STRESSED", "MISSING"])
    banner = "FREE PUBLIC DATA, NOT OUR FLIGHT"
    overlay = save_flag_overlay(flagged, None, tmp_path / "o.png", title="t", banner=banner)
    histogram = save_flag_histogram(flagged, "rows", tmp_path / "h.png", title="t", banner=banner)
    assert overlay.stat().st_size > 0 and histogram.stat().st_size > 0


def test_block_judged_units_are_plotted_against_their_own_block(tmp_path: Path) -> None:
    flagged = _flagged(["HEALTHY"] * 10).assign(
        height_mean_m=[2.4] * 5 + [1.2] * 5, block=["a"] * 5 + ["b"] * 5)
    path = save_flag_histogram(flagged, "rows", tmp_path / "h.png", title="t", within="block")
    assert path.exists()
