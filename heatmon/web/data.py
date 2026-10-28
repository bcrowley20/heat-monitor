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
from heatmon.processing import baseline
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


# ---------- invoice baseline and period comparison ----------

def hdd_by_day(c: Ctx) -> dict[date, float]:
    with c.sessions() as db:
        return dict(db.execute(select(DailySummary.date, DailySummary.hdd).where(DailySummary.hdd.is_not(None))).all())


def price_per_gal(c: Ctx) -> float | None:
    if c.cfg.propane.price_per_gal is not None:
        return c.cfg.propane.price_per_gal
    with c.sessions() as db:
        return db.scalar(select(PropaneDelivery.price_per_gal)
                         .where(PropaneDelivery.source == "invoice", PropaneDelivery.price_per_gal.is_not(None))
                         .order_by(PropaneDelivery.date.desc()).limit(1))


@dataclass
class InvoiceBaseline:
    intervals: list[baseline.DeliveryInterval]
    fit: baseline.Baseline | None
    hdd: dict[date, float]


def invoice_baseline(c: Ctx) -> InvoiceBaseline:
    """Intervals between invoiced deliveries and the line fitted through them."""
    with c.sessions() as db:
        invoices = [baseline.Delivery(r.date, r.gallons, r.price_per_gal, r.tank_pct_before, r.tank_pct_after)
                    for r in db.scalars(select(PropaneDelivery).where(PropaneDelivery.source == "invoice"))]
    hdd = hdd_by_day(c)
    intervals = baseline.delivery_intervals(invoices, hdd, c.cfg.propane.tank_size_gal)
    return InvoiceBaseline(intervals, baseline.fit_baseline(intervals), hdd)


def excluded_days(c: Ctx, types: tuple[str, ...] = ("fireplace", "away")) -> set[date]:
    with c.sessions() as db:
        return {local_date(ts, c.tz) for ts in db.scalars(select(Event.ts).where(Event.type.in_(types)))}


def first_thermostat_day(c: Ctx) -> date | None:
    with c.sessions() as db:
        first = db.scalar(select(func.min(ThermostatReading.ts)))
    return local_date(first, c.tz) if first else None


def period_stats(c: Ctx, exclude: set[date], fit: baseline.Baseline | None, price: float | None) -> dict:
    """Totals and rates for one comparison period. Days in `exclude` are left out."""
    first_t = first_thermostat_day(c)
    days = [d for d in daily(c) if date.fromisoformat(d["date"]) not in exclude and d["hdd"] is not None]
    gal_days = [d for d in days if d["gal_used_est"] is not None]
    # Days before the thermostats were connected have no runtime, not zero runtime.
    t_days = [d for d in days if first_t and date.fromisoformat(d["date"]) > first_t]
    gallons, gal_hdd = sum(d["gal_used_est"] for d in gal_days), sum(d["hdd"] for d in gal_days)
    t_hdd, call_min = sum(d["hdd"] for d in t_days), sum(d["any_zone_call_min"] for d in t_days)
    bins = [gal_days[i:i + 7] for i in range(0, len(gal_days), 7)]
    bins = [b for b in bins if len(b) >= 4]
    gal_fit = fit_line([mean(d["hdd"] for d in b) for b in bins], [mean(d["gal_used_est"] for d in b) for b in bins])
    call_fit = fit_line([d["hdd"] for d in t_days], [d["any_zone_call_min"] for d in t_days])
    predicted = fit.predict(len(gal_days), gal_hdd) if fit and gal_days else None
    t_dates = {d["date"] for d in t_days}
    overs = [k["overshoot_f"] for k in zone_calls(c) if k["overshoot_f"] is not None and k["start"][:10] in t_dates]
    ratio = gallons / gal_hdd if gal_hdd >= 10 else None
    return {
        "days": len(days), "days_with_propane": len(gal_days), "days_with_thermostats": len(t_days),
        "mean_temp_f": _r(mean(d["mean_temp_f"] for d in days)) if days else None,
        "hdd": _r(sum(d["hdd"] for d in days)),
        "hdd_range": _r(max(d["hdd"] for d in days) - min(d["hdd"] for d in days)) if days else None,
        "gallons": _r(gallons) if gal_days else None,
        "gal_per_hdd": _r(ratio, 3),
        "gal_per_hdd_fit": _r(gal_fit.slope, 3) if gal_fit else None,
        "baseline_gallons": _r(predicted),
        "vs_baseline_pct": _r((gallons - predicted) / predicted * 100) if predicted else None,
        "cost_per_hdd": _r(ratio * price, 2) if ratio is not None and price else None,
        "call_min_per_hdd": _r(call_min / t_hdd) if t_hdd >= 10 else None,
        "call_min_per_hdd_fit": _r(call_fit.slope) if call_fit and len(t_days) >= 5 else None,
        "calls_per_day": _r(mean(d["zone_calls"] for d in t_days)) if t_days else None,
        "overshoot_f": _r(mean(overs)) if overs else None,
    }


def _pct(new: float, old: float) -> str:
    change = (new - old) / old * 100
    return f"{abs(change):.0f}% {'lower' if change < 0 else 'higher'}"


def compare_summary(a: dict, b: dict, a_name: str, b_name: str) -> list[str]:
    """Plain sentences comparing period B against period A, ending with a confidence note."""
    out = []
    if a["mean_temp_f"] is not None and b["mean_temp_f"] is not None:
        out.append(f"{b_name} averaged {b['mean_temp_f']}°F outside with {b['hdd']} degree days; "
                   f"{a_name} averaged {a['mean_temp_f']}°F with {a['hdd']}.")
    if a["gal_per_hdd"] and b["gal_per_hdd"]:
        out.append(f"Propane per degree day was {_pct(b['gal_per_hdd'], a['gal_per_hdd'])} in {b_name} "
                   f"({b['gal_per_hdd']} against {a['gal_per_hdd']} gal/HDD).")
    for name, p in ((a_name, a), (b_name, b)):
        if p["vs_baseline_pct"] is not None:
            out.append(f"{name} used {p['gallons']} gallons. The invoice baseline predicts {p['baseline_gallons']} "
                       f"for the same days and weather, so {name} was {abs(p['vs_baseline_pct']):.0f}% "
                       f"{'below' if p['vs_baseline_pct'] < 0 else 'above'} it.")
    if a["call_min_per_hdd"] and b["call_min_per_hdd"]:
        out.append(f"Calling time per degree day was {_pct(b['call_min_per_hdd'], a['call_min_per_hdd'])} in "
                   f"{b_name} ({b['call_min_per_hdd']} against {a['call_min_per_hdd']} minutes).")
    if not out:
        out.append("There isn't enough data in these periods to compare yet.")

    # Rate each measure on the days that actually have that kind of data.
    coverage = {}
    if a["gal_per_hdd"] and b["gal_per_hdd"]:
        coverage["propane"] = min(a["days_with_propane"], b["days_with_propane"])
    if a["call_min_per_hdd"] and b["call_min_per_hdd"]:
        coverage["calling time"] = min(a["days_with_thermostats"], b["days_with_thermostats"])
    if not coverage:
        coverage["weather"] = min(a["days"], b["days"])
    shortest = min(coverage.values())
    notes = [f"for {measure}, one period has only {n} days of data, and at least 14 are needed"
             for measure, n in coverage.items() if n < 14]
    for name, p in ((a_name, a), (b_name, b)):
        if p["gallons"] is not None and p["gallons"] < 100:
            notes.append(f"{name} used only {p['gallons']:.0f} gallons, and the tank reads in 10-gallon "
                         f"steps, so that total could be off by about {1000 / max(p['gallons'], 1):.0f}%")
    if all(p["hdd_range"] is not None and p["hdd_range"] < 10 for p in (a, b)):
        notes.append("the weather barely varied, so the fitted slopes are unreliable")
    if notes:
        out.append("Low confidence: " + "; ".join(notes) + ".")
    elif shortest < 28:
        out.append("Moderate confidence: both periods have 2 to 4 weeks of data.")
    else:
        out.append("Reasonable confidence: both periods have 4 or more weeks of data.")
    return out


def delivery_intervals(c: Ctx, ib: InvoiceBaseline | None = None) -> list[dict]:
    """Usage between invoiced deliveries (all of them; the range is ignored)."""
    intervals = (ib or invoice_baseline(c)).intervals
    rates = sorted(iv.gallons / iv.days for iv in intervals if iv.days)
    typical = rates[len(rates) // 2] if rates else None
    return [{"from": iv.start.isoformat(), "to": iv.end.isoformat(), "days": iv.days, "gallons_used": iv.gallons,
             "hdd": iv.hdd, "gal_per_day": round(iv.gallons / iv.days, 2) if iv.days else None,
             "gal_per_hdd": round(iv.gallons / iv.hdd, 3) if iv.hdd else None,
             "price_per_gal": iv.price_per_gal, "worked_out_from": iv.method,
             # Three times the typical daily rate usually means a wrong date or a copied row.
             "check": "unusually high, check these invoices"
             if typical and iv.days and iv.gallons / iv.days > 3 * typical else ""}
            for iv in intervals]


def baseline_seasons(c: Ctx, ib: InvoiceBaseline | None = None) -> list[dict]:
    """Invoice usage spread over heating seasons (all of them; the range is ignored)."""
    ib = ib or invoice_baseline(c)
    return baseline.by_season(ib.intervals, ib.hdd, ib.fit, price_per_gal(c)) if ib.fit else []


DATASETS = {
    "daily": daily, "thermostat": thermostat, "zone_calls": zone_calls, "weather_hourly": weather_hourly,
    "tank": tank, "deliveries": deliveries, "weekly_propane": weekly_propane, "events": events,
    "delivery_intervals": delivery_intervals, "baseline_seasons": baseline_seasons,
}
