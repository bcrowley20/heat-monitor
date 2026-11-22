"""Turn boiler status reads into one-minute samples and burner cycles.

Register meanings come from watching the Knight's front service port (see
boiler/boiler_log.py). The status block is holding registers 62 to 182.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

from heatmon.processing.pumps import PROPANE_BTU_PER_GAL

STATUS_START, STATUS_COUNT = 62, 121
# The parameter chunks the Lochinvar app reads. 124 at 1152 avoids overlapping 1276.
PARAM_BLOCKS = [(1000, 16), (1016, 42), (1058, 10), (1068, 84), (1152, 124), (1276, 9), (1285, 12), (1297, 123)]
# Lifetime counters, each a high and low word. They change on their own, so they are not settings.
COUNTER_REGS = range(1300, 1320)
COUNTERS = {"power_on_h": 1302, "burner_starts": 1304, "ignitions": 1306, "burner_h": 1308,
            "burner_h_2": 1310, "sh_cycles": 1312, "dhw_cycles": 1316}
STATES = {4: "standby", 6: "prepurge", 7: "ignition", 9: "running", 10: "postpurge"}
MODES = {1: "dhw", 4: "sh"}  # register 138; anything else is idle
MIN_FIRE_PCT = 25.5  # the boiler's lowest rate reads 25%
MAX_STEP_S = 10  # reads further apart than this are a gap, not a run of the same state


# Settings in the parameter block that were matched against values read off the boiler's
# display (2026-11-22). Kinds: temp is °F like the status temps, diff is °F x 25, tenths is
# tenths of a second, permille is tenths of a percent.
SETTINGS = [
    ("SH1 low outdoor temperature", 1163, "temp"), ("SH1 set point at low outdoor temp", 1164, "temp"),
    ("SH1 high outdoor temperature", 1165, "temp"), ("SH1 set point at high outdoor temp", 1166, "temp"),
    ("SH1 outdoor air shutdown", 1167, "temp"), ("SH1 outdoor shutdown differential", 1168, "diff"),
    ("SH1 offset", 1191, "diff"), ("SH1 differential", 1192, "diff"),
    ("Anti-cycling time", 1157, "tenths"), ("DHW boiler set point", 1181, "temp"),
    *((f"Ramp delay step {i}", (1277 + i, 1271 + i), "step") for i in range(1, 7)),
]


def show_settings(values: dict[int, int]) -> list[tuple[str, str]]:
    """The known settings as (name, value as shown on the boiler)."""
    def mins(tenths: int) -> str:
        return f"{tenths // 600}:{tenths // 10 % 60:02d}"

    out = []
    for name, reg, kind in SETTINGS:
        if kind == "step":
            out.append((name, f"{values[reg[0]] / 10:g}%, {mins(values[reg[1]])}"))
        else:
            raw = values[reg]
            out.append((name, f"{temp_f(raw):g}°F" if kind == "temp" else f"{raw / 25:g}°F" if kind == "diff"
                        else mins(raw)))
    return out


def temp_f(raw: int) -> float | None:
    """Temperatures are signed 16-bit: °F = raw / 25 + 932. 0x8000 means no sensor."""
    if raw == 0x8000:
        return None
    signed = raw - 0x10000 if raw & 0x8000 else raw
    return round(signed / 25 + 932, 1)


@dataclass
class Status:
    """One read of the status block."""

    ts: datetime
    raw: list[int]

    def reg(self, n: int) -> int:
        return self.raw[n - STATUS_START]

    def temp(self, n: int) -> float | None:
        return temp_f(self.reg(n))

    @property
    def flame(self) -> bool:
        return self.reg(90) == 1

    @property
    def state(self) -> int:
        return self.reg(84)

    @property
    def mode(self) -> str:
        return MODES.get(self.reg(138), "idle")

    @property
    def rate_pct(self) -> float:
        return self.reg(131) / 100

    @property
    def sh_demand(self) -> bool:
        return self.reg(118) == 1 or bool(self.reg(139) & 16)

    @property
    def dhw_demand(self) -> bool:
        return bool(self.reg(139) & 2)

    def values(self) -> dict[str, float | None]:
        return {"outlet_f": self.temp(62), "inlet_f": self.temp(73), "system_f": self.temp(77),
                "flue_f": self.temp(63), "tank_f": self.temp(70), "outdoor_f": self.temp(75),
                "target_f": self.temp(146), "sh_setpoint_f": self.temp(142), "dhw_setpoint_f": self.temp(145),
                "rate_pct": self.rate_pct, "fan_rpm": float(self.reg(110)), "pump_pct": self.reg(132) / 100}


@dataclass
class _Minute:
    ts: datetime
    n: int = 0
    sums: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    flame_s: float = 0
    btu: float = 0
    last: Status | None = None

    def row(self) -> dict:
        s = self.last
        avg = {k: round(self.sums[k] / self.counts[k], 2) for k in self.sums}
        return {"ts": self.ts, "samples": self.n, **{k: avg.get(k) for k in s.values()},
                "state": s.state, "mode": s.mode, "flame_s": round(self.flame_s, 1), "btu": round(self.btu, 1),
                "sh_demand": s.sh_demand, "dhw_demand": s.dhw_demand, "raw_json": json.dumps(s.raw)}


@dataclass
class _Cycle:
    start: datetime
    partial: bool
    modes: list[str] = field(default_factory=list)
    burn_s: float = 0
    rate_s: float = 0
    min_fire_s: float = 0
    btu: float = 0
    max_outlet: float | None = None
    inlet: list[float] = field(default_factory=list)
    flue: list[float] = field(default_factory=list)

    def row(self, end: datetime, reason: str) -> dict:
        modes = set(self.modes) - {"idle"}
        return {"start_ts": self.start, "end_ts": end, "end_reason": reason, "partial": self.partial or reason == "gap",
                "mode": modes.pop() if len(modes) == 1 else ("mixed" if modes else "sh"),
                "avg_rate_pct": round(self.rate_s / self.burn_s, 1) if self.burn_s else None,
                "min_fire_s": round(self.min_fire_s), "btu": round(self.btu),
                "max_outlet_f": self.max_outlet,
                "avg_inlet_f": round(sum(self.inlet) / len(self.inlet), 1) if self.inlet else None,
                "avg_flue_f": round(sum(self.flue) / len(self.flue), 1) if self.flue else None}


class Tracker:
    """Feed it reads in time order. It hands back finished minutes and finished burner
    cycles (flame on to flame off). A cycle whose start wasn't seen is marked partial."""

    def __init__(self, input_btu_h: float):
        self.input_btu_h = input_btu_h
        self.prev: Status | None = None
        self.minute: _Minute | None = None
        self.cycle: _Cycle | None = None

    def add(self, s: Status) -> tuple[list[dict], list[dict]]:
        minutes, cycles = [], []
        step = (s.ts - self.prev.ts).total_seconds() if self.prev else None
        gap = step is not None and not 0 < step <= MAX_STEP_S
        dt = 0.0 if step is None or gap else step
        if gap and self.cycle:
            cycles.append(self.cycle.row(self.prev.ts, "gap"))
            self.cycle = None

        key = s.ts.replace(second=0, microsecond=0)
        if self.minute and self.minute.ts != key:
            minutes.append(self.minute.row())
            self.minute = None
        m = self.minute = self.minute or _Minute(key)
        m.n += 1
        for k, v in s.values().items():
            if v is not None:
                m.sums[k] = m.sums.get(k, 0) + v
                m.counts[k] = m.counts.get(k, 0) + 1
        btu = s.rate_pct / 100 * self.input_btu_h * dt / 3600 if s.flame else 0
        m.flame_s += dt if s.flame else 0
        m.btu += btu
        m.last = s

        if s.flame:
            c = self.cycle = self.cycle or _Cycle(s.ts, partial=self.prev is None or gap)
            c.modes.append(s.mode)
            c.burn_s += dt
            c.rate_s += s.rate_pct * dt
            c.min_fire_s += dt if s.rate_pct <= MIN_FIRE_PCT else 0
            c.btu += btu
            v = s.values()
            if v["outlet_f"] is not None:
                c.max_outlet = max(c.max_outlet or v["outlet_f"], v["outlet_f"])
            if v["inlet_f"] is not None:
                c.inlet.append(v["inlet_f"])
            if v["flue_f"] is not None:
                c.flue.append(v["flue_f"])
        elif self.cycle:
            mode = next((x for x in reversed(self.cycle.modes) if x != "idle"), "sh")
            wanted = s.dhw_demand if mode == "dhw" else s.sh_demand
            cycles.append(self.cycle.row(s.ts, "setpoint_met" if wanted else "demand_ended"))
            self.cycle = None
        self.prev = s
        return minutes, cycles

    def flush(self) -> list[dict]:
        """The minute in progress, for the end of an import. An unfinished cycle is dropped."""
        rows = [self.minute.row()] if self.minute else []
        self.minute = None
        return rows


def counters(values: dict[int, int]) -> dict[str, int]:
    return {name: values[r] * 65536 + values[r + 1] for name, r in COUNTERS.items()}


def settings_of(values: dict[int, int]) -> dict[int, int]:
    return {r: v for r, v in values.items() if r not in COUNTER_REGS}


def setting_changes(old: dict[int, int], new: dict[int, int]) -> list[str]:
    return [f"r{r}: {old.get(r)} → {new.get(r)}" for r in sorted(set(old) | set(new)) if old.get(r) != new.get(r)]


def gallons(btu: float, fuel_factor: float) -> float:
    return btu * fuel_factor / PROPANE_BTU_PER_GAL


CONDENSING_F = 130  # return water below this lets flue gas condense
SHORT_CYCLE_MIN = 10


def daily_totals(samples, cycles, tz, short_min: float = SHORT_CYCLE_MIN) -> dict:
    """Burner time, fuel, cycles and health figures per local day. `samples` and `cycles`
    are BoilerSample and BoilerCycle rows. Cycles count on the day they start."""
    out: dict = {}

    def day(ts):
        d = ts.astimezone(tz).date()
        return out.setdefault(d, {"sh_burn_h": 0.0, "dhw_burn_min": 0.0, "sh_btu": 0.0, "dhw_btu": 0.0,
                                  "firing_min": 0.0, "condensing_min": 0.0, "sh_cycles": 0, "dhw_cycles": 0,
                                  "short_cycles": 0, "min_fire_h": 0.0, "sh_cycle_min": []})

    for s in samples:
        d = day(s.ts)
        if s.flame_s <= 0:
            continue
        if s.mode == "dhw":
            d["dhw_burn_min"] += s.flame_s / 60
            d["dhw_btu"] += s.btu
        else:
            d["sh_burn_h"] += s.flame_s / 3600
            d["sh_btu"] += s.btu
        d["firing_min"] += s.flame_s / 60
        if s.inlet_f is not None and s.inlet_f < CONDENSING_F:
            d["condensing_min"] += s.flame_s / 60
    for c in cycles:
        d = day(c.start_ts)
        minutes = (c.end_ts - c.start_ts).total_seconds() / 60
        d["min_fire_h"] += c.min_fire_s / 3600
        if c.mode == "dhw":
            d["dhw_cycles"] += 1
        else:
            d["sh_cycles"] += 1
            d["sh_cycle_min"].append(minutes)
            if is_short(c, short_min):
                d["short_cycles"] += 1
    return out


def is_short(c, short_min: float = SHORT_CYCLE_MIN) -> bool:
    """A heating burn that stopped on setpoint met, with a zone still calling, soon after it lit."""
    return (c.mode != "dhw" and c.end_reason == "setpoint_met" and not c.partial
            and (c.end_ts - c.start_ts).total_seconds() < short_min * 60)
