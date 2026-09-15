"""The team's commands, and the way in when Windows blocks the .exe launcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from conftest import SQUARE, FakeStatus, FakeWater

from dosojos_sms import status as status_mod
from dosojos_sms import store
from dosojos_sms.cli import cli
from dosojos_sms.config import ConfigError, Settings

SCRIPT = """# a farmer, start to finish
hola
1
Juan Ejemplo
si
Campo Norte
40
26.1484, -97.9940
1
7/20
si
1
N
8/18 4
si
no
"""


def run(data: Path, *args: str, input: str | None = None):
    result = CliRunner().invoke(cli, ["--data", str(data), "--as-of", "2026-09-12", *args],
                                input=input, catch_exceptions=False)
    return result


@pytest.fixture
def farm(tmp_path: Path) -> Path:
    data = tmp_path / "farm_data"
    script = tmp_path / "farmer.txt"
    script.write_text(SCRIPT, encoding="utf-8")
    result = run(data, "replay", str(script))
    assert result.exit_code == 0, result.output
    return data


@pytest.mark.parametrize("module", ["dosojos_sms", "dosojos_sms.cli"])
def test_python_dash_m_reaches_every_command(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"], capture_output=True, check=False,
        encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0, result.stderr
    listed = {line.split()[0] for line in result.stdout.split("Commands:")[1].splitlines()
              if line.strip()}
    assert set(cli.commands) <= listed


def test_python_dash_m_ignores_a_folder_named_like_the_package(tmp_path: Path) -> None:
    """Dos_Ojos/ holds a dosojos_sms folder, which must not hide the package."""
    (tmp_path / "dosojos_sms" / "tests").mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, "-m", "dosojos_sms", "--help"], cwd=tmp_path, capture_output=True,
        check=False, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0, result.stderr
    assert "serve" in result.stdout


def test_replay_fields_and_farmers(farm: Path) -> None:
    fields = run(farm, "fields").output
    assert "F001" in fields and "Campo Norte" in fields and "map" in fields
    farmers = run(farm, "farmers").output
    assert "+19565550123" in farmers and "Juan Ejemplo" in farmers and "idle" in farmers


def test_todo_names_the_map_to_draw(farm: Path) -> None:
    assert "MAP     F001 Campo Norte" in run(farm, "todo").output


def test_outline_picks_a_feature_by_id(farm: Path, tmp_path: Path) -> None:
    small = [[-97.9990, 26.1470], [-97.9990, 26.1488], [-97.9969, 26.1488], [-97.9969, 26.1470]]
    path = tmp_path / "fields.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": "other"},
         "geometry": {"type": "Polygon", "coordinates": [small + [small[0]]]}},
        {"type": "Feature", "properties": {"id": "this-one"},
         "geometry": {"type": "Polygon", "coordinates": [SQUARE + [SQUARE[0]]]}},
    ]}), encoding="utf-8")
    result = run(farm, "outline", "F001", str(path), "--id", "this-one", "--quiet")
    assert result.exit_code == 0, result.output
    assert "F001 Campo Norte: 41.5 acres" in result.output
    missing = run(farm, "outline", "F001", str(path), "--id", "nope")
    assert missing.exit_code != 0 and "no feature with id 'nope'" in missing.output


def test_a_demo_folder_pins_its_own_day(tmp_path: Path) -> None:
    script = tmp_path / "farmer.txt"
    script.write_text(SCRIPT.split("7/20")[0] + "7/20\n", encoding="utf-8")
    data = tmp_path / "demo"
    (data / "sms").mkdir(parents=True)
    (data / "sms" / "sms.env").write_text("DOSOJOS_AS_OF=2025-05-20\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["--data", str(data), "replay", str(script)],
                                catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "20 jul 2024" in result.output       # last July, seen from May 2025


def test_a_pinned_day_must_be_a_date(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="2025-05-20"):
        Settings.load(tmp_path, env={"DOSOJOS_AS_OF": "May 20"})


def test_outline_from_a_google_earth_kml(farm: Path, tmp_path: Path) -> None:
    ring = " ".join(f"{lon},{lat},0" for lon, lat in SQUARE + [SQUARE[0]])
    kml = tmp_path / "field.kml"
    kml.write_text(f'<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Placemark>'
                   f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{ring}</coordinates>"
                   f"</LinearRing></outerBoundaryIs></Polygon></Placemark></kml>", encoding="utf-8")
    result = run(farm, "outline", "F001", str(kml))
    assert result.exit_code == 0, result.output
    assert "F001 Campo Norte: 41.5 acres (the farmer said 40)" in result.output
    assert "text to +19565550123: kept" in result.output
    exported = run(farm, "export").output
    assert "1 field(s) and 2 event(s)" in exported
    features = json.loads((farm / "dosojos_sat" / "fields.geojson").read_text())["features"]
    assert features[0]["properties"]["water_enters"] == "N"


def test_say_texts_a_farmer(farm: Path) -> None:
    result = run(farm, "say", "956-555-0123", "Hola Juan, mañana pasamos a volar.")
    assert result.output.strip() == "+19565550123: kept"
    assert "mañana pasamos a volar" in run(farm, "messages", "--last", "1").output


def test_the_chat_command(tmp_path: Path) -> None:
    result = run(tmp_path / "farm_data", "chat", "--phone", "9565550160", input="hola\n1\n\n")
    assert "Texting as +19565550160" in result.output
    assert "Responda 1 para español" in result.output and "¿Como se llama?" in result.output


def test_remind_lists_before_it_sends(farm: Path, monkeypatch) -> None:
    monkeypatch.setattr(status_mod, "Water", lambda settings: FakeWater(
        FakeStatus(days_left=2, days_range=[1, 4], water_by="2026-09-14")))
    with store.session(farm / "sms" / "sms.sqlite") as conn:
        record = store.get_field(conn, "F001")
        record.outline = {"type": "Polygon", "coordinates": [SQUARE + [SQUARE[0]]]}
        store.save_field(conn, record)

    listed = run(farm, "remind").output
    assert "water_soon" in listed and "not sent (add --send)" in listed
    assert "necesitará agua en unos 2 días, antes del lun 14 sep" in listed

    sent = run(farm, "remind", "--send").output
    assert "water_soon" in sent and "kept" in sent
    assert "No alerts or reminders due." in run(farm, "remind").output


def test_remind_asks_for_a_missing_fact(tmp_path: Path, monkeypatch) -> None:
    """A farmer who stopped halfway is sent the map, then asked the next thing."""
    monkeypatch.setattr(status_mod, "Water", lambda settings: FakeWater(reason="no_data"))
    data = tmp_path / "farm_data"
    script = tmp_path / "farmer.txt"
    script.write_text("hola\n1\nJuan\nsi\nCampo Norte\n40\n26.1484, -97.9940\n1\n7/20\nsi\n"
                      "MENU\n", encoding="utf-8")
    run(data, "replay", str(script))
    listed = run(data, "remind").output
    assert "missing" in listed and "falta marcar el mapa de Campo Norte" in listed
    with store.session(data / "sms" / "sms.sqlite") as conn:
        record = store.get_field(conn, "F001")
        record.outline = {"type": "Polygon", "coordinates": [SQUARE + [SQUARE[0]]]}
        store.save_field(conn, record)
    listed = run(data, "remind").output
    assert "nos falta un dato" in listed and "riega Campo Norte" in listed
    run(data, "remind", "--send")
    with store.session(data / "sms" / "sms.sqlite") as conn:
        assert store.get_farmer(conn, "+19565550123").state == "f:method"
    assert "No alerts or reminders due." in run(data, "remind").output


def test_daily_with_nothing_to_look_at(tmp_path: Path) -> None:
    result = run(tmp_path / "farm_data", "daily")
    assert result.exit_code == 0 and "nothing for the satellite" in result.output
