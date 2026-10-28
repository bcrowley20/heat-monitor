import json
import stat
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select

from heatmon.config import Zone as ZoneCfg
from heatmon.db.models import ThermostatReading, Zone
from heatmon.db.session import sync_zones
from heatmon.sources import nest
from tests.conftest import load_fixture

DEVICES_URL = f"{nest.SDM_URL}/enterprises/test-project/devices"
TS = datetime(2026, 10, 28, 13, 0, tzinfo=UTC)


def zones_by_id(sessions):
    with sessions() as db:
        return list(db.scalars(select(Zone)))


def test_parse_devices(sessions):
    zones = zones_by_id(sessions)
    names = {z.id: z.name for z in zones}
    rows = nest.parse_devices(load_fixture("nest_devices.json"), TS, zones)
    by_dev = {r["device_id"]: r for r in rows}

    assert len(rows) == 4  # the camera is skipped
    assert all("test-project" not in r["device_id"] for r in rows)
    hall = next(r for r in rows if r["device_id"].startswith("AVPH"))
    assert names[hall["zone_id"]] == "Hallway"  # matched by room name
    assert hall["hvac_status"] == "HEATING" and hall["ambient_c"] == 19.9
    assert names[by_dev["DEV-GREAT"]["zone_id"]] == "Great Room"  # matched by custom name
    assert by_dev["DEV-MASTER"]["setpoint_heat_c"] == 15.5  # eco setpoint in effect
    assert by_dev["DEV-MYSTERY"]["zone_id"] is None
    assert by_dev["DEV-MYSTERY"]["connectivity"] == "OFFLINE"


def test_device_id_in_config_wins(cfg, sessions):
    sync_zones(sessions, [ZoneCfg(name="Guest Bedroom", nest_device_id="DEV-MYSTERY")])
    zones = zones_by_id(sessions)
    rows = nest.parse_devices(load_fixture("nest_devices.json"), TS, zones)
    mystery = next(r for r in rows if r["device_id"] == "DEV-MYSTERY")
    assert {z.id: z.name for z in zones}[mystery["zone_id"]] == "Guest Bedroom"


def test_auth_url(cfg):
    url = nest.auth_url("test-project", "test-client-id")
    assert url.startswith("https://nestservices.google.com/partnerconnections/test-project/auth?")
    assert "client_id=test-client-id" in url and "access_type=offline" in url


@respx.mock
def test_exchange_code_saves_private_token(cfg):
    route = respx.post(nest.TOKEN_URL).respond(json={"access_token": "a", "refresh_token": "r1"})
    nest.exchange_code(cfg, "the-code")
    assert b"code=the-code" in route.calls.last.request.content
    assert json.loads(cfg.token_path.read_text()) == {"refresh_token": "r1"}
    assert stat.S_IMODE(cfg.token_path.stat().st_mode) == 0o600


@respx.mock
async def test_poll_refreshes_token_once_and_stores(cfg, sessions):
    nest.save_refresh_token(cfg.token_path, "r1")
    token = respx.post(nest.TOKEN_URL).respond(json={"access_token": "a1", "expires_in": 3599})
    devices = respx.get(DEVICES_URL).respond(json=load_fixture("nest_devices.json"))
    src = nest.NestSource(cfg, sessions)

    assert await src.poll() == 4
    await src.poll()
    assert token.call_count == 1
    assert devices.calls.last.request.headers["Authorization"] == "Bearer a1"
    with sessions() as db:
        assert db.query(ThermostatReading).count() >= 4


@respx.mock
async def test_poll_401_forces_token_refresh(cfg, sessions):
    nest.save_refresh_token(cfg.token_path, "r1")
    token = respx.post(nest.TOKEN_URL).respond(json={"access_token": "a1", "expires_in": 3599})
    respx.get(DEVICES_URL).mock(side_effect=[
        httpx.Response(401), httpx.Response(200, json=load_fixture("nest_devices.json"))])
    src = nest.NestSource(cfg, sessions)
    with pytest.raises(httpx.HTTPStatusError):
        await src.poll()
    await src.poll()
    assert token.call_count == 2


async def test_poll_without_token_explains(cfg, sessions):
    with pytest.raises(RuntimeError, match="heatmon auth nest"):
        await nest.NestSource(cfg, sessions).poll()


def test_unmatched_thermostat_warned_once(sessions, caplog, monkeypatch):
    monkeypatch.setattr(nest, "_warned_unmatched", set())
    zones = zones_by_id(sessions)
    for _ in range(3):
        nest.parse_devices(load_fixture("nest_devices.json"), TS, zones)
    assert sum("DEV-MYSTERY" in r.message for r in caplog.records) == 1
