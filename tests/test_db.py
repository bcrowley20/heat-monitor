from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from heatmon.config import Zone as ZoneCfg
from heatmon.db.models import SourceStatus, WeatherObs, Zone
from heatmon.db.session import init_db, insert_ignore, record_status, sync_zones


def test_migration_creates_tables_and_is_rerunnable(cfg, sessions):
    init_db(cfg.db_path)  # second run is a no-op
    with sessions() as db:
        names = set(db.scalars(text("SELECT name FROM sqlite_master WHERE type='table'")))
        mode = db.scalar(text("PRAGMA journal_mode"))
    assert {"zones", "thermostat_readings", "weather_obs", "tank_readings", "source_status"} <= names
    assert mode == "wal"


def test_timestamps_stored_as_utc_iso_text(sessions):
    pacific = timezone(timedelta(hours=-7))
    row = {"source": "wu", "station_id": "S", "ts": datetime(2026, 10, 28, 6, 5, tzinfo=pacific)}
    insert_ignore(sessions, WeatherObs, [row])
    with sessions() as db:
        assert db.scalar(text("SELECT ts FROM weather_obs")) == "2026-10-28T13:05:00+00:00"
        assert db.scalar(select(WeatherObs.ts)) == datetime(2026, 10, 28, 13, 5, tzinfo=UTC)


def test_naive_timestamp_rejected(sessions):
    with pytest.raises(Exception, match="naive"):
        insert_ignore(sessions, WeatherObs, [{"source": "wu", "station_id": "S", "ts": datetime(2026, 1, 1)}])


def test_insert_ignore_skips_duplicates(sessions):
    ts = datetime(2026, 10, 28, tzinfo=UTC)
    rows = [{"source": "wu", "station_id": "S", "ts": ts + timedelta(minutes=i)} for i in range(3)]
    assert insert_ignore(sessions, WeatherObs, rows) == 3
    assert insert_ignore(sessions, WeatherObs, rows + [{**rows[0], "ts": ts + timedelta(hours=1)}]) == 1


def test_sync_zones_updates_and_keeps_removed(sessions):
    sync_zones(sessions, [ZoneCfg(name="Hallway", loops=3, exposure="S")])
    with sessions() as db:
        zones = {z.name: z for z in db.scalars(select(Zone))}
    assert len(zones) == 4
    assert zones["Hallway"].loops == 3 and zones["Hallway"].exposure == "S"


def test_record_status(sessions):
    record_status(sessions, "nest")
    record_status(sessions, "nest", error="boom")
    with sessions() as db:
        st = db.get(SourceStatus, "nest")
    assert st.last_success_ts and st.last_error_ts and st.last_error_msg == "boom"
