from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from heatmon.db.models import ThermostatReading, WeatherObs, Zone, ZoneCall
from heatmon.db.session import insert_ignore
from heatmon.web import api, data
from heatmon.web.components import date_range
from heatmon.web.pages import _bins

TZ = ZoneInfo("America/Los_Angeles")


def local(*args) -> datetime:
    return datetime(*args, tzinfo=TZ).astimezone(UTC)


def ctx(cfg, sessions, start=date(2026, 10, 15), end=date(2026, 10, 15)):
    return data.Ctx(cfg, sessions, start, end)


def add_call(sessions, zone_id, start, end, open_=False):
    with sessions.begin() as db:
        db.add(ZoneCall(zone_id=zone_id, start_ts=start, end_ts=end, duration_s=int((end - start).total_seconds()),
                        setpoint_c=20.0, open=open_, gap_closed=False))


def test_overshoot_stops_at_next_call(cfg, sessions):
    with sessions() as db:
        hall = db.scalar(select(Zone.id).where(Zone.name == "Hallway"))
    add_call(sessions, hall, local(2026, 10, 15, 10, 0), local(2026, 10, 15, 11, 0))
    add_call(sessions, hall, local(2026, 10, 15, 12, 0), local(2026, 10, 15, 12, 30), open_=True)
    insert_ignore(sessions, ThermostatReading, [
        {"device_id": "d", "zone_id": hall, "ts": local(2026, 10, 15, 11, 30), "ambient_c": 21.0, "setpoint_heat_c": 20.0},
        # after the next call starts: not counted for the first call
        {"device_id": "d", "zone_id": hall, "ts": local(2026, 10, 15, 12, 10), "ambient_c": 23.0, "setpoint_heat_c": 20.0},
    ])
    first, second = data.zone_calls(ctx(cfg, sessions))
    assert first["overshoot_f"] == 1.8 and first["minutes"] == 60 and first["start"] == "2026-10-15 10:00"
    assert second["overshoot_f"] is None and second["note"] == "in progress"


def test_settings_periods_from_seeded_events(cfg, sessions):
    c = ctx(cfg, sessions)
    periods = data.settings_periods(c)
    assert [p.label for p in periods] == ["Before Oct 25, 2026", "From Oct 25, 2026"]  # three changes, one day
    assert data.period_of_day(date(2026, 10, 25), periods, TZ) == "Before Oct 25, 2026"  # changed that evening
    assert data.period_of_day(date(2026, 10, 26), periods, TZ) == "From Oct 25, 2026"


def test_events_shown_in_local_time(cfg, sessions):
    evs = data.events(ctx(cfg, sessions, date(2026, 10, 25), date(2026, 10, 26)))
    assert [e["time"] for e in evs] == ["2026-10-25 20:30", "2026-10-25 21:35", "2026-10-25 21:55",
                                        "2026-10-26 14:00"]


def test_fit_line():
    fit = data.fit_line([10, 20, 30], [3, 5, 7])
    assert fit.slope == pytest.approx(0.2) and fit.intercept == pytest.approx(1) and fit.r2 == pytest.approx(1)
    assert data.fit_line([10, 10, 10], [1, 2, 3]) is None and data.fit_line([1, 2], [1, 2]) is None


def test_bins_never_mix_periods():
    days = [{"label": f"d{i}", "days": 1, "hdd": i, "y": 2 * i} for i in range(10)]
    out = _bins({"A": days, "B": days[:3]})
    assert [(b["days"], b["hdd"]) for b in out["A"]] == [(7, 3.0)]  # the last 3 days are too few
    assert out["B"] == []


def test_weather_hourly_prefers_station_and_shifts_solar(cfg, sessions):
    h = local(2026, 10, 15, 12)
    insert_ignore(sessions, WeatherObs, [
        {"source": "wu", "station_id": "KWACLEEL38", "ts": h, "temp_f": 40.0, "wind_mph": 2.0, "gust_mph": 5.0},
        {"source": "wu", "station_id": "KWACLEEL38", "ts": h + timedelta(minutes=30), "temp_f": 42.0,
         "wind_mph": 4.0, "gust_mph": 7.0}])
    insert_ignore(sessions, WeatherObs, [
        {"source": "open-meteo", "station_id": "KWACLEEL38", "ts": h + timedelta(hours=i), "temp_f": 50.0,
         "solar_wm2": 100.0 * i} for i in (0, 1, 2)])
    rows = {r["time"]: r for r in data.weather_hourly(ctx(cfg, sessions))}
    noon, one = rows["2026-10-15 12:00"], rows["2026-10-15 13:00"]
    assert (noon["temp_f"], noon["temp_source"], noon["wind_mph"], noon["gust_mph"]) == (41.0, "wu", 3.0, 7.0)
    assert noon["solar_wm2"] == 100.0  # Open-Meteo's 13:00 value covers 12:00 to 13:00
    assert (one["temp_f"], one["temp_source"]) == (50.0, "open-meteo")


def test_api_json_csv_and_unknown(cfg, sessions):
    app = FastAPI()
    app.include_router(api.router(cfg, sessions))
    client = TestClient(app)
    r = client.get("/api/events", params={"start": "2026-10-25", "end": "2026-10-26"})
    assert r.status_code == 200 and len(r.json()) == 4
    r = client.get("/api/events.csv", params={"start": "2026-10-25", "end": "2026-10-26"})
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0] == "time,type,title,details" and len(r.text.splitlines()) == 5
    assert client.get("/api/nope").status_code == 404


def test_overshoot_window_stops_at_a_call_after_the_range(cfg, sessions):
    with sessions() as db:
        hall = db.scalar(select(Zone.id).where(Zone.name == "Hallway"))
    add_call(sessions, hall, local(2026, 10, 15, 22, 0), local(2026, 10, 15, 23, 0))
    add_call(sessions, hall, local(2026, 10, 16, 0, 30), local(2026, 10, 16, 1, 0))  # next day
    insert_ignore(sessions, ThermostatReading, [
        {"device_id": "d", "zone_id": hall, "ts": local(2026, 10, 15, 23, 30), "ambient_c": 20.5, "setpoint_heat_c": 20.0},
        {"device_id": "d", "zone_id": hall, "ts": local(2026, 10, 16, 1, 30), "ambient_c": 22.0, "setpoint_heat_c": 20.0},
    ])
    [call] = data.zone_calls(ctx(cfg, sessions))  # range is Oct 15 only
    assert call["overshoot_f"] == 0.9  # only the 23:30 reading; 01:30 belongs to the next call


def test_latest_uses_newest_reading_when_a_zone_had_two_thermostats(cfg, sessions):
    with sessions() as db:
        hall = db.scalar(select(Zone.id).where(Zone.name == "Hallway"))
    insert_ignore(sessions, ThermostatReading, [
        {"device_id": "new", "zone_id": hall, "ts": local(2026, 10, 15, 12), "ambient_c": 20.0},
        {"device_id": "old", "zone_id": hall, "ts": local(2026, 10, 1, 12), "ambient_c": 10.0}])
    [z] = data.latest(ctx(cfg, sessions))["zones"]
    assert z["temp_f"] == 68.0


def test_date_range_caps_future_and_ignores_bad_input(cfg):
    today = datetime.now(TZ).date()
    assert date_range(cfg, "2026-10-01", "9999-12-31") == (date(2026, 10, 1), today)
    assert date_range(cfg, "junk", None) == (today - timedelta(days=13), today)
    assert date_range(cfg, "2026-10-10", "2026-10-01") == (date(2026, 10, 1), date(2026, 10, 10))


def test_small_changes_read_as_about_the_same():
    assert data._pct(29.71, 29.7) == "about the same" and data._pct(27, 30) == "10% lower"


def test_compare_summary_and_confidence():
    a = {"mean_temp_f": 40.0, "hdd": 250.0, "gal_per_hdd": 0.2, "vs_baseline_pct": None, "gallons": 50.0,
         "baseline_gallons": None, "call_min_per_hdd": 20.0, "days_with_propane": 10, "days": 30,
         "days_with_thermostats": 30, "hdd_range": 5, "zone_min_per_hdd": 40.0, "estimated_days": 0}
    b = {**a, "mean_temp_f": 35.0, "hdd": 300.0, "gal_per_hdd": 0.17, "vs_baseline_pct": -12.0,
         "gallons": 51.0, "baseline_gallons": 58.0, "call_min_per_hdd": 22.0, "zone_min_per_hdd": 36.0,
         "estimated_days": 30}
    text = " ".join(data.compare_summary(a, b, "Before", "After"))
    assert "Propane per degree day was 15% lower in After" in text
    assert "After was 12% below it" in text and "all zones added up, was 10% lower in After" in text
    assert "any zone calling per degree day was 10% higher in After (estimated" in text
    assert text.count("Low confidence") == 1 and "for propane, one period has only 10 days" in text
    assert "calling time, one" not in text and "barely varied" in text  # 30 thermostat days is enough
