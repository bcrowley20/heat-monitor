from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from heatmon.db.models import PumpEvent
from heatmon.db.session import insert_ignore, read_setting
from heatmon.processing.pumps import Run, daily_pumps, derive_runs, gallons
from heatmon.sources.pumps import PiReply, PumpLink, PumpSettings, PumpSource, base_url, save_settings
from heatmon.web import data

URL = "http://10.0.0.5:8081/events"
NOW = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)


def reply(boot="b1", mono_now=1000.0, events=(), pumps=None, first_seq=1, more=False):
    events = [{"seq": s, "pump": p, "on": on, "mono": m} for s, p, on, m in events]
    pumps = pumps or {"boiler": (False, 0.0), "system": (False, 0.0), "dhw": (False, 0.0)}
    return {"boot_id": boot, "mono_now": mono_now, "first_seq": first_seq, "more": more, "events": events,
            "last_seq": events[-1]["seq"] if events else first_seq - 1,
            "pumps": {n: {"on": on, "since_mono": s} for n, (on, s) in pumps.items()}}


def stored(sessions):
    with sessions() as db:
        return [(e.pump, e.on, e.gap, e.ts) for e in db.scalars(select(PumpEvent).order_by(PumpEvent.ts, PumpEvent.id))]


@pytest.fixture
def source(cfg, sessions):
    save_settings(sessions, PumpSettings(enabled=True, address="10.0.0.5"))
    return PumpSource(cfg, sessions)


def test_base_url():
    assert base_url("10.0.0.5") == "http://10.0.0.5:8081"
    assert base_url(" http://pi.local:9000/ ") == "http://pi.local:9000"
    with pytest.raises(ValueError):
        base_url("  ")


async def test_turned_off_does_nothing(cfg, sessions):
    assert await PumpSource(cfg, sessions).poll() is None


@respx.mock
async def test_switch_times_come_from_the_pi_monotonic_clock(source, sessions):
    route = respx.get(URL).respond(json=reply(mono_now=1000.0, events=[(1, "dhw", False, 0.0), (2, "dhw", True, 940.0)],
                                              pumps={"dhw": (True, 940.0)}))
    before = datetime.now(UTC)
    assert await source.poll() == 2
    on = stored(sessions)[-1]
    assert on[:3] == ("dhw", True, False)
    assert timedelta(seconds=58) < before - on[3] < timedelta(seconds=62)  # 60 s before the reply
    assert route.calls.last.request.url.params["after"] == "0"
    await source.poll()
    assert route.calls.last.request.url.params["after"] == "2"
    assert route.calls.last.request.url.params["boot"] == "b1"


@respx.mock
async def test_pages_are_followed(source, sessions):
    respx.get(URL, params={"after": "0"}).respond(json=reply(events=[(1, "dhw", True, 10.0)], more=True,
                                                             pumps={"dhw": (False, 20.0)}))
    respx.get(URL, params={"after": "1"}).respond(json=reply(events=[(2, "dhw", False, 20.0)],
                                                             pumps={"dhw": (False, 20.0)}))
    assert await source.poll() == 2
    assert [e[:3] for e in stored(sessions)] == [("dhw", True, False), ("dhw", False, False)]


def test_restart_closes_running_pumps_at_last_contact(source, sessions):
    t0 = NOW - timedelta(hours=1)
    source.store(PiReply.model_validate(reply(events=[(1, "system", True, 500.0)], mono_now=500.0,
                                 pumps={"system": (True, 500.0)})), link := PumpLink(), t0)
    # The Pi restarted and the system pump is running again since 30 s after its boot.
    source.store(PiReply.model_validate(reply(boot="b2", events=[(1, "system", False, 1.0)], mono_now=100.0,
                                 pumps={"system": (True, 31.0)})), link, NOW)
    s = stored(sessions)
    assert [e[:3] for e in s] == [("system", True, False), ("system", False, True), ("system", False, False),
                                  ("system", True, True)]
    assert s[1][3] == t0  # stopped when last heard from
    assert link.boot_id == "b2" and link.last_contact == NOW


def test_falling_behind_marks_a_gap(source, sessions):
    link = PumpLink(boot_id="b1", last_seq=3, last_contact=NOW - timedelta(minutes=10))
    insert_ignore(sessions, PumpEvent, [{"boot_id": "b1", "seq": 3, "pump": "dhw", "ts": NOW - timedelta(minutes=20),
                                         "on": True, "gap": False}])
    source.store(PiReply.model_validate(reply(first_seq=10, events=[(10, "boiler", True, 990.0)], mono_now=1000.0,
                                 pumps={"boiler": (True, 990.0), "dhw": (False, 900.0)})), link, NOW)
    assert [e[:3] for e in stored(sessions)] == [("dhw", True, False), ("dhw", False, True), ("boiler", True, False)]


@respx.mock
async def test_errors_raise_without_storing(source, sessions):
    respx.get(URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(httpx.ConnectTimeout):
        await source.poll()
    respx.get(URL).respond(json={"hello": "world"})
    with pytest.raises(RuntimeError, match="not with pump sensor data"):
        await source.poll()
    respx.get(URL).respond(500)
    with pytest.raises(httpx.HTTPStatusError):
        await source.poll()
    assert stored(sessions) == [] and read_setting(sessions, "pump_link", PumpLink).last_contact is None


# ---------- runs and hot water ----------

def test_derive_runs():
    t = NOW
    events = [("dhw", t, True, False), ("system", t, True, False), ("dhw", t + timedelta(minutes=12), False, False),
              ("dhw", t + timedelta(minutes=12), False, False), ("system", t + timedelta(minutes=30), False, True)]
    runs = derive_runs(events + [("boiler", t + timedelta(minutes=50), True, False)], t + timedelta(hours=1))
    assert [(r.pump, r.minutes, r.open, r.gap) for r in runs] == [
        ("dhw", 12, False, False), ("system", 30, False, True), ("boiler", 10, True, False)]


def test_gallons_at_full_fire():
    assert gallons(60, 91_500) == 1
    assert round(gallons(20, 110_000), 3) == 0.401


def test_daily_pumps_split_at_midnight(cfg):
    tz = data.ZoneInfo(cfg.timezone)
    start = datetime(2026, 11, 2, 23, 50, tzinfo=tz)
    runs = [Run("dhw", start, start + timedelta(minutes=20)), Run("boiler", start, start + timedelta(minutes=20))]
    days = daily_pumps(runs, [date(2026, 11, 2), date(2026, 11, 3)], tz, 91_500, 2.0)
    assert [(d["dhw_min"], d["dhw_cycles"], d["boiler_pump_min"]) for d in days] == [(10, 1, 10), (10, 0, 10)]
    assert days[0]["dhw_gal_est"] == round(10 / 60, 2) and days[0]["dhw_cost_est"] == round(10 / 60 * 2, 2)


def test_runs_include_one_already_running_at_range_start(cfg, sessions):
    tz = data.ZoneInfo(cfg.timezone)
    on = datetime(2026, 11, 1, 23, 0, tzinfo=tz)
    insert_ignore(sessions, PumpEvent, [
        {"boot_id": "b", "seq": 1, "pump": "system", "ts": on, "on": True, "gap": False},
        {"boot_id": "b", "seq": 2, "pump": "system", "ts": on + timedelta(hours=2), "on": False, "gap": False},
        {"boot_id": "b", "seq": 3, "pump": "dhw", "ts": on - timedelta(days=3), "on": False, "gap": False}])
    c = data.Ctx(cfg, sessions, date(2026, 11, 2), date(2026, 11, 2))
    assert [(r["pump"], r["minutes"]) for r in data.pump_runs(c)] == [("System pump", 120)]
    day = data.hot_water_daily(c)
    assert len(day) == 1 and day[0]["system_pump_min"] == 60 and day[0]["dhw_cycles"] == 0


def test_pump_on_through_a_gap_stays_on_and_rows_dont_pile_up(source, sessions):
    link = PumpLink(boot_id="b1", last_seq=3, last_contact=NOW - timedelta(minutes=10))
    insert_ignore(sessions, PumpEvent, [{"boot_id": "b1", "seq": 3, "pump": "system", "ts": NOW - timedelta(hours=1),
                                         "on": True, "gap": False}])
    # Fell behind, but the Pi says the system pump has been on for 50 minutes.
    r = reply(first_seq=10, events=[(10, "dhw", True, 990.0)], mono_now=1000.0,
              pumps={"system": (True, -2000.0), "dhw": (True, 990.0)})
    source.store(PiReply.model_validate(r), link, NOW)
    source.store(PiReply.model_validate(reply(first_seq=10, events=[], mono_now=1005.0,
                                              pumps={"system": (True, -2000.0), "dhw": (True, 990.0)})),
                 link, NOW + timedelta(seconds=5))
    assert [e[:3] for e in stored(sessions)] == [("system", True, False), ("dhw", True, False)]


def test_current_state_never_lands_before_a_stored_row(source, sessions):
    link = PumpLink(boot_id="b1", last_seq=3, last_contact=NOW - timedelta(minutes=10))
    insert_ignore(sessions, PumpEvent, [{"boot_id": "b1", "seq": 3, "pump": "system", "ts": NOW - timedelta(minutes=5),
                                         "on": False, "gap": False}])
    r = reply(first_seq=4, events=[], mono_now=1000.0, pumps={"system": (True, 0.0)})  # "on" since 1000 s ago
    for _ in range(3):
        source.store(PiReply.model_validate(r), link, NOW)
    s = stored(sessions)
    assert len(s) == 2 and s[-1][:3] == ("system", True, True) and s[-1][3] == NOW - timedelta(minutes=5)


def test_run_ending_after_the_range_is_not_open(cfg, sessions):
    tz = data.ZoneInfo(cfg.timezone)
    on = datetime(2026, 11, 2, 23, 58, tzinfo=tz)
    insert_ignore(sessions, PumpEvent, [
        {"boot_id": "b", "seq": 1, "pump": "dhw", "ts": on, "on": True, "gap": False},
        {"boot_id": "b", "seq": 2, "pump": "dhw", "ts": on + timedelta(minutes=12), "on": False, "gap": False}])
    c = data.Ctx(cfg, sessions, date(2026, 11, 2), date(2026, 11, 2))
    assert [(r["minutes"], r["note"]) for r in data.pump_runs(c)] == [(12, "")]
    assert data.hot_water_daily(c)[0]["dhw_min"] == 2
    assert data.hot_water_daily(data.Ctx(cfg, sessions, date(2026, 10, 1), date(2026, 10, 5))) == []
    assert data.has_pump_data(c)


def test_guessed_start_is_flagged():
    runs = derive_runs([("dhw", NOW, True, True), ("dhw", NOW + timedelta(minutes=5), False, False)], NOW)
    assert runs[0].gap


def test_pumps_now_only_while_on_and_marks_stale(cfg, sessions):
    from heatmon.db.session import write_setting
    insert_ignore(sessions, PumpEvent, [{"boot_id": "b", "seq": 1, "pump": "dhw", "ts": NOW, "on": True, "gap": False}])
    c = data.Ctx(cfg, sessions, date(2026, 11, 2), date(2026, 11, 2))
    assert data.pumps_now(c) == []
    save_settings(sessions, PumpSettings(enabled=True, address="10.0.0.5"))
    old = datetime.now(UTC) - timedelta(minutes=10)
    write_setting(sessions, "pump_link", PumpLink(boot_id="b", last_seq=1, last_contact=old))
    assert [(p["pump"], p["on"], p["current"]) for p in data.pumps_now(c)] == [("DHW pump", True, False)]
    write_setting(sessions, "pump_link", PumpLink(boot_id="b", last_seq=1, last_contact=datetime.now(UTC)))
    assert data.pumps_now(c)[0]["current"]
