import httpx
import pytest
import respx
from sqlalchemy import select

from heatmon.db.models import TankReading
from heatmon.sources.centriconnect import CentriConnectSource

URL = "https://api.centriconnect.com/centriconnect/test-cc-user/device/test-cc-device/all-data"


def tank_response(level: float, post_time: str) -> httpx.Response:
    return httpx.Response(200, json={"test-cc-device": {
        "AlertStatus": "No Alert", "Altitude": 584.0, "BatteryVolts": 3.95,
        "DeviceID": "test-cc-device", "DeviceName": "Tank", "DeviceTempFahrenheit": 41.0,
        "LastPostTimeIso": post_time, "Latitude": 47.2, "Longitude": -120.94,
        "NextPostTimeIso": "2026-10-28 20:00:00.000", "SignalQualLTE": -95.0, "SolarVolts": 1.3,
        "TankLevel": level, "TankSize": 1000, "TankSizeUnit": "Gallons",
        "VersionHW": "1", "VersionLTE": "1",
    }})


@respx.mock
async def test_stores_only_changed_readings(cfg, sessions):
    route = respx.get(URL).mock(side_effect=[
        tank_response(62.0, "2026-10-28 08:00:00.123"),
        tank_response(62.0, "2026-10-28 08:00:00.123"),  # same report fetched again
        tank_response(61.0, "2026-10-28 16:00:00.000"),
    ])
    src = CentriConnectSource(cfg, sessions)
    assert [await src.poll() for _ in range(3)] == [1, 0, 1]
    assert route.calls.last.request.url.params["device_auth"] == "test-cc-auth"

    with sessions() as db:
        rows = list(db.scalars(select(TankReading).order_by(TankReading.ts)))
    assert [r.level_pct for r in rows] == [62.0, 61.0]
    assert rows[0].ts.isoformat() == "2026-10-28T08:00:00+00:00"
    assert rows[0].remaining_gal == 620.0 and rows[0].tank_size_gal == 1000
    assert rows[0].battery_pct == 90.0
    assert "test-cc-device" not in rows[0].raw_json


@respx.mock
async def test_error_response(cfg, sessions):
    respx.get(URL).respond(json={"error": "NotFound"})
    with pytest.raises(RuntimeError, match="NotFound"):
        await CentriConnectSource(cfg, sessions).poll()
