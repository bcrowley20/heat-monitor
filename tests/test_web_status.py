from datetime import UTC, datetime

from heatmon.db.models import ThermostatReading
from heatmon.db.session import insert_ignore, record_status
from heatmon.sources.nest import parse_devices
from heatmon.web.status import status_data
from tests.conftest import load_fixture
from tests.test_sources_nest import zones_by_id


def test_status_data(cfg, sessions):
    zones = zones_by_id(sessions)
    fixture = load_fixture("nest_devices.json")
    for minute in (0, 2):
        ts = datetime(2026, 10, 28, 13, minute, tzinfo=UTC)
        insert_ignore(sessions, ThermostatReading, parse_devices(fixture, ts, zones))
    record_status(sessions, "nest")

    d = status_data(cfg, sessions, active={"nest"})

    assert [s["collecting"] for s in d["sources"]] == ["yes", "no", "no"]
    assert d["sources"][0]["every_min"] == 2 and d["sources"][0]["last_success"]
    assert len(d["thermostats"]) == 4  # latest reading per thermostat only
    hall = next(t for t in d["thermostats"] if t["zone"] == "Hallway")
    assert hall["time"] == "2026-10-28 06:02"  # shown in Pacific time
    assert hall["temp_f"] == 67.8 and hall["setpoint_f"] == 68.9
    assert {"table": "thermostat_readings", "rows": 8} in d["counts"]
