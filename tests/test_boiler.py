import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from heatmon.alerts import AlertSettings, AlertsJob, boiler_conditions, boiler_day_conditions
from heatmon.db.models import (BoilerCounters, BoilerCycle, BoilerParams, BoilerSample, DailySummary, Event)
from heatmon.db.session import insert_ignore
from heatmon.processing import boiler as bp
from heatmon.processing.boiler import STATUS_COUNT, STATUS_START, Status, Tracker
from heatmon.sources import boiler as src
from heatmon.web import data

T0 = datetime(2026, 11, 16, 7, 0, tzinfo=UTC)
TZ = ZoneInfo("America/Los_Angeles")


def enc(f: float) -> int:
    return round((f - 932) * 25) & 0xFFFF


def raw(flame=False, rate=0.0, mode="idle", sh=False, dhw=False, state=None, outlet=106.0, inlet=101.0,
        system=101.0, tank=120.0, dhw_sp=120.0) -> list[int]:
    r = [0] * STATUS_COUNT

    def put(n, v):
        r[n - STATUS_START] = v
    for n, f in {62: outlet, 63: 102.0, 70: tank, 73: inlet, 75: 30.0, 77: system, 142: 101.7, 145: dhw_sp,
                 146: 101.7 if flame else 32.0}.items():
        put(n, enc(f))
    put(84, state if state is not None else (9 if flame else 4))
    put(90, int(flame))
    put(131, int(rate * 100))
    put(138, {"dhw": 1, "sh": 4}.get(mode, 8))
    put(118, int(sh))
    put(139, (16 if sh else 0) | (2 if dhw else 0))
    return r


def feed(tracker: Tracker, steps: list[tuple[int, list[int]]], start: datetime = T0):
    """steps: (seconds to repeat, raw) read every 2 s."""
    minutes, cycles, t = [], [], start
    for seconds, r in steps:
        for _ in range(seconds // 2):
            m, c = tracker.add(Status(t, r))
            minutes += m
            cycles += c
            t += timedelta(seconds=2)
    return minutes, cycles, t


# ---------- Modbus ----------

def test_only_reads_are_ever_sent():
    req = src.read_request(62, 121)
    assert req[0] == 1 and req[1] == 3  # slave 1, read holding registers
    assert src.crc16(req) == 0  # a frame with its CRC appended checks to zero


def frame(values: list[int]) -> bytes:
    body = bytes([1, 3, 2 * len(values)]) + b"".join(v.to_bytes(2, "big") for v in values)
    return body + src.crc16(body).to_bytes(2, "little")


def test_parse_response():
    assert src.parse_response(frame([1, 2, 65535]), 3) == [1, 2, 65535]
    with pytest.raises(IOError, match="bad or short"):
        src.parse_response(frame([1, 2])[:-1] + b"\x00", 2)
    with pytest.raises(IOError, match="wrong length"):
        src.parse_response(frame([1, 2]), 3)


def test_temp_f():
    assert bp.temp_f(45237) == 120.0
    assert bp.temp_f(42986) == 30.0
    assert bp.temp_f(0x8000) is None


# ---------- cycles and minutes ----------

def test_cycles_and_end_reasons():
    tracker = Tracker(110_000)
    _, cycles, t = feed(tracker, [
        (10, raw()),
        (600, raw(flame=True, rate=100, mode="dhw", dhw=True)),
        (4, raw(mode="dhw")),  # call ended, then flame off
        (60, raw(sh=True)),
        (240, raw(flame=True, rate=25, mode="sh", sh=True)),
        (4, raw(sh=True, mode="sh")),  # flame off with the zone still calling
    ])
    assert [(c["mode"], c["end_reason"], c["partial"]) for c in cycles] == [
        ("dhw", "demand_ended", False), ("sh", "setpoint_met", False)]
    dhw, sh = cycles
    assert dhw["avg_rate_pct"] == 100
    assert dhw["btu"] == pytest.approx(110_000 * 598 / 3600, rel=0.01)  # the first read has no interval yet
    assert sh["min_fire_s"] == pytest.approx(240, abs=2)
    row = BoilerCycle(**sh)
    assert bp.is_short(row)
    assert not bp.is_short(row, short_min=3)


def test_gap_closes_a_cycle_and_marks_the_next_partial():
    tracker = Tracker(110_000)
    _, cycles, t = feed(tracker, [(60, raw(flame=True, rate=50, mode="sh", sh=True))])
    _, more, _ = feed(tracker, [(60, raw(flame=True, rate=50, mode="sh", sh=True)), (4, raw())],
                      start=t + timedelta(minutes=5))
    assert [(c["end_reason"], c["partial"]) for c in cycles + more] == [("gap", True), ("demand_ended", True)]


def test_minutes_average_and_count_flame():
    tracker = Tracker(110_000)
    minutes, _, _ = feed(tracker, [(30, raw()), (30, raw(flame=True, rate=50, mode="sh", sh=True, outlet=110.0)),
                                   (2, raw())])
    m = minutes[0]
    assert m["ts"] == T0 and m["samples"] == 30
    assert m["flame_s"] == pytest.approx(30)
    assert m["outlet_f"] == pytest.approx(108.0)
    assert len(json.loads(m["raw_json"])) == STATUS_COUNT


# ---------- parameters ----------

def params(**changes) -> dict[int, int]:
    values = {r: 0 for start, n in bp.PARAM_BLOCKS for r in range(start, start + n)}
    values.update({1302: 1, 1303: 16551, 1304: 1, 1305: 100, 1306: 1, 1307: 90})
    values.update({int(k[1:]): v for k, v in changes.items()})
    return values


def test_params_saved_on_change_with_an_event(sessions):
    src.store_params(sessions, T0, params())
    src.store_params(sessions, T0 + timedelta(hours=1), params(r1303=16552))  # counters only
    src.store_params(sessions, T0 + timedelta(hours=2), params(r1303=16552, r1045=47111))
    with sessions() as db:
        assert len(db.scalars(select(BoilerParams)).all()) == 2
        counters = db.scalars(select(BoilerCounters).order_by(BoilerCounters.ts)).all()
        events = db.scalars(select(Event).where(Event.title.like("Boiler settings%"))).all()
    assert [c.power_on_h for c in counters] == [65536 + 16551, 65536 + 16552, 65536 + 16552]
    assert [e.details for e in events] == ["r1045: 0 → 47111"]


def test_setting_changes_name_known_settings():
    # The owner's test on 2026-11-22: SH1 offset 11°F → 10°F and anti-cycling 9:00 → 10:00.
    old = params(r1157=5400, r1191=275, r1000=8, r1272=1800, r1278=250)
    new = params(r1157=6000, r1191=250, r1000=9, r1272=1800, r1278=250)
    assert bp.setting_changes(old, new) == ["SH1 offset: 11°F → 10°F", "Anti-cycling time: 9:00 → 10:00",
                                            "r1000: 8 → 9"]
    assert bp.setting_changes(new, params(r1157=6000, r1191=250, r1000=9, r1272=2400, r1278=250)) == \
        ["Ramp delay step 1: 25%, 3:00 → 25%, 4:00"]


def test_latest_settings_as_shown_on_the_boiler(sessions):
    assert src.latest_settings(sessions) is None
    # Values read 2026-11-22 while the owner had SH1 offset at 11°F and anti-cycling at 9:00.
    live = dict(r1163=42362, r1164=44987, r1165=43862, r1166=44487, r1167=43862, r1168=250, r1191=275, r1192=500,
                r1157=5400, r1181=45987, r1272=1800, r1273=1800, r1274=1800, r1275=3000, r1276=3000, r1277=600,
                r1278=250, r1279=300, r1280=350, r1281=350, r1282=400, r1283=600)
    src.store_params(sessions, T0, params(**live))
    ts, rows = src.latest_settings(sessions)
    assert ts == T0
    assert rows == [
        ("SH1 low outdoor temperature", "5°F"), ("SH1 set point at low outdoor temp", "110°F"),
        ("SH1 high outdoor temperature", "65°F"), ("SH1 set point at high outdoor temp", "90°F"),
        ("SH1 outdoor air shutdown", "65°F"), ("SH1 outdoor shutdown differential", "10°F"),
        ("SH1 offset", "11°F"), ("SH1 differential", "20°F"), ("Anti-cycling time", "9:00"),
        ("DHW boiler set point", "150°F"),
        ("Ramp delay step 1", "25%, 3:00"), ("Ramp delay step 2", "30%, 3:00"), ("Ramp delay step 3", "35%, 3:00"),
        ("Ramp delay step 4", "35%, 5:00"), ("Ramp delay step 5", "40%, 5:00"), ("Ramp delay step 6", "60%, 1:00")]


def test_lifetime_uses_latest_counters(cfg, sessions):
    c = data.Ctx(cfg, sessions, date(2026, 11, 16), date(2026, 11, 16))
    assert data.boiler_lifetime(c) is None
    src.store_params(sessions, T0, params(r1309=4000))
    src.store_params(sessions, T0 + timedelta(hours=1), params(r1309=4001, r1313=500, r1317=200))
    life = data.boiler_lifetime(c)
    assert (life["sh_cycles"], life["dhw_cycles"], life["burner_h"]) == (500, 200, 4001)
    assert life["avg_cycle_min"] == 4001 * 60 / (65536 + 100)


def test_boiler_pump_now(cfg, sessions):
    c = data.Ctx(cfg, sessions, date(2026, 11, 16), date(2026, 11, 16))
    assert data.boiler_pump_now(c) is None
    t = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=4)
    base = {"samples": 30, "state": 4, "mode": "idle", "flame_s": 0.0, "btu": 0.0, "sh_demand": False,
            "dhw_demand": False}
    insert_ignore(sessions, BoilerSample, [{**base, "ts": t + timedelta(minutes=i), "pump_pct": pct}
                                           for i, pct in enumerate([35.0, 0.0, 35.0, 35.0])])
    now = data.boiler_pump_now(c)
    assert (now["on"], now["pct"], now["since"], now["current"]) == (True, 35.0, c.local(t + timedelta(minutes=2)), True)

    with sessions() as db:
        db.query(BoilerSample).delete()
        db.commit()
    old = t - timedelta(minutes=10)
    insert_ignore(sessions, BoilerSample, [{**base, "ts": old + timedelta(minutes=i), "pump_pct": 100.0}
                                           for i in range(2)])
    now = data.boiler_pump_now(c)  # never seen off, and the latest read is old
    assert (now["on"], now["since"], now["current"]) == (True, c.local(old), False)

# ---------- the source and the CSV import ----------

class FakePort:
    def __init__(self, status: list[int]):
        self.status, self.request, self.closed = status, b"", False

    def reset_input_buffer(self):
        pass

    def write(self, b):
        assert b[1] == 3
        self.request, self.reply = b, None

    def read(self, n):
        if self.reply is None:
            start, count = int.from_bytes(self.request[2:4], "big"), int.from_bytes(self.request[4:6], "big")
            self.reply = frame(self.status if start == STATUS_START else [0] * count)
        out, self.reply = self.reply[:n], self.reply[n:]
        return out

    def close(self):
        self.closed = True


async def test_source_reads_and_stores(cfg, sessions, monkeypatch):
    source = src.BoilerSource(cfg, sessions)
    assert await source.poll() is None  # off until turned on
    port = FakePort(raw())
    monkeypatch.setattr(src, "open_port", lambda url: port)
    monkeypatch.setattr(src.time, "sleep", lambda s: None)
    src.save_settings(sessions, src.BoilerSettings(enabled=True, port="rfc2217://pi:2217"))
    assert await source.poll() == 1  # the parameter read
    with sessions() as db:
        assert db.scalars(select(BoilerCounters)).first() is not None
    src.save_settings(sessions, src.BoilerSettings(enabled=False, port="rfc2217://pi:2217"))
    assert await source.poll() is None and port.closed


def test_import_csv_is_repeatable(cfg, sessions, tmp_path):
    status = tmp_path / "boiler_status.csv"
    lines = ["time," + ",".join(f"r{r}" for r in range(STATUS_START, STATUS_START + STATUS_COUNT))]
    t = T0
    for seconds, r in [(20, raw()), (300, raw(flame=True, rate=40, mode="sh", sh=True)), (100, raw())]:
        for _ in range(seconds // 2):
            lines.append(t.astimezone(TZ).isoformat() + "," + ",".join(map(str, r)))
            t += timedelta(seconds=2)
    status.write_text("\n".join(lines) + "\n")
    assert src.import_csv(cfg, sessions, status) == (7, 1, 0)  # 420 s of reads
    assert src.import_csv(cfg, sessions, status) == (0, 0, 0)


# ---------- page data ----------

def store_run(sessions, start, minutes, mode="sh", reason="demand_ended", rate=25.0, **sample):
    tracker = Tracker(110_000)
    demand = {"sh": mode == "sh", "dhw": mode == "dhw"}
    rows, cycles, t = feed(tracker, [(2, raw()), (int(minutes * 60), raw(flame=True, rate=rate, mode=mode, **demand, **sample)),
                                     (64, raw(mode=mode, **(demand if reason == "setpoint_met" else {})))],
                           start=start)
    insert_ignore(sessions, BoilerSample, rows + tracker.flush())
    insert_ignore(sessions, BoilerCycle, cycles)


def test_boiler_daily(cfg, sessions):
    store_run(sessions, T0, 60)  # 23:00 local on Nov 15
    store_run(sessions, T0 + timedelta(hours=3), 5, reason="setpoint_met")
    store_run(sessions, T0 + timedelta(hours=4), 20, mode="dhw", rate=100)
    days = data.boiler_daily(data.Ctx(cfg, sessions, date(2026, 11, 15), date(2026, 11, 16)))
    assert [(d["date"], d["sh_cycles"], d["dhw_cycles"], d["short_cycles"]) for d in days] == [
        ("2026-11-15", 1, 0, 0), ("2026-11-16", 1, 1, 1)]
    assert days[1]["dhw_gal_est"] == pytest.approx(110_000 / 3 / 91_500, rel=0.03)


# ---------- alerts ----------

def test_short_cycling_alert(sessions):
    for i in range(3):
        store_run(sessions, T0 + timedelta(minutes=20 * i), 4, reason="setpoint_met")
    now = T0 + timedelta(hours=1)
    found = boiler_conditions(sessions, AlertSettings(), now, TZ)
    assert "3 heating burns" in found["boiler_short", "boiler"]
    assert ("boiler_short", "boiler") not in boiler_conditions(sessions, AlertSettings(boiler_short_count=4), now, TZ)


def test_sustained_readings_alert(sessions):
    store_run(sessions, T0, 40, system=125.0, inlet=135.0)
    found = boiler_conditions(sessions, AlertSettings(), T0 + timedelta(minutes=39), TZ)
    assert {k for k, _ in found} == {"boiler_slab", "boiler_return"}
    assert boiler_conditions(sessions, AlertSettings(), T0 + timedelta(minutes=3), TZ) == {}


def test_settings_change_alert(sessions):
    src.store_params(sessions, T0, params())
    assert boiler_conditions(sessions, AlertSettings(), T0, TZ) == {}  # the first read isn't a change
    src.store_params(sessions, T0 + timedelta(hours=1), params(r1045=1))
    assert ("boiler_settings", "boiler") in boiler_conditions(sessions, AlertSettings(), T0 + timedelta(hours=2), TZ)
    assert boiler_conditions(sessions, AlertSettings(), T0 + timedelta(hours=26), TZ) == {}


def full_day(sessions, day: date, burn_h: float, dhw_min: float):
    """A day of boiler minutes with `burn_h` of heating and `dhw_min` of hot water."""
    lo = datetime(day.year, day.month, day.day, tzinfo=TZ).astimezone(UTC)
    rows = []
    for i in range(1440):
        flame, mode = (60.0, "sh") if i < burn_h * 60 else (60.0, "dhw") if i < burn_h * 60 + dhw_min else (0.0, "idle")
        rows.append({"ts": lo + timedelta(minutes=i), "samples": 30, "state": 9 if flame else 4, "mode": mode,
                     "flame_s": flame, "btu": 500.0 if flame else 0.0, "sh_demand": False, "dhw_demand": False})
    insert_ignore(sessions, BoilerSample, rows)


def test_runtime_and_hot_water_day_alerts(sessions):
    day = date(2026, 12, 1)
    for i in range(1, 16):
        d = day - timedelta(days=i)
        hdd = 20 + i % 5
        insert_ignore(sessions, DailySummary, [{"date": d, "hdd": hdd, "weather_hours": 24, "open_meteo_hours": 0,
                                                "any_zone_call_min": 0, "total_zone_call_min": 0,
                                                "zone_call_count": 0, "per_zone_call_min_json": "{}",
                                                "per_zone_call_count_json": "{}"}])
        full_day(sessions, d, burn_h=hdd * 0.2, dhw_min=30)
    insert_ignore(sessions, DailySummary, [{"date": day, "hdd": 22, "weather_hours": 24, "open_meteo_hours": 0,
                                            "any_zone_call_min": 0, "total_zone_call_min": 0, "zone_call_count": 0,
                                            "per_zone_call_min_json": "{}", "per_zone_call_count_json": "{}"}])
    full_day(sessions, day, burn_h=8, dhw_min=90)  # 4.4 h expected at 22 HDD
    found = boiler_day_conditions(sessions, AlertSettings(), day, TZ)
    assert "8.0 hours" in found["boiler_runtime", "boiler"] and "4.4 hours" in found["boiler_runtime", "boiler"]
    assert "90 minutes" in found["boiler_dhw", "boiler"]
    # A setting change starts a new period with too few days to compare against.
    with sessions.begin() as db:
        db.add(Event(ts=datetime(2026, 11, 28, 12, tzinfo=UTC), type="setting_change", title="x"))
    assert ("boiler_runtime", "boiler") not in boiler_day_conditions(sessions, AlertSettings(), day, TZ)


async def test_alerts_job_skips_boiler_when_off(cfg, sessions):
    for i in range(3):
        store_run(sessions, datetime.now(UTC) - timedelta(minutes=60 - 20 * i), 4, reason="setpoint_met")
    job = AlertsJob(cfg, sessions, {"boiler"}, send=None)
    await job.poll()
    from heatmon.db.models import Alert
    with sessions() as db:
        assert db.scalars(select(Alert)).all() == []
    src.save_settings(sessions, src.BoilerSettings(enabled=True, port="x"))
    await job.poll()
    with sessions() as db:
        assert [a.kind for a in db.scalars(select(Alert))] == ["boiler_short"]
