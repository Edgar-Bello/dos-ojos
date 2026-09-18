"""Work done right away: a new field read from the satellite, a flight processed."""

from __future__ import annotations

import json
from datetime import date

import pytest
from conftest import NOW, SQUARE, FakeWater, resolver_to

from dosojos_sms import store
from dosojos_sms.config import Settings
from dosojos_sms.jobs import Job, Jobs
from dosojos_sms.web import READ_ATTEMPTS, READ_RETRY_MINUTES, App


class Held(Jobs):
    """Keeps what is queued, so a test runs it when it chooses, with no thread."""

    def __init__(self, result: int = 0) -> None:
        super().__init__(run=lambda command: result)
        self.held: list[Job] = []

    def add(self, key, command, done, *, attempt=1) -> bool:
        if any(job.key == key for job in self.held):
            return False
        self.held.append(Job(key, command, done, attempt))
        return True

    def finish(self) -> list:
        """Run everything held; returns what each asked for next."""
        jobs, self.held = self.held, []
        return [job.done(self._run(job.command) == 0, job.attempt) for job in jobs]


@pytest.fixture
def app(tmp_path) -> App:
    settings = Settings.load(tmp_path / "farm_data", env={
        "DOSOJOS_READ_NOW": "1", "DOSOJOS_ON_UPLOAD": '"C:/team/python.exe" "step.py" --uploads-only'})
    settings.ensure_dirs()
    application = App(settings, sim=True, resolve=resolver_to("none"),
                      water=FakeWater(reason="no_data"))
    application.now = NOW
    application.jobs = Held()
    return application


def farmer_with_field(app: App, *, state: str = "idle", plan: str = "satellite",
                      planted: bool = True, outline: bool = True) -> str:
    with app.db() as conn:
        farmer = store.add_farmer(conn, "+19565550123", channel="sim")
        farmer.lang, farmer.state, farmer.name, farmer.plan = "en", state, "Rosa", plan
        farmer.alerts = True
        store.save_farmer(conn, farmer)
        record = store.add_field(conn, farmer.phone, "North Field")
        record.crop, record.irrigation = "sorghum", "furrow"
        if outline:
            record.outline = {"type": "Polygon", "coordinates": [SQUARE + [SQUARE[0]]]}
        store.save_field(conn, record)
        if planted:
            store.add_event(conn, record.id, date(2026, 7, 20), "planted")
        return record.id


def texts(app: App) -> list[str]:
    with app.db() as conn:
        return [r["body"] for r in store.messages(conn, "+19565550123") if r["direction"] == "out"]


def read_now(app: App, field_id: str) -> None:
    with app.lock, app.db() as conn:
        app._maybe_read(conn, store.get_field(conn, field_id))


def test_a_finished_field_is_read_straight_away(app: App) -> None:
    field_id = farmer_with_field(app)
    read_now(app, field_id)

    [job] = app.jobs.held
    assert job.key == f"read:{field_id}"
    assert job.command[-5:] == ["daily", "--fields", field_id, "--since-planting",
                                "--skip-baseline"]
    assert "--as-of" in job.command            # the pinned day goes along
    assert texts(app)[-1].startswith("I'm reading North Field from the satellite")

    app.water.reason = None                    # the reading has landed
    assert app.jobs.finish() == [None]
    answer, menu = texts(app)[-2:]
    assert answer.startswith("North Field")
    assert "STAGE" in menu and "APHID" in menu and "WHY" in menu and "HELP" in menu
    assert "DRONE" not in menu                 # a satellite-only farmer has no flight


def test_a_flying_farmer_is_told_about_drone(app: App) -> None:
    field_id = farmer_with_field(app, plan="drone")
    read_now(app, field_id)
    app.water.reason = None
    app.jobs.finish()
    assert "DRONE (send your flight)" in texts(app)[-1]


@pytest.mark.parametrize("change", [
    {"state": "f_method"},          # still answering sign-up questions
    {"planted": False},             # no planting date yet
    {"outline": False},             # no map yet
])
def test_nothing_is_read_before_the_field_is_complete(app: App, change: dict) -> None:
    read_now(app, farmer_with_field(app, **change))
    assert app.jobs.held == []


def test_the_same_field_is_read_once(app: App) -> None:
    field_id = farmer_with_field(app)
    read_now(app, field_id)
    app.water.reason = None
    app.jobs.finish()
    read_now(app, field_id)
    assert app.jobs.held == []


def test_a_field_read_before_a_restart_is_left_alone(app: App) -> None:
    field_id = farmer_with_field(app)
    app.water.reason = None                    # the checkbook already has it
    read_now(app, field_id)
    assert app.jobs.held == []


def test_a_failed_reading_is_tried_again_then_handed_to_the_team(app: App) -> None:
    app.jobs = Held(result=1)
    field_id = farmer_with_field(app)
    read_now(app, field_id)
    job = app.jobs.held[0]

    assert job.done(False, 1) == READ_RETRY_MINUTES * 60
    assert "try again in 15 minutes" in texts(app)[-1]
    assert job.done(False, READ_ATTEMPTS) is None
    assert "passed it to the team" in texts(app)[-1]


def test_reading_now_is_off_unless_asked_for(app: App, tmp_path) -> None:
    app.settings = Settings.load(tmp_path / "other")
    app.settings.ensure_dirs()
    read_now(app, farmer_with_field(app))
    assert app.jobs.held == []


def flight(app: App, field_id: str, flight_id: str, files: dict[str, str]) -> None:
    out = app.settings.drone_workspace / "out" / flight_id
    out.mkdir(parents=True)
    for name, content in files.items():
        (out / name).write_bytes(content.encode() if isinstance(content, str) else content)
    app._process_flight(field_id, flight_id)


def test_a_finished_flight_comes_back_with_its_picture(app: App) -> None:
    field_id = farmer_with_field(app, plan="drone")
    summary = {"n_judged": 9040, "n_stressed": 33, "n_dead": 0, "n_missing": 2907}
    flight(app, field_id, "F001-20260910", {"block_summary.json": json.dumps(summary),
                                             "flag_overlay.png": "PNG"})
    [job] = app.jobs.held
    assert job.command[:3] == ["C:/team/python.exe", "step.py", "--uploads-only"]
    assert job.command[-2:] == ["--flight", "F001-20260910"]

    app.jobs.finish()
    with app.db() as conn:
        last = store.messages(conn, "+19565550123")[-1]
    assert "2,940 of 9,040 stretches of row" in last["body"]
    assert "yellow = stressed" in last["body"]
    [url] = json.loads(last["media"])
    assert app.picture(url.rsplit("/", 1)[1]) == b"PNG"


def test_a_thermal_flight_says_where_to_look(app: App) -> None:
    field_id = farmer_with_field(app, plan="thermal")
    report = {"flight_id": "F001-20260910", "patches": [
        {"chance": 0.5, "where": "south-west", "area_m2": 29, "above_c": 4.6}]}
    flight(app, field_id, "F001-20260910", {"thermal.json": json.dumps(report),
                                             "thermal.png": "PNG"})
    app.jobs.finish()
    heat, pest = texts(app)[-2:]
    assert heat.startswith("The thermal scan of North Field is ready")
    assert "50%" in pest


def test_a_flight_that_fails_is_handed_to_the_team(app: App) -> None:
    app.jobs = Held(result=1)
    field_id = farmer_with_field(app, plan="drone")
    app._process_flight(field_id, "F001-20260910")
    app.jobs.finish()
    assert "couldn't process the flight" in texts(app)[-1]


def test_one_job_at_a_time_and_never_twice() -> None:
    ran, answered = [], []
    jobs = Jobs(run=lambda command: ran.append(command) or 0)
    job = Job("read:F001", ["daily"], lambda ok, attempt: answered.append((ok, attempt)))
    jobs._waiting.add(job.key)
    assert jobs.add("read:F001", ["daily"], job.done) is False     # already waiting
    jobs.run_one(job)
    assert ran == [["daily"]] and answered == [(True, 1)] and not jobs.busy("read:F001")


def test_a_job_that_cannot_start_is_a_failed_job() -> None:
    answered = []

    def missing(command):
        raise FileNotFoundError(command[0])

    Jobs(run=missing).run_one(Job("x", ["nope"], lambda ok, attempt: answered.append(ok)))
    assert answered == [False]


def test_stitched_photos_come_back_as_squares_of_the_field(app: App) -> None:
    field_id = farmer_with_field(app, plan="drone")
    summary = {"unit_type": "cell", "n_judged": 6265, "n_stressed": 854, "n_dead": 0,
               "n_missing": 1066}
    flight(app, field_id, "F001-20260910", {"block_summary.json": json.dumps(summary),
                                             "flag_overlay.png": "PNG"})
    app.jobs.finish()
    last = texts(app)[-1]
    assert "Your photos of North Field are joined into one map" in last
    assert "1,920 of 6,265 squares" in last
