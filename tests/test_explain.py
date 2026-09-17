"""The "why" file: the page a farmer gets when they reply PORQUE after AGUA."""

from __future__ import annotations

import json
from datetime import timedelta

import pandas as pd
import pytest
from conftest import NOW, TODAY, FakeStatus, Phone, draw, onboard, register_field

from dosojos_sms import explain, store
from dosojos_sms.status import FieldWater


def _item(conn, field_id: str = "F001", *, charts: bool = False, **status_values) -> FieldWater:
    """One field's checkbook as the page receives it, optionally with frames to plot."""
    record = store.get_field(conn, field_id)
    item = FieldWater(record, status=FakeStatus(**status_values), intake="slow",
                      soil={"name": "Hidalgo sandy clay loam", "awc_in_per_ft": 1.9})
    if charts:
        item.daily, item.projection = _balance()
        item.ndvi, item.baseline = _season()
        item.baseline_years = (2021, 2024)
    return item


def _balance() -> tuple[pd.DataFrame, pd.DataFrame]:
    """A season's worth of root-zone balance, in the columns the chart wants."""
    days = [TODAY - timedelta(days=n) for n in range(54, -1, -1)]
    daily = pd.DataFrame({
        "date": days,
        "taw_mm": [150.0] * len(days),
        "raw_mm": [90.0] * len(days),
        "dr_mm": [min(89.0, 1.6 * n) for n in range(len(days))],
        "rain_effective_mm": [12.0 if n == 20 else 0.0 for n in range(len(days))],
        "irrigation_mm": [100.0 if n == 5 else 0.0 for n in range(len(days))],
    })
    ahead = [TODAY + timedelta(days=n) for n in range(1, 6)]
    projection = pd.DataFrame({"date": ahead, "dr_mm": [90.0 + 7 * n for n in range(5)]})
    return daily, projection


def _season() -> tuple[pd.DataFrame, pd.DataFrame]:
    """This year's greenness and the field's own normal for it."""
    season = pd.DataFrame({
        "date": [TODAY - timedelta(days=5 * n) for n in range(12)],
        "median": [0.72 - 0.01 * n for n in range(12)],
    })
    baseline = pd.DataFrame({
        "doy": list(range(1, 366)),
        "median": [0.55] * 365, "p10": [0.40] * 365, "p25": [0.48] * 365,
        "p75": [0.62] * 365, "p90": [0.70] * 365,
        "n_obs": [40] * 365, "n_years": [4] * 365,
        "interpolated": [0] * 365, "confidence": ["good"] * 365,
    })
    return season, baseline


def _built(settings, conn, **kwargs) -> str:
    farmer = store.get_farmer(conn, "+19565550123")
    item = kwargs.pop("item", None) or _item(conn)
    return explain.build(settings, farmer, item, store.events_for(conn, item.field.id),
                         today=TODAY, **kwargs)


@pytest.fixture
def farm(phone: Phone, conn):
    """One farmer with one drawn field, watered and planted."""
    onboard(phone)
    register_field(phone)
    draw(conn, "F001")
    return conn


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #


def test_the_page_says_the_same_thing_the_text_message_said(settings, farm) -> None:
    """A farmer comparing the two must not find them disagreeing."""
    from dosojos_sms.status import message as status_message

    item = _item(farm)
    page = _built(settings, farm, item=item)
    assert status_message(item, "es", TODAY) in page


def test_the_page_shows_the_arithmetic_with_this_field_s_own_numbers(settings, farm) -> None:
    page = _built(settings, farm)
    assert "Hidalgo sandy clay loam" in page
    assert "6.2 pulgadas" in page              # what the root zone holds
    assert "0.28 pulgadas al d" in page        # what it uses a day
    assert "1.6 pulgadas" in page              # what is left


def test_the_page_lists_every_date_the_sums_used(settings, farm) -> None:
    """A wrong date is the commonest reason an answer is wrong, so it is shown."""
    page = _built(settings, farm)
    assert "Lo que usted nos dijo" in page
    assert "riego" in page and "4 in" in page


def test_the_page_says_the_normal_is_the_field_s_own_history(settings, farm) -> None:
    page = _built(settings, farm, item=_item(farm, charts=True))
    assert "2021-2024" in page
    assert "Nunca comparamos su campo con el de otro" in page


def test_a_field_with_no_season_yet_says_so_instead_of_an_empty_frame(settings, farm) -> None:
    page = _built(settings, farm)
    assert "Falta la gr" in page
    assert "<img" not in page


def test_a_field_with_a_season_but_no_history_says_which_is_missing(settings, farm) -> None:
    """A green line with nothing to judge it against is not the picture we promised."""
    item = _item(farm, charts=True)
    item.baseline = item.baseline.iloc[0:0]           # a field registered this year
    item.baseline_years = None
    page = _built(settings, farm, item=item)
    assert "Falta la banda gris" in page
    assert "suficientes a" in page                    # not enough years of this field
    assert page.count('<img src="data:image/png;base64,') == 1   # the water chart still drew


def test_the_charts_are_inlined_so_a_saved_file_still_shows_them(settings, farm) -> None:
    page = _built(settings, farm, item=_item(farm, charts=True))
    assert page.count('<img src="data:image/png;base64,') == 2
    assert "http://" not in page and "https://" not in page


def test_the_page_is_in_the_farmer_s_language(settings, phone: Phone, conn) -> None:
    onboard(phone, lang="2")
    register_field(phone, crop="1", planted="7/20", method="1", side="N", last="8/18 4")
    draw(conn, "F001")
    page = _built(settings, conn)
    assert "How that number came out" in page
    assert "never compare your field with anyone else" in page


def test_the_page_says_what_it_is_not(settings, farm) -> None:
    """Nobody should read a checkbook as a soil-moisture probe."""
    page = _built(settings, farm)
    assert "Lo que esto no es" in page
    assert "no una medici" in page


def test_a_demo_folder_s_banner_reaches_the_page(settings, farm) -> None:
    banner = "EXAMPLE - made-up farmer"
    marked = type(settings)(**{**settings.__dict__, "banner": banner})
    page = _built(marked, farm)
    assert banner in page


# --------------------------------------------------------------------------- #
# The ground and the thermal camera
# --------------------------------------------------------------------------- #


#: The smallest valid PNG, to stand in for a figure the drone half wrote.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "0005570bf1b70000000049454e44ae426082")


def _terrain(settings, **extra) -> dict:
    drone = settings.drone_workspace
    (drone / "flights.json").write_text(json.dumps({"flights": {
        "F001-20190401": {"field_id": "F001", "flown_on": "2019-04-01"}}}), encoding="utf-8")
    (drone / "out" / "F001-20190401").mkdir(parents=True, exist_ok=True)
    payload = {"ground_source": "lidar", "advice": [
        {"topic": "high spot", "priority": 1, "finding": "High spot H1 in the west side",
         "advice": "knock it down"}], **extra}
    (drone / "out" / "F001-20190401" / "terrain.json").write_text(
        json.dumps(payload), encoding="utf-8")
    from dosojos_sms.status import latest_terrain
    return latest_terrain(settings, "F001")


def test_the_ground_section_says_who_measured_it_and_when(settings, farm) -> None:
    page = _built(settings, farm, terrain=_terrain(settings))
    assert "USGS 3DEP" in page and "2019" in page
    assert "Nadie vol" in page                 # nobody flew anything of theirs


def _thermal_report(chance: float = 0.62, **patch) -> dict:
    return {"flight_id": "F001-20260910", "flown_on": "2026-09-10", "notes": [],
            "patches": [{"chance": chance, "where": "north-east corner", "area_m2": 1240.0,
                         "above_c": 3.1, "signs": ["runs 3.1 C above the rest of the canopy"],
                         "sign_keys": ["hot"], **patch}]}


def test_the_thermal_section_gives_a_chance_and_the_signs_behind_it(settings, farm) -> None:
    page = _built(settings, farm, thermal=_thermal_report())
    assert "62%" in page
    assert "esquina noreste" in page
    # The drone half writes its signs in English; a Spanish page says them in Spanish.
    assert "corre +3.1 C arriba del resto del cultivo" in page
    assert "runs 3.1 C above" not in page


def test_a_sign_this_page_has_not_learned_still_shows(settings, farm) -> None:
    """A key the drone half grows later must not silently drop off the page."""
    report = _thermal_report(sign_keys=["something_new"],
                             signs=["the moon was in the wrong quarter"])
    assert "the moon was in the wrong quarter" in _built(settings, farm, thermal=report)


def test_the_signs_are_in_english_for_an_english_grower(settings, phone: Phone, conn) -> None:
    onboard(phone, lang="2")
    register_field(phone)
    draw(conn, "F001")
    page = _built(settings, conn, thermal=_thermal_report())
    assert "runs +3.1 C above the rest of the canopy" in page


def test_the_thermal_section_refuses_to_sound_like_a_diagnosis(settings, farm) -> None:
    page = _built(settings, farm, thermal=_thermal_report())
    assert "no es un diagn" in page.lower() or "No es un an" in page
    assert "Vaya a verlo" in page


def test_a_farmer_s_own_flight_adds_its_height_and_flag_pictures(settings, farm) -> None:
    """Only a drone can give these, so a satellite-only field has no such section."""
    report = _terrain(settings)
    folder = settings.drone_workspace / "out" / report["flight_id"]
    for name in ("chm", "flag_overlay"):
        (folder / f"{name}.png").write_bytes(PNG)

    page = _built(settings, farm, terrain=report)
    assert "la altura del cultivo" in page
    assert "planta por planta" in page
    assert page.count('<img src="data:image/png;base64,') == 2      # no terrain.png written


def test_without_a_flight_there_are_no_flight_pictures(settings, farm) -> None:
    assert "altura del cultivo" not in _built(settings, farm, terrain=_terrain(settings))


def test_without_a_thermal_camera_there_is_no_thermal_section(settings, farm) -> None:
    assert "rmica" not in _built(settings, farm).replace("térmica", "")


# --------------------------------------------------------------------------- #
# Through the conversation and the web server
# --------------------------------------------------------------------------- #


def test_agua_offers_the_file_and_porque_sends_a_link(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    draw(conn, "F001")
    assert "PORQUE" in phone("AGUA")[-1]
    reply = phone("PORQUE")[0]
    assert "/r/" in reply and "Campo Norte" in reply


def test_asking_twice_gives_the_same_link(phone: Phone, conn) -> None:
    """A farmer who kept the first text should find that link still works."""
    onboard(phone)
    register_field(phone)
    draw(conn, "F001")
    first = phone("PORQUE")[0].split("/r/")[1].split()[0]
    assert phone("PORQUE")[0].split("/r/")[1].split()[0] == first


def test_why_works_in_english_too(phone: Phone, conn) -> None:
    onboard(phone, lang="2")
    register_field(phone)
    draw(conn, "F001")
    assert "WHY" in phone("WATER")[-1]
    assert "/r/" in phone("WHY")[0]


def test_why_before_the_satellite_has_looked_says_so(phone: Phone, conn, water) -> None:
    onboard(phone)
    register_field(phone)
    water.reason = "no_data"
    assert "satélite" in phone("PORQUE")[0] or "satelite" in phone("PORQUE")[0]
    assert "/r/" not in phone("PORQUE")[0]


def test_the_link_serves_the_page_and_a_download(settings, phone: Phone, conn, water) -> None:
    from dosojos_sms.web import App

    onboard(phone)
    register_field(phone)
    draw(conn, "F001")
    token = phone("PORQUE")[0].split("/r/")[1].split()[0]
    conn.commit()                 # the server opens its own connection to the same file

    app = App(settings, water=water)
    app.now = NOW
    page = app.explain_page(token).decode("utf-8")
    assert "<!doctype html>" in page and "Campo Norte" in page
    assert f"{token}/file" in page                     # the save button
    assert f"{token}/file" not in app.explain_page(token, download=True).decode("utf-8")


def test_an_expired_link_is_refused(settings, phone: Phone, conn, water) -> None:
    from dosojos_sms.web import App, HttpError

    onboard(phone)
    register_field(phone)
    draw(conn, "F001")
    token = phone("PORQUE")[0].split("/r/")[1].split()[0]
    conn.commit()
    app = App(settings, water=water)
    app.now = NOW.replace(year=NOW.year + 1)
    with pytest.raises(HttpError):
        app.explain_page(token)


# --------------------------------------------------------------------------- #
# Sorghum
# --------------------------------------------------------------------------- #


def _sorghum_item(conn, *, stage_key: str = "flowering", assumed: bool = False,
                  heat: bool = True) -> FieldWater:
    """A sorghum field with the stage the checkbook hands back, and the heat behind it."""
    from dosojos_sat import stages

    planted = TODAY - timedelta(days=69)
    weather = pd.DataFrame({"date": [planted + timedelta(days=n) for n in range(70)],
                            "tmax_c": [35.0] * 70, "tmin_c": [21.1] * 70})
    estimate = stages.estimate(weather, planted, TODAY, None if assumed else "medium")
    data = estimate.to_dict()
    data["stage"] = stage_key
    item = _item(conn, stage=data)
    if heat:
        item.heat = stages.gdu_series(weather, planted, TODAY)
    return item


def test_a_sorghum_page_says_where_the_crop_is_and_why_by_heat(settings, farm) -> None:
    page = _built(settings, farm, item=_sorghum_item(farm))
    assert "La etapa del sorgo" in page and "va en floración" in page
    assert "grados-día" in page and "no con el calendario" in page
    assert "Lo calculamos como ciclo mediano, que es lo que usted nos dijo" in page


def test_the_stage_chart_is_inlined(settings, farm) -> None:
    page = _built(settings, farm, item=_sorghum_item(farm))
    section = page.split("La etapa del sorgo")[1].split("Plagas del sorgo")[0]
    assert section.count('src="data:image/png;base64,') == 1


def test_no_heat_series_says_so_instead_of_a_blank(settings, farm) -> None:
    page = _built(settings, farm, item=_sorghum_item(farm, heat=False))
    assert "Todavía no hay suficientes temperaturas" in page


def test_an_assumed_maturity_is_explained_on_the_page(settings, farm) -> None:
    page = _built(settings, farm, item=_sorghum_item(farm, assumed=True))
    assert "No sabemos el ciclo del híbrido" in page and "mande CICLO" in page


def test_the_stage_table_marks_where_the_crop_is_now(settings, farm) -> None:
    page = _built(settings, farm, item=_sorghum_item(farm, stage_key="flowering"))
    assert '<tr class="now"><td>floración</td>' in page
    assert "mosquita" in page and "pulgón amarillo" in page


def test_the_pest_guidance_names_its_thresholds_and_who_to_ask(settings, farm) -> None:
    page = _built(settings, farm, item=_sorghum_item(farm))
    assert "20% de las plantas antes del panojeo" in page and "30% de panojeo a grano duro" in page
    assert "1 por panoja" in page and "AgriLife en Weslaco" in page
    assert "Texas A&amp;M AgriLife B-6137" in page and "Sorghum Checkoff" in page


def test_aphid_counts_sent_in_are_listed(settings, farm, phone: Phone) -> None:
    store.add_event(farm, "F001", TODAY, "scouting", note=json.dumps(
        {"pest": "sugarcane_aphid", "percent": 32.5, "infested": 26, "checked": 80,
         "stage": "flowering", "threshold": 30, "verdict": "above"}))
    page = _built(settings, farm, item=_sorghum_item(farm))
    counts = page.split("Sus conteos de pulgón")[1]
    assert "33%" in counts or "32%" in counts
    assert "26 / 80" in counts and "pasó el umbral (30%)" in counts


def test_other_crops_have_no_sorghum_section(settings, farm) -> None:
    page = _built(settings, farm, item=_item(farm, crop_model="corn"))
    assert "La etapa del sorgo" not in page and "B-6137" not in page


def test_the_start_of_the_count_is_said_in_the_farmer_s_language(settings, farm) -> None:
    """The checkbook says 'planted 2026-07-20' for the team; the page says it in Spanish."""
    page = _built(settings, farm, item=_item(farm, start_reason="planted 2026-07-20"))
    assert "(la siembra)" in page and "planted 2026" not in page
