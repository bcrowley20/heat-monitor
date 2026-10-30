"""Alerts: check for conditions that need attention and send them to Pushover.

Each alert is sent once when it starts and once when it clears. Open alerts are kept in
the alerts table, which the web pages also read to show popups.
"""

import logging
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import Alert, SourceStatus, TankReading, ThermostatReading, Zone
from heatmon.db.session import read_setting, write_setting
from heatmon.logs import scrub
from heatmon.sources.pumps import load_settings as load_pump_settings

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
# A room must have a reading this close to the start of its window to count as cold the
# whole time. At least two Nest polls, so one missed poll doesn't hide a cold room.
COVERAGE_SLACK = timedelta(minutes=10)
# An open tank alert clears only once the level is this many points above the limit, so
# readings that wobble around the limit don't alert over and over.
TANK_CLEAR_MARGIN_PCT = 2


class AlertSettings(BaseModel):
    pushover_app_token: str = ""
    pushover_user_key: str = ""
    popups: bool = True
    tank_low_on: bool = True
    tank_low_pct: float = Field(20, ge=0, le=100)
    source_stale_on: bool = True
    source_stale_hours: float = Field(6, gt=0)
    room_cold_on: bool = True
    room_cold_f: float = Field(2, gt=0)
    room_cold_hours: float = Field(2, gt=0)


def load_settings(sessions: sessionmaker[Session]) -> AlertSettings:
    return read_setting(sessions, "alerts", AlertSettings)


def save_settings(sessions: sessionmaker[Session], s: AlertSettings) -> None:
    write_setting(sessions, "alerts", s)


def conditions(sessions: sessionmaker[Session], s: AlertSettings, now: datetime, *, sources: set[str] = frozenset(),
               started: datetime | None = None, open_keys: set[tuple[str, str]] = frozenset(),
               nest_s: int = 120) -> dict[tuple[str, str], str]:
    """Everything that needs attention right now, as (kind, subject) -> message.

    `sources` are the data sources being collected and `started` is when collecting began,
    so a source that has never worked still alerts. `open_keys` are alerts already open.
    """
    found: dict[tuple[str, str], str] = {}
    with sessions() as db:
        if s.tank_low_on:
            tank = db.scalars(select(TankReading).order_by(TankReading.ts.desc()).limit(1)).first()
            limit = s.tank_low_pct + (TANK_CLEAR_MARGIN_PCT if ("tank_low", "tank") in open_keys else 0)
            if tank and tank.level_pct < limit:
                found["tank_low", "tank"] = f"Propane tank is at {tank.level_pct:.0f}% (alert below {s.tank_low_pct:g}%)."

        if s.source_stale_on:
            last = {st.source: st.last_success_ts
                    for st in db.scalars(select(SourceStatus).where(SourceStatus.source.in_(sources)))}
            for name in sorted(sources):
                since = last.get(name) or started
                if since and now - since > timedelta(hours=s.source_stale_hours):
                    hours = (now - since).total_seconds() / 3600
                    found["source_stale", name] = (f"No new data from {name} for {hours:.0f} hours." if last.get(name)
                                                   else f"No data from {name} since the app started {hours:.0f} hours ago.")

        if s.room_cold_on:
            since = now - timedelta(hours=s.room_cold_hours)
            slack = max(COVERAGE_SLACK, timedelta(seconds=2 * nest_s))
            margin_c = s.room_cold_f * 5 / 9
            rows = db.execute(select(Zone.name, ThermostatReading)
                              .join(Zone, Zone.id == ThermostatReading.zone_id)
                              .where(ThermostatReading.ts >= since)
                              .order_by(ThermostatReading.ts)).all()
            by_zone: dict[str, list[ThermostatReading]] = {}
            for name, r in rows:
                by_zone.setdefault(name, []).append(r)
            for name, rs in by_zone.items():
                cold = rs[0].ts <= since + slack and all(
                    r.mode == "HEAT" and r.eco_mode != "MANUAL_ECO" and r.ambient_c is not None
                    and r.setpoint_heat_c is not None and r.ambient_c < r.setpoint_heat_c - margin_c for r in rs)
                if cold:
                    gap_f = (rs[-1].setpoint_heat_c - rs[-1].ambient_c) * 9 / 5
                    found["room_cold", name] = (f"{name} has been more than {s.room_cold_f:g}°F below its setpoint "
                                                f"for {s.room_cold_hours:g} hours (now {gap_f:.1f}°F below).")
    return found


async def send_pushover(s: AlertSettings, title: str, message: str) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(PUSHOVER_URL, data={"token": s.pushover_app_token, "user": s.pushover_user_key,
                                                   "title": title, "message": message})
        r.raise_for_status()


class AlertsJob:
    """Opens and clears alerts, and sends each change to Pushover."""

    name = "alerts"

    def __init__(self, cfg: Config, sessions: sessionmaker[Session], sources: set[str], send=send_pushover):
        self.sessions, self.sources, self.send = sessions, sources, send
        self.interval_s, self.nest_s = cfg.intervals.alerts_s, cfg.intervals.nest_s
        self.started = datetime.now(UTC)

    @staticmethod
    def _checked(s: AlertSettings, sources: set[str], kind: str, subject: str) -> bool:
        """Whether this alert is still being checked. One that isn't closes without a message."""
        return {"tank_low": s.tank_low_on, "room_cold": s.room_cold_on,
                "source_stale": s.source_stale_on and subject in sources}.get(kind, False)

    async def poll(self) -> int:
        s = load_settings(self.sessions)
        now = datetime.now(UTC)
        # The pump sensor is watched only while it is turned on.
        sources = self.sources if load_pump_settings(self.sessions).enabled else self.sources - {"pumps"}
        changed = 0
        with self.sessions.begin() as db:
            open_alerts = {(a.kind, a.subject): a for a in db.scalars(select(Alert).where(Alert.cleared_ts.is_(None)))}
            found = conditions(self.sessions, s, now, sources=sources, started=self.started,
                               open_keys=set(open_alerts), nest_s=self.nest_s)
            for key, message in found.items():
                if key not in open_alerts:
                    db.add(Alert(kind=key[0], subject=key[1], message=message, started_ts=now))
                    changed += 1
            for key, a in open_alerts.items():
                if key not in found:
                    a.cleared_ts = now
                    changed += 1
                    if not self._checked(s, sources, *key) or not a.start_sent:
                        a.start_sent = a.clear_sent = True  # turned off, or never announced: stay quiet
        await self._send_pending(s)
        return changed

    async def _send_pending(self, s: AlertSettings) -> None:
        """Send start and clear messages Pushover doesn't have yet. Without Pushover set up,
        mark them sent so turning it on later doesn't send a backlog."""
        with self.sessions.begin() as db:
            pending = list(db.scalars(select(Alert).where(or_(Alert.start_sent.is_(False), Alert.clear_sent.is_(False)
                                                              & Alert.cleared_ts.is_not(None))).order_by(Alert.id)))
            for a in pending:
                field = "start_sent" if not a.start_sent else "clear_sent"
                if s.pushover_app_token and s.pushover_user_key:
                    title = "Heating alert" if field == "start_sent" else "Heating alert cleared"
                    try:
                        await self.send(s, title, a.message)
                    except httpx.HTTPStatusError as e:  # Pushover refused it, e.g. a wrong key: retrying won't help
                        log.warning("Pushover refused the message: %s", e.response.status_code)
                    except Exception as e:  # e.g. no internet: try again next time
                        log.warning("Pushover send failed, will retry: %s", scrub(f"{type(e).__name__}: {e}"))
                        return
                setattr(a, field, True)
