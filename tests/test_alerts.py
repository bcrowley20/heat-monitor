from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from heatmon.alerts import AlertSettings, AlertsJob, conditions, load_settings, save_settings
from heatmon.db.models import Alert, SourceStatus, TankReading, ThermostatReading, Zone
from heatmon.db.session import insert_ignore
from heatmon.web.settings import Seen, changes

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def _zone_id(sessions, name):
    with sessions() as db:
        return next(z.id for z in db.query(Zone) if z.name == name)


def _readings(sessions, zone, ambient_c, setpoint_c=20.0, hours=2.5, mode="HEAT", eco="OFF"):
    zid = _zone_id(sessions, zone)
    steps = int(hours * 30)
    insert_ignore(sessions, ThermostatReading, [
        {"device_id": zone, "ts": NOW - timedelta(minutes=2 * i), "zone_id": zid, "ambient_c": ambient_c,
         "setpoint_heat_c": setpoint_c, "mode": mode, "eco_mode": eco, "hvac_status": "HEATING"}
        for i in range(steps + 1)])


def test_settings_round_trip(sessions):
    assert load_settings(sessions) == AlertSettings()
    save_settings(sessions, AlertSettings(tank_low_pct=25, pushover_user_key="u"))
    save_settings(sessions, AlertSettings(tank_low_pct=30, pushover_user_key="u"))
    assert load_settings(sessions).tank_low_pct == 30


def test_tank_low(sessions):
    insert_ignore(sessions, TankReading, [{"ts": NOW, "level_pct": 18.0}])
    found = conditions(sessions, AlertSettings(tank_low_pct=20), NOW)
    assert "18%" in found["tank_low", "tank"]
    assert conditions(sessions, AlertSettings(tank_low_pct=15), NOW) == {}
    assert conditions(sessions, AlertSettings(tank_low_on=False), NOW) == {}


def test_source_stale_only_for_running_sources(sessions):
    with sessions.begin() as db:
        db.add(SourceStatus(source="nest", last_success_ts=NOW - timedelta(hours=7)))
        db.add(SourceStatus(source="old", last_success_ts=NOW - timedelta(hours=70)))
        db.add(SourceStatus(source="wunderground", last_success_ts=NOW - timedelta(hours=1)))
    found = conditions(sessions, AlertSettings(), NOW, sources={"nest", "wunderground"})
    assert list(found) == [("source_stale", "nest")]


def test_room_cold(sessions):
    _readings(sessions, "Hallway", 18.0)  # 3.6°F below a 20°C setpoint for 2.5 hours
    _readings(sessions, "Great Room", 19.5)  # only 0.9°F below
    _readings(sessions, "Guest Bedroom", 18.0, eco="MANUAL_ECO")
    _readings(sessions, "Master Bedroom", 18.0, hours=1)  # not cold for long enough yet
    found = conditions(sessions, AlertSettings(), NOW)
    assert list(found) == [("room_cold", "Hallway")]
    assert "3.6°F below" in found["room_cold", "Hallway"]


async def test_job_sends_once_then_clears(cfg, sessions):
    sent = []

    async def fake_send(s, title, message):
        sent.append(title)

    save_settings(sessions, AlertSettings(pushover_app_token="t", pushover_user_key="u"))
    job = AlertsJob(cfg, sessions, set(), send=fake_send)
    insert_ignore(sessions, TankReading, [{"ts": NOW, "level_pct": 10.0}])
    assert await job.poll() == 1
    assert await job.poll() == 0  # still low: no repeat
    insert_ignore(sessions, TankReading, [{"ts": NOW + timedelta(hours=1), "level_pct": 80.0}])
    assert await job.poll() == 1
    assert sent == ["Heating alert", "Heating alert cleared"]
    with sessions() as db:
        assert db.query(Alert).one().cleared_ts is not None


async def test_job_without_pushover_still_records(cfg, sessions):
    async def fail(*a):
        raise AssertionError("should not send")

    insert_ignore(sessions, TankReading, [{"ts": NOW, "level_pct": 10.0}])
    assert await AlertsJob(cfg, sessions, set(), send=fail).poll() == 1


def test_popups_each_change_once(sessions):
    seen = Seen()
    with sessions.begin() as db:
        db.add(Alert(kind="room_cold", subject="Hallway", message="cold", started_ts=NOW))
    assert changes(sessions, seen) == [("negative", "cold")]
    assert changes(sessions, seen) == []
    with sessions.begin() as db:
        db.query(Alert).one().cleared_ts = NOW  # same second it started: timestamps don't matter
        db.add(Alert(kind="tank_low", subject="tank", message="low", started_ts=NOW))
    assert changes(sessions, seen) == [("positive", "Cleared: cold"), ("negative", "low")]
    assert changes(sessions, seen) == [] and seen.open_ids == {2}


def test_tank_clears_only_above_margin(sessions):
    insert_ignore(sessions, TankReading, [{"ts": NOW, "level_pct": 21.0}])
    s = AlertSettings(tank_low_pct=20)
    assert conditions(sessions, s, NOW) == {}
    assert ("tank_low", "tank") in conditions(sessions, s, NOW, open_keys={("tank_low", "tank")})


def test_source_that_never_worked(sessions):
    found = conditions(sessions, AlertSettings(), NOW, sources={"nest"}, started=NOW - timedelta(hours=7))
    assert "since the app started 7 hours ago" in found["source_stale", "nest"]
    assert conditions(sessions, AlertSettings(), NOW, sources={"nest"}, started=NOW - timedelta(hours=1)) == {}


def test_room_cold_allows_slow_polling(sessions):
    zid = _zone_id(sessions, "Hallway")
    insert_ignore(sessions, ThermostatReading, [  # every 15 minutes, first one 14 minutes into the window
        {"device_id": "h", "ts": NOW - timedelta(minutes=106 - 15 * i), "zone_id": zid, "ambient_c": 18.0,
         "setpoint_heat_c": 20.0, "mode": "HEAT", "eco_mode": "OFF"} for i in range(8)])
    assert conditions(sessions, AlertSettings(), NOW) == {}
    assert ("room_cold", "Hallway") in conditions(sessions, AlertSettings(), NOW, nest_s=900)


def test_settings_reject_bad_limits():
    for bad in ({"tank_low_pct": 120}, {"room_cold_hours": -2}, {"source_stale_hours": 0}, {"room_cold_f": None}):
        with pytest.raises(ValidationError):
            AlertSettings(**bad)


async def test_turning_alert_off_clears_quietly(cfg, sessions):
    sent = []

    async def fake_send(s, title, message):
        sent.append(title)

    save_settings(sessions, AlertSettings(pushover_app_token="t", pushover_user_key="u"))
    job = AlertsJob(cfg, sessions, set(), send=fake_send)
    insert_ignore(sessions, TankReading, [{"ts": NOW, "level_pct": 10.0}])
    await job.poll()
    save_settings(sessions, AlertSettings(pushover_app_token="t", pushover_user_key="u", tank_low_on=False))
    assert await job.poll() == 1
    assert sent == ["Heating alert"]
    with sessions() as db:
        assert db.query(Alert).one().cleared_ts is not None


async def test_failed_send_is_retried(cfg, sessions):
    sent, down = [], [True]

    async def flaky_send(s, title, message):
        if down[0]:
            raise httpx.ConnectError("no internet")
        sent.append(title)

    save_settings(sessions, AlertSettings(pushover_app_token="t", pushover_user_key="u"))
    job = AlertsJob(cfg, sessions, set(), send=flaky_send)
    insert_ignore(sessions, TankReading, [{"ts": NOW, "level_pct": 10.0}])
    await job.poll()
    assert sent == []
    down[0] = False
    assert await job.poll() == 0  # nothing new, but the start message goes out now
    assert sent == ["Heating alert"]
    await job.poll()
    assert sent == ["Heating alert"]


async def test_alerts_before_pushover_setup_are_not_sent_later(cfg, sessions):
    sent = []

    async def fake_send(s, title, message):
        sent.append(title)

    job = AlertsJob(cfg, sessions, set(), send=fake_send)
    insert_ignore(sessions, TankReading, [{"ts": NOW, "level_pct": 10.0}])
    await job.poll()
    save_settings(sessions, AlertSettings(pushover_app_token="t", pushover_user_key="u"))
    await job.poll()
    assert sent == []


async def test_pump_sensor_watched_only_when_on(cfg, sessions):
    from heatmon.sources.pumps import PumpSettings, save_settings as save_pumps
    job = AlertsJob(cfg, sessions, {"pumps"}, send=None)
    job.started = datetime.now(UTC) - timedelta(hours=8)
    assert await job.poll() == 0
    save_pumps(sessions, PumpSettings(enabled=True, address="10.0.0.5"))
    assert await job.poll() == 1
    with sessions() as db:
        assert db.query(Alert).one().subject == "pumps"
