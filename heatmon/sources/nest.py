"""Nest thermostats through the Google Smart Device Management (SDM) API."""

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import ThermostatReading, Zone
from heatmon.db.session import insert_ignore

log = logging.getLogger(__name__)

SDM_URL = "https://smartdevicemanagement.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/sdm.service"
# Google's Device Access guide uses this redirect. After consent the browser lands on
# google.com with ?code=... in the address bar, which works even when the app runs headless.
REDIRECT_URI = "https://www.google.com"
T = "sdm.devices.traits."


def auth_url(project_id: str, client_id: str) -> str:
    query = urlencode({
        "redirect_uri": REDIRECT_URI,
        "access_type": "offline",
        "prompt": "consent",
        "client_id": client_id,
        "response_type": "code",
        "scope": SCOPE,
    })
    return f"https://nestservices.google.com/partnerconnections/{project_id}/auth?{query}"


def exchange_code(cfg: Config, code: str) -> None:
    """Trade a one-time consent code for a refresh token and save it."""
    s = cfg.secrets
    r = httpx.post(TOKEN_URL, data={
        "client_id": s.google_client_id,
        "client_secret": s.google_client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    })
    r.raise_for_status()
    save_refresh_token(cfg.token_path, r.json()["refresh_token"])


def save_refresh_token(path: Path, token: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"refresh_token": token}, f)
    os.chmod(path, 0o600)  # in case the file already existed with wider permissions


def parse_devices(data: dict, ts: datetime, zones: list[Zone]) -> list[dict]:
    rows = []
    for dev in data.get("devices", []):
        if dev.get("type") != "sdm.devices.types.THERMOSTAT":
            continue
        tr = dev.get("traits", {})
        # Keep only the trailing device id; the full name also contains the project id.
        device_id = dev["name"].rsplit("/", 1)[-1]
        names = {tr.get(T + "Info", {}).get("customName", "")}
        names |= {p.get("displayName", "") for p in dev.get("parentRelations", [])}
        zone = _match_zone(device_id, names, zones)
        if zone is None:
            log.warning("Thermostat %s (%s) matches no zone in config.yaml", device_id, names)
        eco = tr.get(T + "ThermostatEco", {})
        # In eco mode the normal setpoint is absent and the eco setpoint is in effect.
        if eco.get("mode") == "MANUAL_ECO":
            setpoint = eco.get("heatCelsius")
        else:
            setpoint = tr.get(T + "ThermostatTemperatureSetpoint", {}).get("heatCelsius")
        rows.append({
            "device_id": device_id,
            "ts": ts,
            "zone_id": zone.id if zone else None,
            "ambient_c": tr.get(T + "Temperature", {}).get("ambientTemperatureCelsius"),
            "setpoint_heat_c": setpoint,
            "humidity_pct": tr.get(T + "Humidity", {}).get("ambientHumidityPercent"),
            "mode": tr.get(T + "ThermostatMode", {}).get("mode"),
            "eco_mode": eco.get("mode"),
            "hvac_status": tr.get(T + "ThermostatHvac", {}).get("status"),
            "connectivity": tr.get(T + "Connectivity", {}).get("status"),
        })
    return rows


def _match_zone(device_id: str, names: set[str], zones: list[Zone]) -> Zone | None:
    lowered = {n.strip().lower() for n in names if n}
    for z in zones:
        if z.nest_device_id == device_id:
            return z
    for z in zones:
        if z.name.lower() in lowered:
            return z
    return None


class NestSource:
    name = "nest"

    def __init__(self, cfg: Config, sessions: sessionmaker[Session]):
        self.cfg = cfg
        self.sessions = sessions
        self.interval_s = cfg.intervals.nest_s
        self.client = httpx.AsyncClient(timeout=30)
        self._token: str | None = None
        self._token_expires = 0.0

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires:
            return self._token
        if not self.cfg.token_path.exists():
            raise RuntimeError("No Nest token yet. Run `heatmon auth nest` first.")
        refresh = json.loads(self.cfg.token_path.read_text())["refresh_token"]
        s = self.cfg.secrets
        r = await self.client.post(TOKEN_URL, data={
            "client_id": s.google_client_id,
            "client_secret": s.google_client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        })
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = time.monotonic() + body.get("expires_in", 3600) - 60
        return self._token

    async def poll(self) -> int:
        token = await self._access_token()
        url = f"{SDM_URL}/enterprises/{self.cfg.secrets.sdm_project_id}/devices"
        r = await self.client.get(url, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401:
            self._token = None  # force a refresh next time
        r.raise_for_status()
        with self.sessions() as db:
            zones = list(db.scalars(select(Zone)))
        rows = parse_devices(r.json(), datetime.now(UTC).replace(microsecond=0), zones)
        return insert_ignore(self.sessions, ThermostatReading, rows)
