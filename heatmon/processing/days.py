from datetime import UTC, date, datetime, time, timedelta


def day_bounds(day: date, tz) -> tuple[datetime, datetime]:
    """UTC start and end of a local day. A DST day is 23 or 25 hours long.

    Do time arithmetic in UTC. Python subtracts two times in the same zone by clock
    time, so a 25-hour local day would come out as 24 hours.
    """
    start = datetime.combine(day, time(), tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(), tz).astimezone(UTC)
    return start, end


def local_date(ts: datetime, tz) -> date:
    return ts.astimezone(tz).date()
