"""Queries behind every page, table, CSV download and /api endpoint.

Each dataset function takes a Ctx (config, sessions and a range of local dates,
inclusive) and returns a list of flat dicts. Times are local, as text.
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import (DailySummary, Event, PropaneDelivery, SourceStatus, TankReading,
                               ThermostatReading, WeatherObs, Zone, ZoneCall)
from heatmon.processing.days import day_bounds, local_date
from heatmon.processing.degree_days import HOUR, hourly_temps

OVERSHOOT_WINDOW = timedelta(hours=6)


def c_to_f(c: float | None) -> float | None:
    return None if c is None else round(c * 9 / 5 + 32, 1)


def _r(x: float | None, n: int = 1) -> float | None:
    return None if x is None else round(x, n)


class Ctx:
    """Shared arguments for one request: config, sessions, local timezone and range."""

    def __init__(self, cfg: Config, sessions: sessionmaker[Session], start: date, end: date):
        self.cfg, self.sessions, self.start, self.end = cfg, sessions, start, end
        self.tz = ZoneInfo(cfg.timezone)
        self.lo = day_bounds(start, self.tz)[0]
        self.hi = day_bounds(end, self.tz)[1]

    def local(self, ts: datetime | None) -> str | None:
        return ts.astimezone(self.tz).strftime("%Y-%m-%d %H:%M") if ts else None


def zone_names(c: Ctx) -> list[str]:
    """Zones in config order, which also fixes each zone's chart color."""
    return [z.name for z in c.cfg.zones]


# ---------- datasets ----------

def daily(c: Ctx) -> list[dict]:
    with c.sessions() as db:
        rows = db.scalars(select(DailySummary).where(DailySummary.date >= c.start,
                                                      DailySummary.date <= c.end)
                          .order_by(DailySummary.date)).all()
    out = []
    for r in rows:
        minutes, counts = json.loads(r.per_zone_call_min_json), json.loads(r.per_zone_call_count_json)
        row = {
            "date": r.date.isoformat(), "hdd": r.hdd, "mean_temp_f": r.mean_temp_f,
            "min_temp_f": r.min_temp_f, "max_temp_f": r.max_temp_f, "solar_kwh_m2": r.solar_kwh_m2,
            "open_meteo_hours": r.open_meteo_hours, "weather_hours": r.weather_hours,
            "any_zone_call_min": r.any_zone_call_min, "total_zone_call_min": r.total_zone_call_min,
            "zone_calls": r.zone_call_count, "gal_used_est": r.gal_used_est, "gal_per_hdd": r.gal_per_hdd,
        }
        for z in zone_names(c):
            row[f"{z} min"] = minutes.get(z, 0)
            row[f"{z} calls"] = counts.get(z, 0)
        out.append(row)
    return out


def thermostat(c: Ctx) -> list[dict]:
    with c.sessions() as db:
        rows = db.execute(
            select(Zone.name, ThermostatReading)
            .join(Zone, Zone.id == ThermostatReading.zone_id)
            .where(ThermostatReading.ts >= c.lo, ThermostatReading.ts < c.hi)
            .order_by(ThermostatReading.ts)).all()
    return [{"time": c.local(r.ts), "zone": name, "temp_f": c_to_f(r.ambient_c),
             "setpoint_f": c_to_f(r.setpoint_heat_c), "humidity_pct": r.humidity_pct,
             "hvac_status": r.hvac_status, "mode": r.mode, "eco_mode": r.eco_mode}
            for name, r in rows]


def zone_calls(c: Ctx) -> list[dict]:
    """Calls that touch the range, with overshoot: the most the room rose above its
    setpoint in the 6 hours after the call ended (or until the next call started)."""
    with c.sessions() as db:
        calls = db.execute(
            select(Zone.name, ZoneCall).join(Zone, Zone.id == ZoneCall.zone_id)
            # Calls starting up to 6 h past the range are loaded only to end the overshoot
            # window of the range's last call; they are not listed.
            .where(ZoneCall.end_ts > c.lo, ZoneCall.start_ts < c.hi + OVERSHOOT_WINDOW)
            .order_by(ZoneCall.zone_id, ZoneCall.start_ts)).all()
        readings = defaultdict(list)
        for zone_id, ts, amb, sp in db.execute(
                select(ThermostatReading.zone_id, ThermostatReading.ts,
                       ThermostatReading.ambient_c, ThermostatReading.setpoint_heat_c)
                .where(ThermostatReading.ts >= c.lo,
                       ThermostatReading.ts < c.hi + OVERSHOOT_WINDOW)
                .order_by(ThermostatReading.ts)):
            readings[zone_id].append((ts, amb, sp))

    out = []
    for i, (name, call) in enumerate(calls):
        if call.start_ts >= c.hi:
            continue
        until = call.end_ts + OVERSHOOT_WINDOW
        if i + 1 < len(calls) and calls[i + 1][1].zone_id == call.zone_id:
            until = min(until, calls[i + 1][1].start_ts)
        # 0 when the room never got above its setpoint.
        above = [0.0] + [amb - sp for ts, amb, sp in readings[call.zone_id]
                         if call.end_ts < ts <= until and amb is not None and sp is not None]
        out.append({
            "zone": name, "start": c.local(call.start_ts), "end": c.local(call.end_ts),
            "minutes": round(call.duration_s / 60, 1),
            "start_temp_f": c_to_f(call.start_ambient_c), "end_temp_f": c_to_f(call.end_ambient_c),
            "setpoint_f": c_to_f(call.setpoint_c),
            "overshoot_f": None if call.open else round(max(above) * 9 / 5, 1),
            "note": "in progress" if call.open else ("readings stopped" if call.gap_closed else ""),
        })
    return out


def weather_hourly(c: Ctx) -> list[dict]:
    """One row per hour: temperature (station, or Open-Meteo where the station is missing),
    Open-Meteo solar, and the station's wind."""
    with c.sessions() as db:
        obs = db.execute(select(WeatherObs.ts, WeatherObs.source, WeatherObs.temp_f, WeatherObs.solar_wm2,
                                WeatherObs.wind_mph, WeatherObs.gust_mph)
                         .where(WeatherObs.station_id == c.cfg.station_id,
                                WeatherObs.ts >= c.lo, WeatherObs.ts < c.hi + HOUR)).all()
    temps = hourly_temps([(ts, src, t) for ts, src, t, *_ in obs])
    solar, wind, gust = {}, defaultdict(list), defaultdict(list)
    for ts, src, _, s, w, g in obs:
        h = ts.replace(minute=0, second=0, microsecond=0)
        if src == "open-meteo":
            solar[h - HOUR] = s  # Open-Meteo's value covers the hour before its timestamp
        else:
            if w is not None:
                wind[h].append(w)
            if g is not None:
                gust[h].append(g)
    hours = sorted(h for h in set(temps) | set(solar) if c.lo <= h < c.hi)
    return [{
        "time": c.local(h),
        "temp_f": _r(temps[h][0]) if h in temps else None,
        "temp_source": temps[h][1] if h in temps else None,
        "solar_wm2": solar.get(h),
        "wind_mph": _r(mean(wind[h])) if wind[h] else None,
        "gust_mph": max(gust[h]) if gust[h] else None,
    } for h in hours]


def tank(c: Ctx) -> list[dict]:
    with c.sessions() as db:
        rows = db.scalars(select(TankReading).where(TankReading.ts >= c.lo, TankReading.ts < c.hi)
                          .order_by(TankReading.ts)).all()
    return [{"time": c.local(r.ts), "level_pct": r.level_pct, "remaining_gal": r.remaining_gal,
             "battery_pct": r.battery_pct, "alert": r.alert_status} for r in rows]


def deliveries(c: Ctx) -> list[dict]:
    with c.sessions() as db:
        rows = db.scalars(select(PropaneDelivery).where(PropaneDelivery.date >= c.start,
                                                         PropaneDelivery.date <= c.end)
                          .order_by(PropaneDelivery.date)).all()
    return [{"date": r.date.isoformat(), "gallons": r.gallons, "tank_pct_before": r.tank_pct_before,
             "tank_pct_after": r.tank_pct_after, "source": r.source, "notes": r.notes} for r in rows]


def weekly_propane(c: Ctx, days: list[dict] | None = None) -> list[dict]:
    """Weeks start on Monday. Only days with a gallon estimate count toward a week's
    gallons and HDD, so the ratio compares like with like."""
    weeks = defaultdict(lambda: {"days": 0, "gallons": 0.0, "hdd": 0.0, "call_min": 0.0})
    for d in days if days is not None else daily(c):
        day = date.fromisoformat(d["date"])
        w = weeks[day - timedelta(days=day.weekday())]
        if d["gal_used_est"] is not None and d["hdd"] is not None:
            w["days"] += 1
            w["gallons"] += d["gal_used_est"]
            w["hdd"] += d["hdd"]
            w["call_min"] += d["any_zone_call_min"]
    return [{
        "week_of": k.isoformat(), "days_with_data": w["days"], "gallons": round(w["gallons"], 1),
        "hdd": round(w["hdd"], 1),
        # A few HDD makes the ratio meaningless.
        "gal_per_hdd": round(w["gallons"] / w["hdd"], 3) if w["hdd"] >= 5 else None,
        "call_min_per_hdd": round(w["call_min"] / w["hdd"], 1) if w["hdd"] >= 5 else None,
    } for k, w in sorted(weeks.items())]


def events(c: Ctx) -> list[dict]:
    with c.sessions() as db:
        rows = db.scalars(select(Event).where(Event.ts >= c.lo, Event.ts < c.hi).order_by(Event.ts)).all()
    return [{"time": c.local(e.ts), "type": e.type, "title": e.title, "details": e.details} for e in rows]


DATASETS = {
    "daily": daily, "thermostat": thermostat, "zone_calls": zone_calls, "weather_hourly": weather_hourly,
    "tank": tank, "deliveries": deliveries, "weekly_propane": weekly_propane, "events": events,
}


# ---------- settings periods and fitting ----------

@dataclass
class Period:
    label: str
    start: datetime | None  # None: from the beginning of the data


def settings_periods(c: Ctx) -> list[Period]:
    """Setting changes split time into periods. Changes on the same day count as one."""
    with c.sessions() as db:
        stamps = db.scalars(select(Event.ts).where(Event.type == "setting_change").order_by(Event.ts)).all()
    last_per_day = {}
    for ts in stamps:
        last_per_day[local_date(ts, c.tz)] = ts
    periods = [Period("Before " + min(last_per_day).strftime("%b %-d, %Y") if last_per_day else "All", None)]
    periods += [Period("From " + d.strftime("%b %-d, %Y"), ts) for d, ts in sorted(last_per_day.items())]
    return periods


def period_of_day(day: date, periods: list[Period], tz) -> str:
    """A day belongs to the period in effect at its start. A changeover day counts as before."""
    start = day_bounds(day, tz)[0]
    return [p for p in periods if p.start is None or p.start <= start][-1].label


@dataclass
class Fit:
    intercept: float
    slope: float
    r2: float
    n: int


def fit_line(xs: list[float], ys: list[float]) -> Fit | None:
    """Least squares y = intercept + slope * x."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - intercept - slope * x) ** 2 for x, y in zip(xs, ys))
    return Fit(intercept, slope, 1 - ss_res / ss_tot if ss_tot else 0.0, n)


# ---------- overview ----------

def latest(c: Ctx) -> dict:
    """Current conditions for the Overview page."""
    with c.sessions() as db:
        newest = (select(ThermostatReading.device_id, func.max(ThermostatReading.ts).label("ts"))
                  .group_by(ThermostatReading.device_id).subquery())
        zones = {name: r for name, r in db.execute(
            select(Zone.name, ThermostatReading)
            .join(newest, (ThermostatReading.device_id == newest.c.device_id) & (ThermostatReading.ts == newest.c.ts))
            .join(Zone, Zone.id == ThermostatReading.zone_id)
            .order_by(ThermostatReading.ts))}  # newest last, so it wins if a zone had two thermostats
        station = db.scalars(select(WeatherObs).where(WeatherObs.source == "wu")
                             .order_by(WeatherObs.ts.desc()).limit(1)).first()
        solar = db.scalars(select(WeatherObs).where(WeatherObs.source == "open-meteo")
                           .order_by(WeatherObs.ts.desc()).limit(1)).first()
        tank_row = db.scalars(select(TankReading).order_by(TankReading.ts.desc()).limit(1)).first()
    return {
        "zones": [{"zone": z, "temp_f": c_to_f(zones[z].ambient_c), "setpoint_f": c_to_f(zones[z].setpoint_heat_c),
                   "heating": zones[z].hvac_status == "HEATING", "time": c.local(zones[z].ts)}
                  for z in zone_names(c) if z in zones],
        "outdoor_f": station.temp_f if station else None,
        "outdoor_time": c.local(station.ts) if station else None,
        "solar_wm2": solar.solar_wm2 if solar else None,
        "tank_pct": tank_row.level_pct if tank_row else None,
        "tank_gal": tank_row.remaining_gal if tank_row else None,
        "tank_time": c.local(tank_row.ts) if tank_row else None,
        "now": datetime.now(UTC),
    }


def freshness(c: Ctx) -> list[dict]:
    """How long since each source last worked."""
    with c.sessions() as db:
        rows = db.scalars(select(SourceStatus).order_by(SourceStatus.source)).all()
    now = datetime.now(UTC)
    out = []
    for r in rows:
        ago = (now - r.last_success_ts).total_seconds() / 60 if r.last_success_ts else None
        failing = r.last_error_ts is not None and (r.last_success_ts is None or r.last_error_ts > r.last_success_ts)
        out.append({"source": r.source, "minutes_ago": None if ago is None else round(ago), "failing": failing})
    return out
