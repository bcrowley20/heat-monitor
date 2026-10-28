"""Propane tank level through CentriConnect / MyPropane.

Calls the same endpoint, and reads the same fields, as the aiocentriconnect library
used by Home Assistant. That library is not used directly: it is missing its own
dependency list and has a 1 second timeout.
"""

import json
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import TankReading
from heatmon.logs import scrub

API_URL = "https://api.centriconnect.com/centriconnect/{user_id}/device/{device_id}/all-data"


def tank_row(data: dict) -> TankReading:
    level, size, volts = data["TankLevel"], data.get("TankSize"), data.get("BatteryVolts")
    # Battery percent as aiocentriconnect estimates it: 3.5 V or less is 0%, 4.0 V or more is 100%.
    battery = None if volts is None else round(min(1.0, max((volts - 3.5) / 0.5, 0.0)) * 100, 1)
    return TankReading(
        ts=datetime.fromisoformat(data["LastPostTimeIso"].replace(" ", "T") + "Z").replace(microsecond=0),
        level_pct=level,
        remaining_gal=None if size is None else level / 100 * size,
        tank_size_gal=size,
        alert_status=data.get("AlertStatus"),
        battery_pct=battery,
        raw_json=scrub(json.dumps(data)),
    )


class CentriConnectSource:
    name = "centriconnect"

    def __init__(self, cfg: Config, sessions: sessionmaker[Session]):
        self.cfg = cfg
        self.sessions = sessions
        self.interval_s = cfg.intervals.tank_s
        self.client = httpx.AsyncClient(timeout=30)

    async def fetch(self) -> dict:
        s = self.cfg.secrets
        url = API_URL.format(user_id=s.centriconnect_user_id, device_id=s.centriconnect_device_id)
        r = await self.client.get(url, params={"device_auth": s.centriconnect_device_auth})
        r.raise_for_status()
        body = r.json() or {}
        if s.centriconnect_device_id not in body:
            raise RuntimeError(f"Unexpected CentriConnect response: {body.get('error', 'no device data')}")
        return body[s.centriconnect_device_id]

    async def poll(self) -> int:
        """Store the reading only if it is new: the device reports just 2-3 times a day."""
        row = tank_row(await self.fetch())
        with self.sessions.begin() as db:
            last = db.scalars(select(TankReading).order_by(TankReading.ts.desc()).limit(1)).first()
            if last and last.ts == row.ts and last.level_pct == row.level_pct:
                return 0
            db.merge(row)
        return 1
