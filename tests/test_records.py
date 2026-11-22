from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from heatmon import records
from heatmon.db.models import Event, PropaneDelivery, TankReading
from heatmon.db.session import insert_ignore
from heatmon.processing.summaries import rebuild

SPREADSHEET = '''﻿date,gallons,price_per_gal,total_cost,tank_pct_before,tank_pct_after,notes
11/17/17,428.4,$1.69 , $724.00 ,,79%,
2/27/2019,776.2,$1.89 ," $1,467.02 ",,81%,
,,,,,,
2025-10-30,440,,,,,cash
'''


def test_parse_spreadsheet_styles_and_blank_rows():
    rows, problems = records.parse_deliveries_csv(SPREADSHEET)
    assert problems == []
    assert [r["date"] for r in rows] == [date(2017, 11, 17), date(2019, 2, 27), date(2025, 10, 30)]
    assert rows[1]["total_cost"] == 1467.02 and rows[1]["tank_pct_after"] == 81 and rows[0]["price_per_gal"] == 1.69
    assert rows[2]["notes"] == "cash" and rows[2]["price_per_gal"] is None


def test_parse_reports_each_bad_line():
    text = "date,gallons,tank_pct_before,tank_pct_after\n13/45/25,abc,,\n,100,,\n2025-01-01,-5,90,80\n"
    rows, problems = records.parse_deliveries_csv(text)
    assert rows == []
    assert problems[0].startswith("Line 2: date '13/45/25' is not a date") and "gallons 'abc'" in problems[0]
    assert problems[1] == "Line 3: date is missing"
    assert "more than 0" in problems[2] and "higher than" in problems[2]
    assert records.parse_deliveries_csv("when,amount\n1/1/25,5\n")[1][0].startswith("The header row is missing")


def test_warnings_for_copied_rows_and_close_dates():
    rows, _ = records.parse_deliveries_csv(
        "date,gallons,total_cost\n3/10/25,634.3,1362.16\n3/11/26,634.3,1362.16\n10/30/25,440,963\n11/3/25,440,853\n")
    w = records.delivery_warnings(rows)
    assert any("2025-03-10 and 2026-03-11 have the same gallons and cost" in x for x in w)
    assert any("2025-10-30 and 2025-11-03 are less than a week apart" in x for x in w)


def test_import_twice_adds_once(sessions):
    rows, _ = records.parse_deliveries_csv(SPREADSHEET)
    assert records.save_invoices(sessions, rows) == (3, 0)
    assert records.save_invoices(sessions, rows) == (0, 3)


def test_detected_fill_dropped_when_invoice_covers_it(cfg, sessions):
    insert_ignore(sessions, TankReading, [
        {"ts": datetime(2026, 10, 10, 15, tzinfo=UTC), "level_pct": 30.0, "tank_size_gal": 1000},
        {"ts": datetime(2026, 10, 10, 21, tzinfo=UTC), "level_pct": 80.0, "tank_size_gal": 1000}])
    rebuild(cfg, sessions, date(2026, 10, 10))
    with sessions() as db:
        assert db.scalars(select(PropaneDelivery.source)).all() == ["detected"]
    records.save_invoices(sessions, [{"date": date(2026, 10, 12), "gallons": 500.0, "price_per_gal": None,
                                      "total_cost": None, "tank_pct_before": None, "tank_pct_after": 80.0,
                                      "notes": None}])
    rebuild(cfg, sessions, date(2026, 10, 10))
    with sessions() as db:
        assert db.scalars(select(PropaneDelivery.source)).all() == ["invoice"]


def test_events_save_edit_delete(sessions):
    ts = datetime(2026, 11, 2, 18, tzinfo=UTC)
    records.save_event(sessions, ts, "fireplace", "  Fire in the great room ", "")
    with sessions() as db:
        e = db.scalars(select(Event).where(Event.type == "fireplace")).one()
    assert (e.title, e.details) == ("Fire in the great room", None)
    records.save_event(sessions, ts, "away", "Away for the weekend", "back Sunday", e.id)
    with sessions() as db:
        assert db.get(Event, e.id).type == "away"
    records.delete_event(sessions, e.id)
    with sessions() as db:
        assert db.get(Event, e.id) is None
    with pytest.raises(ValueError):
        records.save_event(sessions, ts, "party", "x", None)


def test_settings_flatten_round_trip_and_diff():
    before = {"sh1_offset_f": 5, "ramp_delay": {"enabled": False}, "outdoor_reset": "unknown"}
    after = {"sh1_offset_f": 10, "ramp_delay": {"enabled": True, "steps": [[15, 5], [60, None]]},
             "outdoor_reset": {"low_outdoor_temp_f": 5}}
    assert records.unflatten(records.flatten(after)) == after
    assert records.parse_value("[[15, 5], [60, null]]") == [[15, 5], [60, None]]
    assert records.parse_value("10") == 10 and records.parse_value("confirm") == "confirm"
    diff = {d["setting"]: (d["before"], d["after"]) for d in records.settings_diff(before, after)}
    assert diff == {"sh1_offset_f": ("5", "10"), "ramp_delay.enabled": ("false", "true"),
                    "ramp_delay.steps": ("", "[[15, 5], [60, null]]"), "outdoor_reset": ("unknown", ""),
                    "outdoor_reset.low_outdoor_temp_f": ("", "5")}


def test_seeded_snapshots(sessions):
    from heatmon.db.models import BoilerSettingsSnapshot
    with sessions() as db:
        labels = db.scalars(select(BoilerSettingsSnapshot.label)).all()
    assert labels == ["before 2026-10-25", "2026-10-25 new settings"]


def test_price_worked_out_from_total_cost():
    rows, _ = records.parse_deliveries_csv("date,gallons,price_per_gal,total_cost\n1/5/25,400,,$1000.00\n")
    assert rows[0]["price_per_gal"] == 2.5


def test_editing_a_deleted_record_explains(sessions):
    with pytest.raises(ValueError, match="deleted"):
        records.save_event(sessions, datetime(2026, 11, 2, tzinfo=UTC), "note", "x", None, event_id=9999)
    with pytest.raises(ValueError, match="deleted"):
        records.save_snapshot(sessions, "x", None, {}, snapshot_id=9999)
