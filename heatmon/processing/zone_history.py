"""Daily zone runtime from 15-minute history records (Google Takeout)."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from heatmon.processing.days import local_date

QUARTER = timedelta(minutes=15)


@dataclass
class DayRuntime:
    per_zone_min: dict[str, float] = field(default_factory=dict)
    per_zone_count: dict[str, int] = field(default_factory=dict)
    any_zone_min: float = 0.0  # estimated
    quarters: int = 0  # 15-minute records for the best-covered zone


def history_by_day(rows: list[tuple[str, datetime, int]], tz) -> dict[date, DayRuntime]:
    """From (zone, interval start, seconds heating) rows.

    Minutes per zone are exact. The rest are estimates, because the records don't say when
    in each 15 minutes the heating happened: a call is a run of back-to-back intervals with
    heating, and "any zone calling" counts the busiest zone in each interval. That is the
    least the zones can have overlapped, so it can come out a little low.
    """
    out: dict[date, DayRuntime] = defaultdict(DayRuntime)
    busiest: dict[datetime, int] = defaultdict(int)
    by_zone: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    for zone, start, secs in rows:
        by_zone[zone].append((start, secs))
        busiest[start] = max(busiest[start], secs)
    for zone, recs in by_zone.items():
        prev_start, prev_secs = None, 0
        per_day = defaultdict(int)
        for start, secs in sorted(recs):
            per_day[local_date(start, tz)] += 1
            day = out[local_date(start, tz)]
            day.per_zone_min[zone] = day.per_zone_min.get(zone, 0.0) + secs / 60
            if secs > 0 and not (prev_secs > 0 and prev_start == start - QUARTER):
                day.per_zone_count[zone] = day.per_zone_count.get(zone, 0) + 1
            prev_start, prev_secs = start, secs
        for d, n in per_day.items():
            out[d].quarters = max(out[d].quarters, n)
    for start, secs in busiest.items():
        out[local_date(start, tz)].any_zone_min += secs / 60
    return dict(out)
