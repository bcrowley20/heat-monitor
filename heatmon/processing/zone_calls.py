"""Turn thermostat readings into calls for heat, and measure call time per day."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from heatmon.processing.days import day_bounds


@dataclass
class Sample:
    ts: datetime
    hvac_status: str | None
    ambient_c: float | None
    setpoint_c: float | None


@dataclass
class Call:
    start_ts: datetime
    end_ts: datetime
    start_ambient_c: float | None
    end_ambient_c: float | None
    setpoint_c: float | None
    open: bool = False
    gap_closed: bool = False

    @property
    def duration_s(self) -> int:
        return int((self.end_ts - self.start_ts).total_seconds())


def _midpoint(a: datetime, b: datetime) -> datetime:
    return (a + (b - a) / 2).replace(microsecond=0)


def derive_calls(samples: list[Sample], interval_s: int, now: datetime) -> list[Call]:
    """Samples must be in time order and from one zone.

    A call starts at the first HEATING sample and ends at the first sample that isn't
    HEATING. Each change is placed halfway between the two samples around it. If the
    readings stop for more than 3 poll intervals during a call, including up to `now`,
    the call ends at the last reading and is flagged.
    """
    max_gap = timedelta(seconds=3 * interval_s)
    calls: list[Call] = []
    cur: dict | None = None
    prev: Sample | None = None
    for s in samples:
        gap_ok = prev is not None and s.ts - prev.ts <= max_gap
        if cur is not None and not gap_ok:
            calls.append(Call(**cur, end_ts=prev.ts, end_ambient_c=prev.ambient_c, gap_closed=True))
            cur = None
        heating = s.hvac_status == "HEATING"
        if heating and cur is None:
            start = _midpoint(prev.ts, s.ts) if gap_ok else s.ts
            cur = {"start_ts": start, "start_ambient_c": s.ambient_c, "setpoint_c": s.setpoint_c}
        elif not heating and cur is not None:
            calls.append(Call(**cur, end_ts=_midpoint(prev.ts, s.ts), end_ambient_c=s.ambient_c))
            cur = None
        prev = s
    if cur is not None:
        still_reporting = now - prev.ts <= max_gap
        calls.append(Call(**cur, end_ts=prev.ts, end_ambient_c=prev.ambient_c,
                          open=still_reporting, gap_closed=not still_reporting))
    return calls


def minutes_on_day(intervals: list[tuple[datetime, datetime]], day: date, tz) -> float:
    """Minutes of the local day covered by the intervals, counting overlaps once."""
    lo, hi = day_bounds(day, tz)
    utc = [(a.astimezone(UTC), b.astimezone(UTC)) for a, b in intervals]  # see day_bounds
    clipped = sorted((max(a, lo), min(b, hi)) for a, b in utc if b > lo and a < hi)
    total = timedelta()
    end = None
    for a, b in clipped:
        if end is None or a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return total.total_seconds() / 60
