from datetime import UTC, datetime

import httpx
import respx
from sqlalchemy import select

from heatmon.db.models import TankReading
from heatmon.sources.centriconnect import CentriConnectSource
from heatmon.sources.tank_csv import import_tank_csv
from tests.test_sources_centriconnect import URL, tank_response

CSV = """﻿Date,Weather Temp - F,Level,Usage,Battery,Solar,Signal
"10/28/2026, 10:00 AM",37,79%,0,∞,71%,90%
"10/28/2026, 03:59 AM",35,80%,0,∞,0%,90%
"10/27/2026, 10:00 PM",--,80%,--,∞,--,90%
"bad date",38,79%,0,∞,0%,90%
"""


def levels(sessions):
    with sessions() as db:
        return [(r.ts, r.level_pct) for r in db.scalars(select(TankReading).order_by(TankReading.ts))]


def test_imports_local_times_and_skips_repeats(cfg, sessions):
    added, skipped, problems = import_tank_csv(cfg, sessions, CSV)
    assert (added, skipped) == (3, 0)
    assert len(problems) == 1 and "line 5" in problems[0]
    assert levels(sessions) == [  # Pacific daylight time is UTC-7
        (datetime(2026, 10, 28, 5, 0, tzinfo=UTC), 80.0),
        (datetime(2026, 10, 28, 10, 59, tzinfo=UTC), 80.0),
        (datetime(2026, 10, 28, 17, 0, tzinfo=UTC), 79.0),
    ]
    with sessions() as db:
        assert db.get(TankReading, datetime(2026, 10, 28, 17, 0, tzinfo=UTC)).remaining_gal == 790.0
    assert import_tank_csv(cfg, sessions, CSV)[:2] == (0, 3)


@respx.mock
async def test_same_report_is_stored_once_from_either_side(cfg, sessions):
    respx.get(URL).mock(side_effect=[tank_response(80.0, "2026-10-28 11:00:22.000"),
                                     tank_response(79.0, "2026-10-28 17:00:22.000")])
    src = CentriConnectSource(cfg, sessions)
    assert await src.poll() == 1  # the 03:59 AM report, already fetched live
    assert import_tank_csv(cfg, sessions, CSV)[:2] == (2, 1)
    assert await src.poll() == 0  # the 10:00 AM report, already imported from the CSV
    assert len(levels(sessions)) == 3
