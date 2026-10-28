import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from heatmon.db.models import (DailySummary, PropaneDelivery, TankReading, ThermostatReading,
                               WeatherObs, Zone, ZoneCall)
from heatmon.db.session import insert_ignore
from heatmon.processing.summaries import rebuild

TZ = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 15)  # must be in the past: rebuilds stop at today


def local(*args) -> datetime:
    return datetime(*args, tzinfo=TZ).astimezone(UTC)


def add_readings(sessions, zone: str, start: datetime, statuses: str):
    with sessions() as db:
        zone_id = db.scalar(select(Zone.id).where(Zone.name == zone))
    insert_ignore(sessions, ThermostatReading, [
        {"device_id": zone, "ts": start + timedelta(minutes=2 * i), "zone_id": zone_id,
         "ambient_c": 20.0, "setpoint_heat_c": 21.0, "hvac_status": "HEATING" if c == "H" else "OFF"}
        for i, c in enumerate(statuses)])


def setup_day(sessions):
    # Hallway calls 23:49 to 00:21 across midnight; Great Room 10:01 to 10:31.
    add_readings(sessions, "Hallway", local(2026, 10, 15, 23, 48), "." + "H" * 16 + ".")
    add_readings(sessions, "Great Room", local(2026, 10, 15, 10, 0), "." + "H" * 15 + ".")
    add_readings(sessions, "Hallway", local(2026, 10, 15, 10, 20), "." + "H" * 10 + ".")
    lo = local(2026, 10, 15, 0, 0)
    insert_ignore(sessions, WeatherObs, [
        {"source": "wu", "station_id": "KWACLEEL38", "ts": lo + timedelta(hours=i), "temp_f": 41.0}
        for i in range(24)])
    insert_ignore(sessions, WeatherObs, [
        {"source": "open-meteo", "station_id": "KWACLEEL38", "ts": lo + timedelta(hours=i),
         "temp_f": 50.0, "solar_wm2": 250.0} for i in range(1, 25)])
    insert_ignore(sessions, TankReading, [
        {"ts": local(2026, 10, 14, 22), "level_pct": 60.0, "tank_size_gal": 1000},
        {"ts": local(2026, 10, 16, 2), "level_pct": 58.0, "tank_size_gal": 1000},
        {"ts": local(2026, 10, 16, 10), "level_pct": 98.0, "tank_size_gal": 1000}])


def test_rebuild_builds_calls_fills_and_daily_rows(cfg, sessions):
    setup_day(sessions)
    rebuild(cfg, sessions, DAY)

    with sessions() as db:
        assert db.query(ZoneCall).count() == 3
        [fill] = db.scalars(select(PropaneDelivery)).all()
        day = db.get(DailySummary, DAY)
        next_day = db.get(DailySummary, DAY + timedelta(days=1))

    assert (fill.source, fill.gallons, fill.date) == ("detected", 400, date(2026, 10, 16))
    assert day.hdd == 24 and day.weather_hours == 24 and day.open_meteo_hours == 0
    assert day.solar_kwh_m2 == 6.0  # 24 hours x 250 W/m²
    assert json.loads(day.per_zone_call_min_json) == {"Great Room": 30, "Hallway": 31}
    assert json.loads(day.per_zone_call_count_json) == {"Great Room": 1, "Hallway": 2}
    # Great Room 10:01-10:31 and Hallway 10:21-10:41 overlap for 10 minutes.
    assert day.any_zone_call_min == 51 and day.total_zone_call_min == 61
    # 20 gallons from 22:00 the day before to 02:00 the day after. Only this day has HDD
    # (24), so its weight is 1.0 + 0.15 x 24 = 4.6; the 2-hour edges weigh 2/24 x 1.0 each.
    assert day.gal_used_est == round(20 * 4.6 / (4.6 + 2 * 2 / 24), 2)
    assert day.gal_per_hdd == round(day.gal_used_est / 24, 3)
    assert next_day.gal_used_est is None  # the fill happened that day
    assert json.loads(next_day.per_zone_call_min_json) == {"Hallway": 21}


def test_rebuild_is_repeatable_and_closes_open_calls(cfg, sessions):
    # Readings from the last few minutes, so the call is still in progress.
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=6)
    today = start.astimezone(TZ).date()
    add_readings(sessions, "Hallway", start, ".HH")
    rebuild(cfg, sessions, today)
    rebuild(cfg, sessions, today)
    with sessions() as db:
        [call] = db.scalars(select(ZoneCall)).all()
    assert call.open

    add_readings(sessions, "Hallway", start + timedelta(minutes=6), "H.")
    rebuild(cfg, sessions, today)
    with sessions() as db:
        [call] = db.scalars(select(ZoneCall)).all()
    assert not call.open and call.duration_s == 6 * 60  # first HEATING to first OFF, halfway points


def test_rebuild_with_no_data(cfg, sessions):
    assert rebuild(cfg, sessions) == 0


def test_call_longer_than_lookback_keeps_its_start(cfg, sessions):
    # Hallway heats without a break for 3 days, longer than the 2-day re-read window.
    add_readings(sessions, "Hallway", local(2026, 10, 12, 0, 0), "." + "H" * (3 * 720) + ".")
    rebuild(cfg, sessions, date(2026, 10, 12))
    rebuild(cfg, sessions, date(2026, 10, 15))  # like the hourly job, days later
    with sessions() as db:
        [call] = db.scalars(select(ZoneCall)).all()
    assert call.start_ts == local(2026, 10, 12, 0, 1) and call.duration_s == 3 * 86400


def test_window_edge_uses_hdd_of_the_day_before(cfg, sessions):
    """A tank interval from 18:00 on 10/14 to 06:00 on 10/15, both days equally cold.

    Rebuilding from 10/15 must split the gallons the same way a rebuild from 10/14 does.
    """
    lo = local(2026, 10, 14, 0, 0)
    insert_ignore(sessions, WeatherObs, [
        {"source": "wu", "station_id": "KWACLEEL38", "ts": lo + timedelta(hours=i), "temp_f": 35.0}
        for i in range(48)])
    insert_ignore(sessions, TankReading, [
        {"ts": local(2026, 10, 13, 23), "level_pct": 70.0, "tank_size_gal": 1000},
        {"ts": local(2026, 10, 14, 18), "level_pct": 69.0, "tank_size_gal": 1000},
        {"ts": local(2026, 10, 15, 6), "level_pct": 67.0, "tank_size_gal": 1000},
        {"ts": local(2026, 10, 16, 1), "level_pct": 66.0, "tank_size_gal": 1000}])

    def oct15():
        with sessions() as db:
            return db.get(DailySummary, date(2026, 10, 15)).gal_used_est

    rebuild(cfg, sessions, date(2026, 10, 14))
    full = oct15()
    rebuild(cfg, sessions, date(2026, 10, 15))
    assert oct15() == full
