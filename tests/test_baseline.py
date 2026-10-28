from datetime import date, timedelta

import pytest

from heatmon.processing.baseline import Baseline, Delivery, delivery_intervals, fit_baseline, season_of, by_season


def d(day, gallons, after=None, before=None, price=None):
    return Delivery(day, gallons, price, before, after)


def test_usage_between_deliveries_three_ways():
    hdd = {date(2025, 1, 1) + timedelta(days=i): 20.0 for i in range(120)}
    ivs = delivery_intervals([
        d(date(2025, 1, 1), 400, after=80),
        d(date(2025, 2, 1), 300, after=76),   # filled to a lower level: used 300 + 40
        d(date(2025, 3, 1), 250, before=50),  # level before known: used 760 - 500
        d(date(2025, 4, 1), 200),             # nothing known: used = delivery size
    ], hdd, 1000)
    assert [(iv.gallons, iv.method) for iv in ivs] == [(340, "fill levels"), (260, "tank levels"),
                                                       (200, "delivery size")]
    assert ivs[0].days == 31 and ivs[0].hdd == 620


def test_interval_without_enough_weather_has_no_hdd():
    hdd = {date(2025, 1, 1) + timedelta(days=i): 10.0 for i in range(20)}  # 20 of 31 days
    [iv] = delivery_intervals([d(date(2025, 1, 1), 1), d(date(2025, 2, 1), 100)], hdd, 1000)
    assert iv.hdd is None


def test_fit_recovers_base_and_slope():
    hdd, ds, day = {}, [d(date(2020, 1, 1), 500)], date(2020, 1, 1)
    for i, (n, per_day) in enumerate([(60, 25), (90, 5), (45, 30), (120, 12), (30, 0)]):
        for k in range(n):
            hdd[day + timedelta(days=k)] = per_day
        used = 1.2 * n + 0.15 * n * per_day  # base 1.2 gal/day, 0.15 gal/HDD
        day += timedelta(days=n)
        ds.append(d(day, used))
    fit = fit_baseline(delivery_intervals(ds, hdd, 1000))
    assert fit.base_gal_per_day == pytest.approx(1.2) and fit.gal_per_hdd == pytest.approx(0.15)
    assert fit.r2 == pytest.approx(1) and fit.predict(10, 100) == pytest.approx(27)
    seasons = by_season(delivery_intervals(ds, hdd, 1000), hdd, fit, 2.0)
    assert sum(s["gallons"] for s in seasons) == pytest.approx(sum(1.2 * n + 0.15 * n * p for n, p in
                                                                   [(60, 25), (90, 5), (45, 30), (120, 12), (30, 0)]), abs=3)


def test_seasons():
    assert season_of(date(2025, 10, 1)) == "2025–26" and season_of(date(2026, 4, 30)) == "2025–26"
    assert season_of(date(2026, 5, 1)) == "Summer 2026"


def test_seasons_skip_delivery_periods_without_weather():
    hdd = {date(2024, 11, 1) + timedelta(days=i): 20.0 for i in range(60)}  # weather for the 2nd period only
    ds = [d(date(2024, 10, 1), 500), d(date(2024, 11, 1), 300), d(date(2024, 12, 31), 400)]
    ivs = delivery_intervals(ds, hdd, 1000)
    assert ivs[0].hdd is None and ivs[1].hdd == 1200
    fit = Baseline(1.0, 0.2, 1.0, 1, date(2024, 11, 1), date(2024, 12, 31))
    [season] = by_season(ivs, hdd, fit, None)
    assert season["gallons"] == 400 and season["hdd"] == 1200 and season["days"] == 60
