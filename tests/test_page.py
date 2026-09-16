"""Tests for the one-page view of a flight, with and without a satellite half."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

import pytest

from dosojos_drone.page import (
    NO_SATELLITE,
    PageError,
    gather,
    headline,
    render,
)

FLIGHT = "DEMO-20240701"
FIELD = "DEMO-f1"


def _png(path: Path, colour: tuple[int, int, int] = (0, 128, 0)) -> None:
    """The smallest real PNG, so the page has something to carry."""
    raw = b"\x00" + bytes(colour)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    """A flight folder with a summary and one picture."""
    folder = tmp_path / "out" / FLIGHT
    folder.mkdir(parents=True)
    (folder / "block_summary.json").write_text(json.dumps({
        "field_id": FIELD, "flight_id": FLIGHT, "flown_on": "2024-07-01",
        "method": "rows", "unit_type": "row_segment",
        "n_trees": 1000, "n_healthy": 800, "n_stressed": 90, "n_dead": 10,
        "n_missing": 50, "n_not_assessed": 50, "n_judged": 950,
        "share_stressed": 0.0947, "share_dead": 0.0105, "share_missing": 0.0526,
        "share_problem": 0.158,
    }), encoding="utf-8")
    _png(folder / "flag_overlay.png")
    return tmp_path / "out"


FLIGHT_RECORD = {"field_id": FIELD, "flown_on": "2024-07-01",
                 "crop": "sorghum", "source": ""}


def test_a_flight_with_no_satellite_says_so_in_words(out_dir: Path) -> None:
    """A gap must never be silent: the farmer is told there is no satellite and why."""
    data = gather(out_dir, FLIGHT, flight=FLIGHT_RECORD)
    assert data.satellite is None
    page = render(data)
    assert "No satellite for this field" in page
    assert "too small for a Sentinel-2 pixel" in page


def test_the_page_is_still_worth_reading_without_a_satellite(out_dir: Path) -> None:
    """The drone's own numbers carry the page on their own."""
    page = render(gather(out_dir, FLIGHT, flight=FLIGHT_RECORD))
    assert "What the flight measured" in page
    assert "800" in page and "Worth a look" in page


def test_pictures_are_carried_inside_the_file(out_dir: Path) -> None:
    """Saved, forwarded or opened with no signal, the page still has its pictures."""
    page = render(gather(out_dir, FLIGHT, flight=FLIGHT_RECORD))
    assert "data:image/png;base64," in page
    assert "<img" in page and "src=\"http" not in page


def test_the_satellite_half_is_shown_when_there_is_one(out_dir: Path, tmp_path: Path) -> None:
    """With both eyes open the page leads with what the satellite saw."""
    sat = tmp_path / "sat_out"
    sat.mkdir()
    _png(sat / f"{FIELD}_NDVI.png")
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"fields": [{
        "field_id": FIELD, "latest_ndvi": 0.82, "percentile": 71.0,
        "note": "within its normal range", "last_observation_date": "2024-06-28",
        "water": {"status": "ok for now", "days_left": 9, "water_by": "2024-07-10"},
    }]}), encoding="utf-8")

    data = gather(out_dir, FLIGHT, flight=FLIGHT_RECORD,
                  satellite_dir=sat, triage_path=triage)
    page = render(data)
    assert "What the satellite says" in page
    assert "0.82" in page and "71%" in page
    assert "about 9 days of water left" in page
    assert NO_SATELLITE not in page


def test_a_field_the_satellite_never_saw_is_not_borrowed_from_another(
    out_dir: Path, tmp_path: Path
) -> None:
    """A triage file listing other fields must not lend this one their numbers."""
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"fields": [
        {"field_id": "SOMEONE-ELSE", "latest_ndvi": 0.91},
    ]}), encoding="utf-8")
    data = gather(out_dir, FLIGHT, flight=FLIGHT_RECORD, triage_path=triage)
    assert data.satellite is None
    assert "No satellite for this field" in render(data)


def test_public_data_wears_its_banner(out_dir: Path) -> None:
    """Borrowed pictures say whose they are, at the top, before anything else."""
    record = {**FLIGHT_RECORD, "source": "Purdue University, PURR doi:10.4231, CC0"}
    page = render(gather(out_dir, FLIGHT, flight=record))
    assert "FREE PUBLIC DATA, NOT OUR FLIGHT" in page
    assert "Purdue University" in page


def test_our_own_flight_wears_no_banner(out_dir: Path) -> None:
    """A banner on our own work would teach people to ignore it."""
    page = render(gather(out_dir, FLIGHT, flight=FLIGHT_RECORD))
    assert "FREE PUBLIC DATA" not in page


def test_ground_findings_are_listed_not_dumped(out_dir: Path) -> None:
    """terrain.json keeps findings as records; the page must read them, not print them."""
    (out_dir / FLIGHT / "terrain.json").write_text(json.dumps({
        "ground_source": "lidar",
        "advice": [
            {"topic": "low spot", "finding": "Low spot L1 in the north-west corner.",
             "advice": "Fill it or open a drain.", "priority": 2},
            {"topic": "leveling", "finding": "Mostly even.", "advice": "", "priority": 3},
        ],
    }), encoding="utf-8")
    page = render(gather(out_dir, FLIGHT, flight=FLIGHT_RECORD))
    assert "Low spot L1 in the north-west corner." in page
    assert "Fill it or open a drain." in page
    assert "'topic':" not in page and "priority" not in page


def test_ground_findings_are_worst_first(out_dir: Path) -> None:
    """Whoever reads only the first line should read the most urgent one."""
    (out_dir / FLIGHT / "terrain.json").write_text(json.dumps({"advice": [
        {"finding": "Third.", "priority": 3},
        {"finding": "First.", "priority": 1},
    ]}), encoding="utf-8")
    page = render(gather(out_dir, FLIGHT, flight=FLIGHT_RECORD))
    assert page.index("First.") < page.index("Third.")


def test_a_flight_that_was_never_processed_says_what_to_run(tmp_path: Path) -> None:
    """An empty out folder gets a instruction, not a traceback."""
    with pytest.raises(PageError, match="dosojos-drone report"):
        gather(tmp_path / "out", FLIGHT, flight=FLIGHT_RECORD)


def test_a_flight_with_pictures_but_no_verdicts_still_makes_a_page(tmp_path: Path) -> None:
    """Stopping after chm is a normal thing to do, and worth looking at."""
    folder = tmp_path / "out" / FLIGHT
    folder.mkdir(parents=True)
    _png(folder / "chm.png")
    data = gather(tmp_path / "out", FLIGHT, flight=FLIGHT_RECORD)
    page = render(data)
    assert "nothing was judged" in headline(data).lower()
    assert "data:image/png;base64," in page


def test_the_headline_counts_trees_for_a_tree_crop(out_dir: Path) -> None:
    """A citrus grower is not counting stretches of row."""
    summary = json.loads((out_dir / FLIGHT / "block_summary.json").read_text())
    summary["unit_type"] = "crown"
    (out_dir / FLIGHT / "block_summary.json").write_text(json.dumps(summary),
                                                         encoding="utf-8")
    assert "trees" in headline(gather(out_dir, FLIGHT, flight=FLIGHT_RECORD))


def test_a_field_in_good_order_is_told_so(out_dir: Path) -> None:
    """Most flights find nothing much, and should say that plainly."""
    summary = json.loads((out_dir / FLIGHT / "block_summary.json").read_text())
    summary["share_problem"] = 0.02
    (out_dir / FLIGHT / "block_summary.json").write_text(json.dumps(summary),
                                                         encoding="utf-8")
    assert "good order" in headline(gather(out_dir, FLIGHT, flight=FLIGHT_RECORD))


def test_a_crop_name_with_html_in_it_cannot_break_the_page(out_dir: Path) -> None:
    """Field names come from farmers and public files; neither is trusted markup."""
    record = {**FLIGHT_RECORD, "crop": "<script>alert(1)</script>"}
    page = render(gather(out_dir, FLIGHT, flight=record, field_name="<b>x</b>"))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_every_picture_decodes(out_dir: Path) -> None:
    """A data URI that is not a real picture is a blank box on somebody's phone."""
    data = gather(out_dir, FLIGHT, flight=FLIGHT_RECORD)
    for figure in data.figures:
        head, _, payload = figure.data_uri.partition(",")
        assert head.endswith("base64")
        assert base64.b64decode(payload).startswith(b"\x89PNG")
