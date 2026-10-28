from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from heatmon.processing.zone_calls import Sample, derive_calls, minutes_on_day

TZ = ZoneInfo("America/Los_Angeles")


def local(*args) -> datetime:
    return datetime(*args, tzinfo=TZ).astimezone(UTC)


def samples(start: datetime, statuses: str, step_min: int = 2) -> list[Sample]:
    """'H' = HEATING, '.' = OFF, one sample per step."""
    return [Sample(start + timedelta(minutes=step_min * i), "HEATING" if c == "H" else "OFF", 20.0 + i, 21.0)
            for i, c in enumerate(statuses)]


def calls(ss: list[Sample]):
    """Derive calls as if checked one minute after the last sample."""
    return derive_calls(ss, 120, ss[-1].ts + timedelta(minutes=1))


def test_changes_placed_halfway_between_samples():
    [call] = calls(samples(local(2026, 11, 5, 10, 0), ".HH."))
    assert call.start_ts == local(2026, 11, 5, 10, 1)
    assert call.end_ts == local(2026, 11, 5, 10, 5)
    assert call.duration_s == 240
    assert (call.start_ambient_c, call.end_ambient_c, call.setpoint_c) == (21.0, 23.0, 21.0)
    assert not call.open and not call.gap_closed


def test_call_spanning_midnight_is_split_between_days():
    # OFF at 23:48, HEATING 23:50 through 00:20, OFF at 00:22.
    [call] = calls(samples(local(2026, 11, 5, 23, 48), "." + "H" * 16 + "."))
    assert call.start_ts == local(2026, 11, 5, 23, 49) and call.end_ts == local(2026, 11, 6, 0, 21)
    iv = [(call.start_ts, call.end_ts)]
    assert minutes_on_day(iv, date(2026, 11, 5), TZ) == 11
    assert minutes_on_day(iv, date(2026, 11, 6), TZ) == 21


def test_missed_samples_close_the_call_at_the_last_reading():
    before = samples(local(2026, 11, 5, 10, 0), "HH")  # 10:00, 10:02, then 18 minutes of nothing
    after = samples(local(2026, 11, 5, 10, 20), "H.")
    first, second = calls(before + after)
    assert first.start_ts == local(2026, 11, 5, 10, 0)
    assert first.end_ts == local(2026, 11, 5, 10, 2) and first.gap_closed
    assert second.start_ts == local(2026, 11, 5, 10, 20)  # no midpoint across the gap
    assert second.end_ts == local(2026, 11, 5, 10, 21) and not second.gap_closed


def test_call_still_running_is_open():
    [call] = calls(samples(local(2026, 11, 5, 10, 0), ".HHH"))
    assert call.open and call.end_ts == local(2026, 11, 5, 10, 6)


def test_overlapping_calls_counted_once():
    a = local(2026, 11, 5, 10, 0)
    iv = [(a, a + timedelta(minutes=30)), (a + timedelta(minutes=20), a + timedelta(minutes=40)),
          (a + timedelta(hours=2), a + timedelta(hours=2, minutes=5))]
    assert minutes_on_day(iv, date(2026, 11, 5), TZ) == 45


def test_dst_fall_back_day():
    # 2026-11-01: clocks go from 01:59 PDT back to 01:00 PST, so the day has 25 hours.
    start = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)  # 00:30 PDT
    end = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)  # 01:30 PST, two real hours later
    ss = samples(start - timedelta(minutes=2), "." + "H" * 60 + ".")
    [call] = calls(ss)
    assert call.duration_s == 2 * 3600
    assert minutes_on_day([(call.start_ts, call.end_ts)], date(2026, 11, 1), TZ) == 120
    whole_day = [(datetime(2026, 11, 1, 0, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC))]
    assert minutes_on_day(whole_day, date(2026, 11, 1), TZ) == 25 * 60
    assert start.astimezone(TZ).hour == 0 and end.astimezone(TZ).hour == 1


def test_call_with_no_recent_readings_is_closed_not_open():
    ss = samples(local(2026, 11, 5, 10, 0), ".HHH")  # thermostat went quiet while heating
    [call] = derive_calls(ss, 120, ss[-1].ts + timedelta(minutes=7))
    assert not call.open and call.gap_closed and call.end_ts == local(2026, 11, 5, 10, 6)
