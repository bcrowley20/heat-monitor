"""Hourly outdoor temperature and solar, and daily heating degree days."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import mean

from heatmon.processing.days import day_bounds, local_date

HOUR = timedelta(hours=1)
# Fewer hours than this with a temperature, and the day gets no HDD.
MIN_HOURS = 18


def _hour(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def hourly_temps(obs: list[tuple[datetime, str, float | None]]) -> dict[datetime, tuple[float, str]]:
    """Mean temperature for each UTC hour, from (ts, source, temp_f) rows.

    The station's readings are used wherever it has any for that hour. Open-Meteo
    fills the rest.
    """
    station: dict[datetime, list[float]] = defaultdict(list)
    fill: dict[datetime, float] = {}
    for ts, source, temp in obs:
        if temp is None:
            continue
        if source == "wu":
            station[_hour(ts)].append(temp)
        else:
            fill[_hour(ts)] = temp
    out = {h: (mean(v), "wu") for h, v in station.items()}
    for h, t in fill.items():
        out.setdefault(h, (t, "open-meteo"))
    return out


def daily_solar(obs: list[tuple[datetime, float | None]], tz) -> dict[date, float]:
    """kWh per square meter per local day, from Open-Meteo (ts, W/m²) rows.

    Each value is the average over the hour before its timestamp.
    """
    out: dict[date, float] = defaultdict(float)
    for ts, wm2 in obs:
        if wm2 is not None:
            out[local_date(ts - HOUR, tz)] += wm2 / 1000
    return dict(out)


@dataclass
class DayWeather:
    hdd: float | None
    mean_f: float | None
    min_f: float | None
    max_f: float | None
    hours: int
    open_meteo_hours: int


def day_weather(hourly: dict[datetime, tuple[float, str]], day: date, tz, base_f: float,
                now: datetime) -> DayWeather:
    """HDD = sum over the day's hours of max(0, base - T) / 24.

    If a few hours have no temperature at all, the average of the rest stands in for them.
    A day that hasn't ended yet gets no HDD, since its coldest hours may still be ahead.
    """
    lo, hi = day_bounds(day, tz)
    hours = [lo + i * HOUR for i in range(int((hi - lo) / HOUR))]
    vals = [hourly[h] for h in hours if h in hourly]
    if not vals:
        return DayWeather(None, None, None, None, 0, 0)
    temps = [t for t, _ in vals]
    hdd = mean(max(0.0, base_f - t) for t in temps) * len(hours) / 24
    return DayWeather(
        hdd=round(hdd, 2) if len(vals) >= MIN_HOURS and hi <= now else None,
        mean_f=round(mean(temps), 1),
        min_f=round(min(temps), 1),
        max_f=round(max(temps), 1),
        hours=len(vals),
        open_meteo_hours=sum(1 for _, src in vals if src == "open-meteo"),
    )
