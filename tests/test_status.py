"""The checkbook and the ground report as texts, and the real checkbook run in-process."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import pytest
from conftest import SQUARE, FakeStatus

from dosojos_sat import cache as sat_cache
from dosojos_sat.fields import load_fields
from dosojos_sat.indices import IndexStats
from dosojos_sat.soils import manual_profile

from dosojos_sms import export, status, store, text
from dosojos_sms.status import FieldWater

TODAY = date(2026, 9, 12)


def record(**values) -> store.FieldRow:
    base = dict(id="F001", phone="+19565550123", name="Campo Norte", crop="sorghum",
                irrigation="furrow", outline={"type": "Polygon", "coordinates": [SQUARE + [SQUARE[0]]]},
                lat=26.1484, lon=-97.994)
    base.update(values)
    return store.FieldRow(**base)


def test_days_left_with_the_range_and_the_date() -> None:
    body = status.message(FieldWater(record(), FakeStatus()), "es", TODAY)
    assert body.split("\n\n")[0] == ("Campo Norte (sorgo): tiene agua para unos 5 días (4 a 7); riegue antes del "
                    "jue 17 sep, unas 5.3 pulgadas por surcos.")
    english = status.message(FieldWater(record(), FakeStatus()), "en", TODAY)
    assert english.startswith("Campo Norte (sorghum): water for about 5 days (4 to 7); water by "
                              "Thu Sep 17, about 5.3 inches by furrows.")


def test_rainfed_counts_days_until_rain() -> None:
    body = status.message(FieldWater(record(irrigation="none"), FakeStatus(method="none")),
                          "es", TODAY)
    answer = body.split("\n\n")[0]
    assert answer.endswith("si no llueve.") and "pulgadas" not in answer


def test_a_rainfed_crop_at_its_thirstiest_is_not_told_to_water() -> None:
    s = FakeStatus(method="none", days_left=0, days_range=None,
                   sensitive="grain sorghum reaches boot to flowering now (day 69 after planting)")
    body = status.message(FieldWater(record(irrigation="none"), s), "en", TODAY)
    assert body.split("\n\n")[0] == ("Campo Norte (sorghum): it needs rain now; the crop is under stress. "
                    "It's at boot to flowering, when drought hurts most.")


def test_stage_soon_and_rough() -> None:
    s = FakeStatus(sensitive="grain sorghum reaches boot to flowering in about 6 days (day 44)",
                   confidence="low")
    body = status.message(FieldWater(record(), s), "es", TODAY)
    assert ("Pronto entra en embuche y floración" in body
            and body.split("\n\n")[0].endswith("(Cálculo aproximado.)"))


@pytest.mark.parametrize("reason, words", [
    ("no_crop", "sin cultivo"), ("no_data", "todavía no tenemos"),
])
def test_reasons_there_is_no_answer_yet(reason: str, words: str) -> None:
    assert words in status.message(FieldWater(record(), reason=reason), "es", TODAY)


def test_no_map_sends_the_link() -> None:
    body = status.message(FieldWater(record(outline=None), reason="no_map"), "es", TODAY,
                          map_link=lambda r: "http://x/f/abc")
    assert body == "Campo Norte: falta marcar el mapa: http://x/f/abc"


def test_harvested() -> None:
    body = status.message(FieldWater(record(), FakeStatus(status="harvested")), "en", TODAY)
    assert body == "Campo Norte: harvested; no water needed until the next crop."


def test_most_urgent_first() -> None:
    later = FieldWater(record(id="F002"), FakeStatus(days_left=9))
    now = FieldWater(record(id="F003"), FakeStatus(days_left=0))
    none = FieldWater(record(id="F001"), reason="no_data")
    assert [i.field.id for i in sorted([none, later, now], key=status.urgency)] == \
        ["F003", "F002", "F001"]


# ---- the drone's ground report ---------------------------------------------------------

REPORT = {
    "cut_yd3_per_acre": 29.3,
    "advice": [
        {"topic": "high spot", "priority": 1, "advice": "Water runs around it.",
         "finding": "High spot H1 in the west side: 112 m2 standing up to 7 cm above the plane"},
        {"topic": "row ends", "priority": 1, "advice": "Shorter runs",
         "finding": "Stress bunches at the tail of the rows: 20% of pieces flagged in the last "
                    "third against 11% elsewhere."},
        {"topic": "grade", "priority": 3, "finding": "Falls 0.15% from north to south.",
         "advice": "A good grade for furrow and border irrigation."},
    ],
}


def test_ground_lines_in_spanish() -> None:
    lines = status.ground_lines(REPORT, "es", intake="slow")
    assert lines == [
        "Hay una parte alta en el lado oeste donde el agua no llega; rebájela al "
        "nivelar, o dele más tiempo de riego mientras.",
        "El agua no llega al final de los surcos. Haga tiradas más cortas (en 2 "
        "partes), más agua al inicio y luego menos, o válvulas de pulsos. En este barro, deje "
        "más tiempo al final.",
    ]


def test_ground_lines_when_the_ground_is_not_the_cause() -> None:
    report = {"advice": [{"topic": "cause", "priority": 2, "finding": "...", "advice": "..."}]}
    assert status.ground_lines(report, "en") == [
        "The weak plants follow neither the ground nor the row ends; check pests, "
        "disease, nutrients or salt."]


def test_a_low_spot_follows_the_leveling_advice() -> None:
    report = {"cut_yd3_per_acre": 119, "advice": [
        {"topic": "leveling", "priority": 1, "finding": "Uneven: only 38% ...", "advice": "..."},
        {"topic": "low spot", "priority": 2, "advice": "...",
         "finding": "Low spot L1 in the middle: 33,656 m2 down to 14 cm below the plane."},
        {"topic": "high spot", "priority": 3, "advice": "...",
         "finding": "High spot H1 in the east side: 22,092 m2 standing up to 13 cm"},
    ]}
    assert status.ground_lines(report, "en") == [
        "Uneven; laser leveling would even out the water (about 119 cubic yards per acre).",
        "A low spot in the middle where water stands; fill it when leveling, or open a drain.",
    ]


def test_two_low_spots_are_one_sentence() -> None:
    report = {"advice": [
        {"topic": "low spot", "priority": 2, "advice": "...",
         "finding": "Low spot L1 in the south-east corner: 9,860 m2 down to 13 cm below the plane."},
        {"topic": "low spot", "priority": 2, "advice": "...",
         "finding": "Low spot L2 in the south-west corner: 2,312 m2 down to 8 cm below the plane."},
    ]}
    assert status.ground_lines(report, "es") == [
        "Hay partes bajas en la esquina sureste y en la esquina suroeste donde se encharca; "
        "rellénelas al nivelar o abra un drenaje."]


def test_lidar_ground_says_when_it_was_measured() -> None:
    report = {**REPORT, "ground_source": "lidar", "flown_on": "2019-01-01"}
    lines = status.ground_lines(report, "en")
    assert len(lines) == 2
    assert lines[-1].endswith(" (Ground measured by government lidar in 2019.)")


def test_the_latest_flight_of_the_field(settings) -> None:
    drone = settings.drone_workspace
    (drone / "flights.json").write_text(json.dumps({"flights": {
        "F001-20260601": {"field_id": "F001", "flown_on": "2026-06-01"},
        "F001-20260910": {"field_id": "F001", "flown_on": "2026-09-10"},
        "F002-20260910": {"field_id": "F002", "flown_on": "2026-09-10"},
    }}), encoding="utf-8")
    for flight, cut in (("F001-20260601", 1.0), ("F001-20260910", 2.0)):
        (drone / "out" / flight).mkdir(parents=True)
        (drone / "out" / flight / "terrain.json").write_text(
            json.dumps({"cut_yd3_per_acre": cut, "advice": []}), encoding="utf-8")
    assert status.latest_terrain(settings, "F001")["cut_yd3_per_acre"] == 2.0
    assert status.latest_terrain(settings, "F001")["flown_on"] == "2026-09-10"
    assert status.latest_terrain(settings, "F002") is None


# ---- the real checkbook, from a satellite workspace ---------------------------------------


def test_the_checkbook_runs_on_the_farm_workspace(conn, settings) -> None:
    """Imagery, weather and soil cached as the daily run leaves them: a real answer."""
    store.add_farmer(conn, "+19565550123")
    field = store.add_field(conn, "+19565550123", "Campo Norte")
    field.outline = {"type": "Polygon", "coordinates": [SQUARE + [SQUARE[0]]]}
    field.crop, field.irrigation, field.acres_said = "sorghum", "furrow", 40.0
    store.save_field(conn, field)
    store.add_event(conn, "F001", date(2026, 7, 20), "planted")
    store.add_event(conn, "F001", date(2026, 8, 18), "irrigated")
    exported = export.export(conn, settings)

    start = date(2026, 7, 1)
    db = settings.sat_workspace / "cache" / "dosojos.sqlite"
    with sat_cache.session(db) as sat:
        sat_cache.upsert_fields(sat, load_fields(exported.fields_path))
        registered = sat_cache.get_fields(sat)[0]
        for offset in range(0, 74, 5):
            median = min(0.8, 0.2 + offset / 90)
            stats = IndexStats(mean=median, median=median, p10=median, p25=median, p75=median,
                               p90=median, std=0.01, valid_fraction=1.0, n_valid_px=9,
                               n_total_px=9)
            sat_cache.upsert_observation(sat, sat_cache.ObservationRecord(
                field_id="F001", obs_date=start + timedelta(days=offset), index_name="NDVI",
                stats=stats, scene_id="S2"))
        days = [start + timedelta(days=i) for i in range((TODAY - start).days + 1)]
        sat_cache.upsert_weather(sat, "F001", pd.DataFrame(
            {"date": days, "eto_mm": 6.0, "rain_mm": 0.0}), "gridmet")
        sat_cache.upsert_soil(sat, "F001", registered.geom_hash, "manual",
                              manual_profile(1.6).to_dict())

    result = status.Water(settings).field(store.get_field(conn, "F001"),
                                          store.events_for(conn, "F001"), TODAY)

    assert result.reason is None and result.status.days_left is not None
    assert result.status.method == "furrow" and result.status.start == "2026-07-20"
    body = status.message(result, "es", TODAY)
    assert body.startswith("Campo Norte (sorgo): ")
    assert all(c in text._GSM for c in text.gsm_safe(body))


def test_no_satellite_workspace_yet(conn, settings) -> None:
    result = status.Water(settings).field(record(), [], TODAY)
    assert result.reason == "no_data"


def test_the_numbers_behind_the_answer() -> None:
    body = status.message(FieldWater(record(acres=40.0), FakeStatus()), "en", TODAY)
    answer, details = body.split("\n\n")
    assert details.startswith("Hidalgo sandy clay loam soil: it holds 66% of the water it can "
                              "(4.1 of 6.2 in. in 36 in. of roots).")
    assert "It gets stressed below 2.5 in.; 1.6 in. to go before that." in details
    assert "It uses about 0.28 in. a day" in details
    assert "Last rain: Thu Sep 3 (0.4 in.)." in details
    assert "Last watering: Tue Aug 18." in details
    # 5.26 in. gross over 40 acres: 210 acre-inches, 5.7 million gallons.
    assert "For the 40 acres: about 210 acre-inches (about 5.7 million gallons)." in details
    assert "Satellite from Thu Sep 10; weather through Fri Sep 11" in details


def test_details_ask_for_waterings_and_skip_volumes_without_irrigation() -> None:
    none_logged = status.message(FieldWater(record(acres=40.0), FakeStatus(last_irrigation=None)),
                                 "es", TODAY)
    assert "mande REGUE fecha pulgadas" in none_logged
    rainfed = status.message(FieldWater(record(irrigation="none", acres=40.0),
                                        FakeStatus(method="none")), "en", TODAY)
    assert "acre-inches" not in rainfed and "WATERED" not in rainfed
