from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from heatmon.config import Propane
from heatmon.processing.propane import Reading, daily_usage, tank_intervals

TZ = ZoneInfo("America/Los_Angeles")
P = Propane()  # 1,000 gallon tank, 5% fill threshold


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=TZ)


def test_refill_detected_and_excluded_from_usage():
    readings = [Reading(at(5), 60, 1000), Reading(at(5, 12), 58, 1000),
                Reading(at(6), 98, 1000), Reading(at(6, 12), 97, None)]
    ivs = tank_intervals(readings, P)
    assert [(iv.kind, iv.gallons) for iv in ivs] == [("usage", 20), ("delivery", 400), ("usage", 10)]
    used = daily_usage(ivs, {}, P, TZ)
    assert used[date(2026, 11, 5)] is None  # the fill happened that day, so usage is unknown
    assert sum(iv.gallons for iv in ivs if iv.kind == "usage") == 30


def test_small_rise_is_noise():
    [iv] = tank_intervals([Reading(at(5), 60, 1000), Reading(at(5, 12), 61, 1000)], P)
    assert (iv.kind, iv.gallons) == ("noise", 0)


def test_noise_does_not_add_usage():
    levels = [50, 51, 49, 49.5, 49, 48]  # true use: 50% down to 48% = 20 gallons
    readings = [Reading(at(5, 2 * i), lvl, 1000) for i, lvl in enumerate(levels)]
    ivs = tank_intervals(readings, P)
    assert [iv.kind for iv in ivs] == ["noise", "usage", "noise", "usage", "usage"]
    assert sum(iv.gallons for iv in ivs) == pytest.approx(20)


def test_fill_after_noise_resets_the_starting_level():
    readings = [Reading(at(5), 50, 1000), Reading(at(5, 6), 51, 1000),
                Reading(at(5, 12), 90, 1000), Reading(at(5, 18), 89, 1000)]
    ivs = tank_intervals(readings, P)
    assert [(iv.kind, round(iv.gallons)) for iv in ivs] == [("noise", 0), ("delivery", 390), ("usage", 10)]


def test_flat_tank_for_two_days():
    readings = [Reading(at(5), 60, 1000), Reading(at(6), 60, 1000), Reading(at(7), 60, 1000)]
    used = daily_usage(tank_intervals(readings, P), {}, P, TZ)
    assert used == {date(2026, 11, 5): 0, date(2026, 11, 6): 0}


def test_usage_spread_by_degree_days():
    # 8% (80 gal) over two whole days with 10 and 30 HDD.
    # Weights: 1.0 + 0.15 x 10 = 2.5 and 1.0 + 0.15 x 30 = 5.5.
    ivs = tank_intervals([Reading(at(5), 60, 1000), Reading(at(7), 52, 1000)], P)
    used = daily_usage(ivs, {date(2026, 11, 5): 10, date(2026, 11, 6): 30}, P, TZ)
    assert used[date(2026, 11, 5)] == pytest.approx(80 * 2.5 / 8)
    assert used[date(2026, 11, 6)] == pytest.approx(80 * 5.5 / 8)


def test_partly_covered_days_have_no_estimate():
    ivs = tank_intervals([Reading(at(5, 8), 60, 1000), Reading(at(7, 8), 58, 1000)], P)
    used = daily_usage(ivs, {}, P, TZ)
    assert used[date(2026, 11, 5)] is None and used[date(2026, 11, 7)] is None
    assert used[date(2026, 11, 6)] == pytest.approx(10)  # 24 of the 48 hours, same weights


def test_dst_day_gets_a_25_hour_share():
    readings = [Reading(at(1), 60, 1000), Reading(at(2), 50, 1000), Reading(at(3), 50, 1000)]
    used = daily_usage(tank_intervals(readings, P), {}, P, TZ)
    assert used[date(2026, 11, 1)] == 100
