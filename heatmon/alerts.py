"""Alerts: check for conditions that need attention and send them to Pushover.

Each alert is sent once when it starts and once when it clears. Open alerts are kept in
the alerts table, which the web pages also read to show popups.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import (Alert, BoilerCounters, BoilerCycle, BoilerParams, BoilerSample, DailySummary, Event,
                               SourceStatus, TankReading, ThermostatReading, Zone)
from heatmon.db.session import read_setting, write_setting
from heatmon.logs import scrub
from heatmon.processing import boiler as bp
from heatmon.processing.days import day_bounds
from heatmon.sources.boiler import load_settings as load_boiler_settings
from heatmon.sources.pumps import load_settings as load_pump_settings
from heatmon.web.data import fit_line

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
# A room must have a reading this close to the start of its window to count as cold the
# whole time. At least two Nest polls, so one missed poll doesn't hide a cold room.
COVERAGE_SLACK = timedelta(minutes=10)
# An open tank alert clears only once the level is this many points above the limit, so
# readings that wobble around the limit don't alert over and over.
TANK_CLEAR_MARGIN_PCT = 2
SHORT_CYCLE_WINDOW = timedelta(hours=2)
# Minutes a boiler reading must stay out of line before it alerts.
STATE_MIN, SLAB_MIN, RETURN_MIN, TANK_MIN = 3, 5, 30, 45
FULL_DAY_SAMPLES = 1200  # of 1440 minutes; days with less boiler data aren't compared
RUNTIME_FIT_DAYS = 14  # full days needed in the current settings period before comparing runtime
RUNTIME_MIN_EXTRA_H = 1
DHW_MEDIAN_DAYS, DHW_MIN_DAYS = 14, 7
# Alert kind -> the setting that turns it on.
BOILER_KINDS = {"boiler_short": "boiler_short_on", "boiler_ignition": "boiler_ignition_on",
                "boiler_state": "boiler_state_on", "boiler_slab": "boiler_slab_on",
                "boiler_return": "boiler_return_on", "boiler_tank": "boiler_tank_on",
                "boiler_runtime": "boiler_runtime_on", "boiler_dhw": "boiler_dhw_jump_on",
                "boiler_settings": "boiler_settings_on"}


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
    boiler_short_on: bool = True
    boiler_short_count: int = Field(3, ge=1)
    boiler_short_min: float = Field(10, gt=0)
    boiler_ignition_on: bool = True
    boiler_state_on: bool = True
    boiler_slab_on: bool = True
    boiler_slab_f: float = Field(120, gt=0)
    boiler_return_on: bool = True
    boiler_return_f: float = Field(130, gt=0)
    boiler_tank_on: bool = True
    boiler_tank_f: float = Field(105, gt=0)
    boiler_runtime_on: bool = True
    boiler_runtime_pct: float = Field(30, gt=0)
    boiler_dhw_jump_on: bool = True
    boiler_dhw_jump_x: float = Field(2, gt=1)
    boiler_settings_on: bool = True


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


def _sustained(db: Session, now: datetime, minutes: int, ok) -> bool:
    """Whether every boiler minute in the last `minutes` passes `ok`, with none missing."""
    rows = db.scalars(select(BoilerSample).where(BoilerSample.ts >= now - timedelta(minutes=minutes + 1),
                                                 BoilerSample.ts <= now)
                      .order_by(BoilerSample.ts)).all()
    return (len(rows) >= minutes and rows[0].ts <= now - timedelta(minutes=minutes - 1)
            and all(ok(r) for r in rows))


def boiler_conditions(sessions: sessionmaker[Session], s: AlertSettings, now: datetime,
                      tz: ZoneInfo) -> dict[tuple[str, str], str]:
    """Boiler problems visible in the last few hours. The day-by-day checks are in boiler_day_conditions."""
    found: dict[tuple[str, str], str] = {}

    def local(ts: datetime) -> str:
        return ts.astimezone(tz).strftime("%b %-d %H:%M")

    with sessions() as db:
        if s.boiler_short_on:
            short = [c for c in db.scalars(select(BoilerCycle).where(BoilerCycle.start_ts >= now - SHORT_CYCLE_WINDOW))
                     if bp.is_short(c, s.boiler_short_min)]
            if len(short) >= s.boiler_short_count:
                found["boiler_short", "boiler"] = (
                    f"Boiler is short cycling: {len(short)} heating burns under {s.boiler_short_min:g} minutes in "
                    f"the last 2 hours stopped on setpoint met while a zone was still calling.")
        if s.boiler_ignition_on:
            rows = db.scalars(select(BoilerCounters).where(BoilerCounters.ts >= now - timedelta(hours=24))
                              .order_by(BoilerCounters.ts)).all()
            if rows:
                failed = ((rows[-1].burner_starts - rows[-1].ignitions) - (rows[0].burner_starts - rows[0].ignitions))
                if failed > 0:
                    found["boiler_ignition", "boiler"] = (
                        f"The boiler's counters show {failed} burner start{'s' if failed > 1 else ''} without a "
                        "successful ignition in the last 24 hours.")
        if s.boiler_state_on and _sustained(db, now, STATE_MIN, lambda r: r.state not in bp.STATES):
            code = db.scalar(select(BoilerSample.state).order_by(BoilerSample.ts.desc()).limit(1))
            found["boiler_state", "boiler"] = (f"The boiler has been in an unfamiliar burner state (code {code}) for "
                                               f"{STATE_MIN} minutes. It may be locked out; check its display.")
        if s.boiler_slab_on and _sustained(db, now, SLAB_MIN, lambda r: r.mode == "sh" and r.system_f is not None
                                           and r.system_f > s.boiler_slab_f):
            found["boiler_slab", "boiler"] = (f"Heating supply water has been above {s.boiler_slab_f:g}°F for "
                                              f"{SLAB_MIN} minutes. The slab has no other protection.")
        if s.boiler_return_on and _sustained(db, now, RETURN_MIN, lambda r: r.mode == "sh" and r.flame_s > 0
                                             and r.inlet_f is not None and r.inlet_f > s.boiler_return_f):
            found["boiler_return", "boiler"] = (f"Return water has been above {s.boiler_return_f:g}°F for "
                                                f"{RETURN_MIN} minutes of heating, so the boiler isn't condensing.")
        if s.boiler_tank_on and _sustained(db, now, TANK_MIN, lambda r: r.tank_f is not None
                                           and r.dhw_setpoint_f is not None and r.dhw_setpoint_f > s.boiler_tank_f
                                           and r.tank_f < s.boiler_tank_f):
            found["boiler_tank", "boiler"] = (f"The hot water tank has been below {s.boiler_tank_f:g}°F for "
                                              f"{TANK_MIN} minutes and isn't recovering.")
        if s.boiler_settings_on:
            recent = db.scalars(select(BoilerParams).where(BoilerParams.ts >= now - timedelta(hours=24))
                                .order_by(BoilerParams.ts.desc())).first()
            if recent and db.scalar(select(BoilerParams.ts).where(BoilerParams.ts < recent.ts).limit(1)):
                found["boiler_settings", "boiler"] = (
                    f"Boiler settings changed around {local(recent.ts)}. The change is listed on the Boiler page.")
    return found


def boiler_day_conditions(sessions: sessionmaker[Session], s: AlertSettings, day: date,
                          tz: ZoneInfo) -> dict[tuple[str, str], str]:
    """Compare one finished day's burner time with the days before it."""
    found: dict[tuple[str, str], str] = {}
    lo, hi = day_bounds(day, tz)
    with sessions() as db:
        period = db.scalar(select(Event.ts).where(Event.type == "setting_change", Event.ts < lo)
                           .order_by(Event.ts.desc()).limit(1))
        since = max(period or lo - timedelta(days=60), lo - timedelta(days=60))
        samples = db.scalars(select(BoilerSample).where(BoilerSample.ts >= since, BoilerSample.ts < hi)).all()
        cycles = db.scalars(select(BoilerCycle).where(BoilerCycle.start_ts >= since, BoilerCycle.start_ts < hi)).all()
        hdd = {r.date: r.hdd for r in db.scalars(select(DailySummary).where(DailySummary.date >= since.date()))}
    counts: dict[date, int] = {}
    for r in samples:
        d = r.ts.astimezone(tz).date()
        counts[d] = counts.get(d, 0) + 1
    totals = bp.daily_totals(samples, cycles, tz)
    full = {d: t for d, t in totals.items() if counts.get(d, 0) >= FULL_DAY_SAMPLES
            and (period is None or d > period.astimezone(tz).date())}
    today = full.pop(day, None)
    if today is None:
        return found
    if s.boiler_runtime_on and hdd.get(day) is not None:
        pts = [(hdd[d], t["sh_burn_h"]) for d, t in full.items() if hdd.get(d) is not None]
        if len(pts) >= RUNTIME_FIT_DAYS:
            fit = fit_line([p[0] for p in pts], [p[1] for p in pts])
            if fit:
                expected = max(0.0, fit.intercept + fit.slope * hdd[day])
                actual = today["sh_burn_h"]
                if actual > expected * (1 + s.boiler_runtime_pct / 100) and actual - expected > RUNTIME_MIN_EXTRA_H:
                    found["boiler_runtime", "boiler"] = (
                        f"On {day:%b %-d} the boiler heated for {actual:.1f} hours. With {hdd[day]:.0f} degree days, "
                        f"the {len(pts)} days since the last setting change point to about {expected:.1f} hours.")
    if s.boiler_dhw_jump_on:
        before = [t["dhw_burn_min"] for d, t in sorted(full.items()) if d < day][-DHW_MEDIAN_DAYS:]
        if len(before) >= DHW_MIN_DAYS and (typical := median(before)) > 0 \
                and today["dhw_burn_min"] > s.boiler_dhw_jump_x * typical:
            found["boiler_dhw", "boiler"] = (
                f"On {day:%b %-d} the boiler heated hot water for {today['dhw_burn_min']:.0f} minutes, against a "
                f"typical {typical:.0f}. Check for a running tap or the recirculation pump.")
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
        self.tz = ZoneInfo(cfg.timezone)
        self._day_cache: tuple | None = None  # (day, settings, conditions); the daily checks are slow

    @staticmethod
    def _checked(s: AlertSettings, sources: set[str], kind: str, subject: str) -> bool:
        """Whether this alert is still being checked. One that isn't closes without a message."""
        if kind in BOILER_KINDS:
            return getattr(s, BOILER_KINDS[kind]) and "boiler" in sources
        return {"tank_low": s.tank_low_on, "room_cold": s.room_cold_on,
                "source_stale": s.source_stale_on and subject in sources}.get(kind, False)

    async def poll(self) -> int:
        s = load_settings(self.sessions)
        now = datetime.now(UTC)
        # The pump sensor and the boiler are watched only while they are turned on.
        sources = set(self.sources)
        if not load_pump_settings(self.sessions).enabled:
            sources.discard("pumps")
        if not load_boiler_settings(self.sessions).enabled:
            sources.discard("boiler")
        changed = 0
        with self.sessions.begin() as db:
            open_alerts = {(a.kind, a.subject): a for a in db.scalars(select(Alert).where(Alert.cleared_ts.is_(None)))}
            found = conditions(self.sessions, s, now, sources=sources, started=self.started,
                               open_keys=set(open_alerts), nest_s=self.nest_s)
            if "boiler" in sources:
                found |= boiler_conditions(self.sessions, s, now, self.tz)
                yesterday = now.astimezone(self.tz).date() - timedelta(days=1)
                if self._day_cache is None or self._day_cache[:2] != (yesterday, s):
                    self._day_cache = (yesterday, s, boiler_day_conditions(self.sessions, s, yesterday, self.tz))
                found |= self._day_cache[2]
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
