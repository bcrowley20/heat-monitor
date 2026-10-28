"""Rebuild zone calls, detected fills and daily summaries from the raw readings."""

import asyncio
import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import (DailySummary, PropaneDelivery, TankReading, ThermostatHistory,
                               ThermostatReading, WeatherObs, Zone, ZoneCall)
from heatmon.processing.days import day_bounds, local_date
from heatmon.processing.degree_days import HOUR, daily_solar, day_weather, hourly_temps
from heatmon.processing.propane import Interval, Reading, daily_usage, tank_intervals
from heatmon.processing.zone_calls import Sample, derive_calls, minutes_on_day
from heatmon.processing.zone_history import history_by_day

# Readings this far before the rebuild window are re-read so calls that started
# before the window keep their true start time.
LOOKBACK = timedelta(days=2)
RECENT_DAYS = 2
# A history day needs records for this share of its 15-minute intervals. The export's
# first and last days are usually partial.
HISTORY_MIN_COVERAGE = 0.9
# A detected fill this close to an invoiced delivery is the same delivery.
INVOICE_MATCH_DAYS = 3


def rebuild(cfg: Config, sessions: sessionmaker[Session], start: date | None = None) -> int:
    """Rebuild everything from the local date `start` through today. None means all data.

    Returns the number of days summarized.
    """
    tz = ZoneInfo(cfg.timezone)
    if start is None:
        with sessions() as db:
            firsts = [db.scalar(select(func.min(m.ts))) for m in (ThermostatReading, WeatherObs, TankReading)]
            firsts.append(db.scalar(select(func.min(ThermostatHistory.interval_start))))
        firsts = [f for f in firsts if f is not None]
        if not firsts:
            return 0
        start = local_date(min(firsts), tz)
    today = datetime.now(tz).date()
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    if not days:
        return 0
    _rebuild_zone_calls(cfg, sessions, day_bounds(start, tz)[0])
    intervals = _rebuild_detected_deliveries(cfg, sessions, tz)
    _rebuild_daily(cfg, sessions, days, intervals, tz)
    return len(days)


def _rebuild_zone_calls(cfg: Config, sessions: sessionmaker[Session], since: datetime) -> None:
    now = datetime.now(UTC)
    with sessions.begin() as db:
        # Reach back to the start of any stored call that runs into the window or was
        # still open, so it is rebuilt whole.
        earliest = db.scalar(select(func.min(ZoneCall.start_ts))
                             .where(or_(ZoneCall.end_ts >= since, ZoneCall.open)))
        if earliest is not None:
            since = min(since, earliest)
        db.execute(delete(ZoneCall).where(ZoneCall.end_ts >= since))
        for zone_id in db.scalars(select(Zone.id)).all():
            rows = db.execute(
                select(ThermostatReading.ts, ThermostatReading.hvac_status,
                       ThermostatReading.ambient_c, ThermostatReading.setpoint_heat_c)
                .where(ThermostatReading.zone_id == zone_id, ThermostatReading.ts >= since - LOOKBACK)
                .order_by(ThermostatReading.ts))
            calls = derive_calls([Sample(*r) for r in rows], cfg.intervals.nest_s, now)
            db.add_all(ZoneCall(zone_id=zone_id, start_ts=c.start_ts, end_ts=c.end_ts,
                                duration_s=c.duration_s, start_ambient_c=c.start_ambient_c,
                                end_ambient_c=c.end_ambient_c, setpoint_c=c.setpoint_c,
                                open=c.open, gap_closed=c.gap_closed)
                       for c in calls if c.end_ts >= since)


def _rebuild_detected_deliveries(cfg: Config, sessions: sessionmaker[Session], tz) -> list[Interval]:
    with sessions.begin() as db:
        readings = [Reading(*r) for r in db.execute(
            select(TankReading.ts, TankReading.level_pct, TankReading.tank_size_gal)
            .order_by(TankReading.ts))]
        intervals = tank_intervals(readings, cfg.propane)
        db.execute(delete(PropaneDelivery).where(PropaneDelivery.source == "detected"))
        invoices = db.scalars(select(PropaneDelivery.date).where(PropaneDelivery.source == "invoice")).all()

        def has_invoice(day: date) -> bool:  # the invoice is the better record of a fill
            return any(abs((day - d).days) <= INVOICE_MATCH_DAYS for d in invoices)

        db.add_all(PropaneDelivery(date=local_date(iv.end, tz), gallons=round(iv.gallons, 1),
                                   tank_pct_before=iv.level_before, tank_pct_after=iv.level_after,
                                   source="detected")
                   for iv in intervals if iv.kind == "delivery" and not has_invoice(local_date(iv.end, tz)))
    return intervals


def _rebuild_daily(cfg: Config, sessions: sessionmaker[Session], days: list[date],
                   intervals: list[Interval], tz) -> None:
    lo, hi = day_bounds(days[0], tz)[0], day_bounds(days[-1], tz)[1]
    intervals = [iv for iv in intervals if iv.end > lo]
    # Tank intervals can start before the window. Their earlier days need HDD too, or the
    # window's first day gets too large a share of the gallons.
    first = min([days[0]] + [local_date(iv.start, tz) for iv in intervals])
    weather_days = [first + timedelta(days=i) for i in range((days[-1] - first).days + 1)]
    with sessions() as db:
        obs = db.execute(select(WeatherObs.ts, WeatherObs.source, WeatherObs.temp_f, WeatherObs.solar_wm2)
                         .where(WeatherObs.station_id == cfg.station_id,
                                WeatherObs.ts >= day_bounds(first, tz)[0] - HOUR,
                                WeatherObs.ts < hi + HOUR)).all()
        calls = db.execute(select(Zone.name, ZoneCall.start_ts, ZoneCall.end_ts)
                           .join(Zone, Zone.id == ZoneCall.zone_id)
                           .where(ZoneCall.end_ts > lo, ZoneCall.start_ts < hi)).all()
        first_live = db.scalar(select(func.min(ThermostatReading.ts)).where(ThermostatReading.zone_id.is_not(None)))
        history = history_by_day(db.execute(
            select(Zone.name, ThermostatHistory.interval_start, ThermostatHistory.heating_s)
            .join(Zone, Zone.id == ThermostatHistory.zone_id)
            .where(ThermostatHistory.interval_start >= lo, ThermostatHistory.interval_start < hi)).all(), tz)
    # Live polling wins from its first day on; earlier days use the Takeout history.
    first_live_day = local_date(first_live, tz) if first_live else None

    hourly = hourly_temps([(ts, src, t) for ts, src, t, _ in obs])
    solar = daily_solar([(ts, s) for ts, src, _, s in obs if src == "open-meteo"], tz)
    now = datetime.now(UTC)
    weather = {d: day_weather(hourly, d, tz, cfg.hdd_base_f, now) for d in weather_days}
    hdd = {d: w.hdd for d, w in weather.items() if w.hdd is not None}
    gallons = daily_usage(intervals, hdd, cfg.propane, tz)

    calls_by_day = defaultdict(list)  # every day each call touches
    for zone, a, b in calls:
        d = local_date(a, tz)
        while d <= local_date(b, tz):
            calls_by_day[d].append((zone, a, b))
            d += timedelta(days=1)

    rows = []
    for d in days:
        if first_live_day and d >= first_live_day:
            todays = calls_by_day.get(d, [])
            by_zone = defaultdict(list)
            for zone, a, b in todays:
                by_zone[zone].append((a, b))
            per_zone_min = {z: round(minutes_on_day(iv, d, tz), 1) for z, iv in sorted(by_zone.items())}
            per_zone_count = Counter(z for z, a, _ in todays if local_date(a, tz) == d)
            any_zone = minutes_on_day([(a, b) for _, a, b in todays], d, tz)
            source = "live"
        elif d in history and history[d].quarters >= HISTORY_MIN_COVERAGE * hours_in(d, tz) * 4:
            h = history[d]
            per_zone_min = {z: round(m, 1) for z, m in sorted(h.per_zone_min.items())}
            per_zone_count, any_zone, source = h.per_zone_count, h.any_zone_min, "history"
        else:
            per_zone_min, per_zone_count, any_zone, source = {}, {}, 0.0, None
        w, gal = weather[d], gallons.get(d)
        rows.append({
            "date": d,
            "hdd": w.hdd,
            "mean_temp_f": w.mean_f,
            "min_temp_f": w.min_f,
            "max_temp_f": w.max_f,
            "solar_kwh_m2": round(solar[d], 2) if d in solar else None,
            "weather_hours": w.hours,
            "open_meteo_hours": w.open_meteo_hours,
            "any_zone_call_min": round(any_zone, 1),
            "total_zone_call_min": round(sum(per_zone_min.values()), 1),
            "zone_call_count": sum(per_zone_count.values()),
            "per_zone_call_min_json": json.dumps(per_zone_min),
            "per_zone_call_count_json": json.dumps(dict(sorted(per_zone_count.items()))),
            "gal_used_est": gal,
            # Below 1 HDD the ratio is mostly noise.
            "gal_per_hdd": round(gal / w.hdd, 3) if gal is not None and w.hdd and w.hdd >= 1 else None,
            "runtime_source": source,
        })
    with sessions.begin() as db:
        db.execute(delete(DailySummary).where(DailySummary.date >= days[0], DailySummary.date <= days[-1]))
        db.execute(insert(DailySummary), rows)


def hours_in(d: date, tz) -> float:
    lo, hi = day_bounds(d, tz)
    return (hi - lo).total_seconds() / 3600


class SummariesJob:
    """Runs on the collector schedule and rebuilds the most recent days."""

    name = "summaries"

    def __init__(self, cfg: Config, sessions: sessionmaker[Session]):
        self.cfg = cfg
        self.sessions = sessions
        self.interval_s = cfg.intervals.summaries_s

    async def poll(self) -> int:
        start = datetime.now(ZoneInfo(self.cfg.timezone)).date() - timedelta(days=RECENT_DAYS)
        # Runs in a thread so the web pages stay responsive.
        return await asyncio.to_thread(rebuild, self.cfg, self.sessions, start)
