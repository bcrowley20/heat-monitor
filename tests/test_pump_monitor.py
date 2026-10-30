"""The Pi program's debounce and switch log, with fake pins and a fake clock."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("pump_monitor", Path(__file__).parents[1] / "pi" / "pump_monitor.py")
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)


class Fake:
    def __init__(self):
        self.t, self.pins = 100.0, {"boiler": False, "system": True, "dhw": False}

    def run(self, monitor, seconds):
        for _ in range(round(seconds / pm.SAMPLE_S)):
            self.t += pm.SAMPLE_S
            monitor.sample()


def make():
    f = Fake()
    return f, pm.Monitor(list(f.pins), lambda n: f.pins[n], clock=lambda: f.t, wall=lambda: 1_800_000_000 + f.t)


def test_start_records_each_pump():
    _, m = make()
    snap = m.snapshot(after=0, boot=m.boot_id)
    assert [(e["pump"], e["on"], e["start"]) for e in snap["events"]] == [
        ("boiler", False, True), ("system", True, True), ("dhw", False, True)]
    assert snap["pumps"]["system"]["on"] is True


def test_blip_is_ignored_and_change_is_timed_from_first_seen():
    f, m = make()
    f.pins["dhw"] = True
    f.run(m, 0.1)  # shorter than SETTLE_S
    f.pins["dhw"] = False
    f.run(m, 0.5)
    assert m.seq == 3
    f.pins["dhw"] = True
    seen_at = f.t + pm.SAMPLE_S
    f.run(m, 0.5)
    e = m.snapshot(after=3, boot=m.boot_id)["events"]
    assert len(e) == 1 and e[0]["on"] is True and abs(e[0]["mono"] - seen_at) < 1e-6


def test_wrong_boot_gets_everything_and_paging():
    f, m = make()
    for i in range(pm.PAGE):
        f.pins["dhw"] = not f.pins["dhw"]
        f.run(m, 0.3)
    assert len(m.snapshot(after=50, boot="old")["events"]) == pm.PAGE  # restart missed: start over
    snap = m.snapshot(after=0, boot=m.boot_id)
    assert snap["more"] and snap["events"][-1]["seq"] == pm.PAGE
    assert not m.snapshot(after=pm.PAGE, boot=m.boot_id)["more"]


def test_old_switches_drop_off(monkeypatch):
    monkeypatch.setattr(pm, "MAX_EVENTS", 5)
    f, m = make()
    for _ in range(4):
        f.pins["dhw"] = not f.pins["dhw"]
        f.run(m, 0.3)
    snap = m.snapshot(after=0, boot=m.boot_id)
    assert (snap["first_seq"], snap["last_seq"]) == (3, 7)
