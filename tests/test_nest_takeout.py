import csv
import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from heatmon.config import Zone as ZoneCfg
from heatmon.db.models import DailySummary, ThermostatHistory
from heatmon.processing.summaries import rebuild
from heatmon.sources.nest_takeout import import_takeout

S = "Takeout/Nest/nestgraph/STRUCTURE_1"


def line(obj) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerow([json.dumps(obj)])
    return buf.getvalue()


def make_zip(path, start: datetime, quarters: int):
    """Three thermostats: Hallway (room name), Master Bath (label), and a Shop."""
    wheres = {"predefinedWheres": {"0": {"whereId": {"resourceId": "A_HALL"}, "label": {"literal": "Hallway"}},
                                   "1": {"whereId": {"resourceId": "A_BATH"}, "label": {"literal": "Bathroom"}}},
              "customWheres": {"0": {"whereId": {"resourceId": "A_SHOP"}, "label": {"literal": "Shop"}}}}
    devices = {"SER_HALL": ("A_HALL", None), "SER_BATH": ("A_BATH", "Master Bath"), "SER_SHOP": ("A_SHOP", None)}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{S}/structuredata/nest.trait.located.LocatedAnnotationsTrait.jsonl", line({"payload": wheres}))
        for i, (serial, (where, label)) in enumerate(devices.items()):
            d = f"{S}/DEVICE_{i:X}/devicedata"
            z.writestr(f"{d}/nest.trait.product.DeviceIdentityTrait.jsonl", line({"payload": {"serialNumber": serial}}))
            z.writestr(f"{d}/nest.trait.located.DeviceLocatedSettingsTrait.jsonl",
                       line({"payload": {"whereAnnotationRid": {"resourceId": where}}}))
            if label:
                z.writestr(f"{d}/weave.trait.description.LabelSettingsTrait.jsonl", line({"payload": {"label": label}}))
            recs = "".join(line({
                "device_id": serial, "interval_start": (start + timedelta(minutes=15 * q)).isoformat().replace("+00:00", "Z"),
                "heating_time": 600 if q % 4 in (1, 2) else 0, "indoor_temp": 20.0, "heating_target": 20.5,
                "indoor_humidity": 40.0}) for q in range(quarters))
            z.writestr(f"Takeout/Nest/thermostats/{serial}/{start:%Y}/{start:%m}/HvacRuntime.jsonl", recs)


def test_import_matches_rooms_and_labels_and_skips_unknown(cfg, sessions, tmp_path):
    cfg.zones.append(ZoneCfg(name="Master Bath", retired=date(2026, 10, 27)))
    from heatmon.db.session import sync_zones
    sync_zones(sessions, cfg.zones)
    zpath = tmp_path / "takeout.zip"
    make_zip(zpath, datetime(2026, 1, 10, 8, tzinfo=UTC), 96 * 2)  # two whole Pacific days
    report = import_takeout(cfg, sessions, zpath)
    assert report["SER_HALL"]["zone"] == "Hallway" and report["SER_BATH"]["zone"] == "Master Bath"
    assert report["SER_SHOP"]["zone"] is None and report["SER_SHOP"]["added"] == 0
    assert report["SER_HALL"]["added"] == 192
    assert import_takeout(cfg, sessions, zpath)["SER_HALL"]["added"] == 0  # rerun adds nothing

    rebuild(cfg, sessions, date(2026, 1, 10))
    with sessions() as db:
        day = db.get(DailySummary, date(2026, 1, 10))
    # Each zone heats 10 + 10 minutes in every hour: 480 minutes a day, in 24 calls.
    assert day.runtime_source == "history"
    assert json.loads(day.per_zone_call_min_json) == {"Hallway": 480.0, "Master Bath": 480.0}
    assert json.loads(day.per_zone_call_count_json) == {"Hallway": 24, "Master Bath": 24}
    assert day.any_zone_call_min == 480.0 and day.total_zone_call_min == 960.0


def test_retired_zone_history_stops_at_retirement(cfg, sessions, tmp_path):
    cfg.zones.append(ZoneCfg(name="Master Bath", retired=date(2026, 1, 11)))
    from heatmon.db.session import sync_zones
    sync_zones(sessions, cfg.zones)
    zpath = tmp_path / "takeout.zip"
    make_zip(zpath, datetime(2026, 1, 10, 8, tzinfo=UTC), 96 * 2)
    report = import_takeout(cfg, sessions, zpath)
    assert report["SER_BATH"]["records"] == 64  # 10 Jan 08:00 UTC until 11 Jan 00:00 UTC


def test_partial_history_day_is_not_used(cfg, sessions, tmp_path):
    zpath = tmp_path / "takeout.zip"
    make_zip(zpath, datetime(2026, 1, 10, 8, tzinfo=UTC), 40)  # 10 hours of one day
    import_takeout(cfg, sessions, zpath)
    rebuild(cfg, sessions, date(2026, 1, 10))
    with sessions() as db:
        day = db.get(DailySummary, date(2026, 1, 10))
        assert db.scalar(select(func.count()).select_from(ThermostatHistory)) == 40
    assert day.runtime_source is None and day.total_zone_call_min == 0
