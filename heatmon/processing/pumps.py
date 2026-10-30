"""Turn pump switches into runs, and work out hot water use and cost per day."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from heatmon.processing.days import local_date
from heatmon.processing.zone_calls import minutes_on_day

PROPANE_BTU_PER_GAL = 91_500


@dataclass
class Run:
    pump: str
    start: datetime
    end: datetime
    open: bool = False  # still running at the last contact with the Pi
    gap: bool = False  # contact was lost, so the start or end time is a guess

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def derive_runs(events: list[tuple[str, datetime, bool, bool]], until: datetime) -> list[Run]:
    """Events are (pump, time, on, gap) in time order. A run still on at the end lasts until
    `until`, the last time the Pi was heard from."""
    started: dict[str, tuple[datetime, bool]] = {}
    runs = []
    for pump, ts, on, gap in events:
        if on and pump not in started:
            started[pump] = (ts, gap)
        elif not on and pump in started:
            start, start_gap = started.pop(pump)
            runs.append(Run(pump, start, ts, gap=gap or start_gap))
    runs += [Run(p, s, max(s, until), open=True, gap=g) for p, (s, g) in started.items()]
    return sorted(runs, key=lambda r: r.start)


def gallons(minutes: float, input_btu_h: float) -> float:
    """Propane burned in `minutes` at full fire."""
    return minutes / 60 * input_btu_h / PROPANE_BTU_PER_GAL


def daily_pumps(runs: list[Run], days: list[date], tz, input_btu_h: float,
                price: float | None) -> list[dict]:
    """Minutes each pump ran per local day, plus hot water cycles, gallons and cost.
    A cycle counts on the day it started."""
    by_pump = defaultdict(list)
    for r in runs:
        by_pump[r.pump].append((r.start, r.end))
    out = []
    for d in days:
        dhw_min = minutes_on_day(by_pump["dhw"], d, tz)
        gal = gallons(dhw_min, input_btu_h)
        out.append({
            "date": d.isoformat(),
            "boiler_pump_min": round(minutes_on_day(by_pump["boiler"], d, tz), 1),
            "system_pump_min": round(minutes_on_day(by_pump["system"], d, tz), 1),
            "dhw_min": round(dhw_min, 1),
            "dhw_cycles": sum(1 for r in runs if r.pump == "dhw" and local_date(r.start, tz) == d),
            "dhw_gal_est": round(gal, 2),
            "dhw_cost_est": round(gal * price, 2) if price is not None else None,
        })
    return out
