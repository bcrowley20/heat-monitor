"""Propane used between tank readings, fill detection, and daily estimates."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from heatmon.config import Propane
from heatmon.processing.days import day_bounds, local_date


@dataclass
class Reading:
    ts: datetime
    level_pct: float
    tank_size_gal: float | None


@dataclass
class Interval:
    start: datetime
    end: datetime
    kind: str  # 'usage', 'noise' (small rise, counted as zero) or 'delivery'
    gallons: float  # used, or delivered for a 'delivery'
    level_before: float
    level_after: float


def tank_intervals(readings: list[Reading], p: Propane) -> list[Interval]:
    """Readings must be in time order.

    Usage is measured from the lowest level since the last fill. A small rise is sensor
    noise and counts as zero, and the next drop is measured from the level before the
    rise. Otherwise 50% -> 51% -> 49% would count 2% used when only 1% was.
    """
    out = []
    ref = readings[0].level_pct if readings else 0.0
    for a, b in zip(readings, readings[1:]):
        size = b.tank_size_gal or p.tank_size_gal
        if b.level_pct - a.level_pct > p.delivery_threshold_pct:
            kind, gallons = "delivery", size * (b.level_pct - a.level_pct) / 100
            ref = b.level_pct
        elif b.level_pct > ref:
            kind, gallons = "noise", 0.0
        else:
            kind, gallons = "usage", size * (ref - b.level_pct) / 100
            ref = b.level_pct
        out.append(Interval(a.ts, b.ts, kind, gallons, a.level_pct, b.level_pct))
    return out


def daily_usage(intervals: list[Interval], hdd: dict[date, float], p: Propane, tz) -> dict[date, float | None]:
    """Spread each interval's usage over the days it covers.

    Each day's share is in proportion to (base load + gallons per HDD x that day's HDD),
    scaled by how much of the day the interval covers. A day gets None if it isn't fully
    covered by readings, or if a fill happened during it, since part of its usage is unknown.
    """
    used: dict[date, float] = defaultdict(float)
    covered: dict[date, float] = defaultdict(float)  # hours covered by readings
    unknown: set[date] = set()
    for iv in intervals:
        # Work in UTC: Python subtracts two times in the same zone by clock time, which
        # gets DST days wrong.
        start, end = iv.start.astimezone(UTC), iv.end.astimezone(UTC)
        pieces = []  # (day, fraction of that day covered)
        day = local_date(start, tz)
        while True:
            lo, hi = day_bounds(day, tz)
            a, b = max(start, lo), min(end, hi)
            if b > a:
                pieces.append((day, (b - a) / (hi - lo)))
                covered[day] += (b - a).total_seconds() / 3600
            if hi >= end:
                break
            day += timedelta(days=1)
        if iv.kind == "delivery":
            unknown.update(d for d, _ in pieces)
            continue
        weights = [f * (p.base_gal_per_day + p.gal_per_hdd * hdd.get(d, 0.0)) for d, f in pieces]
        total = sum(weights) or 1.0
        for (d, _), w in zip(pieces, weights):
            used[d] += iv.gallons * w / total

    out: dict[date, float | None] = {}
    for d, hours in covered.items():
        lo, hi = day_bounds(d, tz)
        full = hours >= (hi - lo).total_seconds() / 3600 - 1e-6
        out[d] = round(used[d], 2) if full and d not in unknown else None
    return out
