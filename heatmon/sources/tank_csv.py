"""Import past tank readings from the MyPropane data table CSV.

The file has a line per tank report, newest first, in local time to the minute:
"10/28/2026, 04:00 AM",37,79%,0,∞,71%,90%. Only the time and level are kept.
The same report fetched live is stamped to the second (04:00:22), so a CSV line
within a few minutes of a stored reading is the same report and is skipped.
"""

import bisect
import csv
import io
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import TankReading

SAME_REPORT = timedelta(minutes=10)


def parse_tank_csv(text: str, tz: ZoneInfo) -> tuple[list[tuple[datetime, float, dict]], list[str]]:
    """Returns (time, level %, original line) for each reading, and any lines that could not be read."""
    rows, problems = [], []
    for n, line in enumerate(csv.DictReader(io.StringIO(text.lstrip("﻿"))), start=2):
        try:
            ts = datetime.strptime(line["Date"], "%m/%d/%Y, %I:%M %p").replace(tzinfo=tz)
            level = float(line["Level"].rstrip("%"))
        except (KeyError, TypeError, ValueError):
            problems.append(f"line {n}: {line.get('Date')!r}, level {line.get('Level')!r}")
            continue
        rows.append((ts, level, line))
    return rows, problems


def import_tank_csv(cfg: Config, sessions: sessionmaker[Session], text: str) -> tuple[int, int, list[str]]:
    """Returns (added, skipped as already stored, problem lines)."""
    rows, problems = parse_tank_csv(text, ZoneInfo(cfg.timezone))
    size = cfg.propane.tank_size_gal
    added = skipped = 0
    with sessions.begin() as db:
        stored = list(db.scalars(select(TankReading.ts).order_by(TankReading.ts)))
        for ts, level, line in rows:
            i = bisect.bisect_left(stored, ts)
            if any(abs(ts - s) < SAME_REPORT for s in stored[max(i - 1, 0):i + 1]):
                skipped += 1
                continue
            db.add(TankReading(ts=ts, level_pct=level, remaining_gal=size * level / 100, tank_size_gal=size,
                               raw_json=json.dumps({"source": "csv", **line}, ensure_ascii=False)))
            stored.insert(i, ts)
            added += 1
    return added, skipped, problems
