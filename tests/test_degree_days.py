from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from heatmon.processing.days import day_bounds
from heatmon.processing.degree_days import daily_solar, day_weather, hourly_temps

TZ = ZoneInfo("America/Los_Angeles")
H = timedelta(hours=1)
LATER = datetime(2030, 1, 1, tzinfo=UTC)  # every test day is over


def hours_of(day: date):
    lo, hi = day_bounds(day, TZ)
    return [lo + i * H for i in range(int((hi - lo) / H))]


def test_station_preferred_over_open_meteo():
    h = datetime(2026, 11, 5, 12, tzinfo=UTC)
    obs = [(h, "wu", 30.0), (h + timedelta(minutes=15), "wu", 32.0), (h, "open-meteo", 40.0),
           (h + H, "open-meteo", 41.0), (h + 2 * H, "wu", None)]
    assert hourly_temps(obs) == {h: (31.0, "wu"), h + H: (41.0, "open-meteo")}


def test_hdd_matches_manual_calculation():
    day = date(2026, 11, 5)
    temps = [30 + i for i in range(24)]  # 30°F at midnight up to 53°F at 23:00
    hourly = {h: (t, "wu") for h, t in zip(hours_of(day), temps)}
    hourly[hours_of(day)[0]] = (30, "open-meteo")
    w = day_weather(hourly, day, TZ, 65, LATER)
    assert w.hdd == pytest.approx(sum(65 - t for t in temps) / 24, abs=0.01)  # 23.5
    assert (w.min_f, w.max_f, w.mean_f) == (30, 53, 41.5)
    assert (w.hours, w.open_meteo_hours) == (24, 1)


def test_warm_hours_add_nothing():
    day = date(2026, 6, 5)
    hourly = {h: (70, "wu") for h in hours_of(day)}
    assert day_weather(hourly, day, TZ, 65, LATER).hdd == 0


def test_dst_day_has_25_hours_of_degree_days():
    day = date(2026, 11, 1)
    assert len(hours_of(day)) == 25
    hourly = {h: (41, "wu") for h in hours_of(day)}
    assert day_weather(hourly, day, TZ, 65, LATER).hdd == round(24 * 25 / 24, 2)


def test_missing_hours_filled_by_the_days_average():
    day = date(2026, 11, 5)
    hourly = {h: (41, "wu") for h in hours_of(day)[:18]}
    w = day_weather(hourly, day, TZ, 65, LATER)
    assert w.hdd == 24 and w.hours == 18


def test_too_few_hours_gives_no_hdd():
    day = date(2026, 11, 5)
    hourly = {h: (41, "wu") for h in hours_of(day)[:17]}
    w = day_weather(hourly, day, TZ, 65, LATER)
    assert w.hdd is None and w.mean_f == 41 and w.hours == 17
    assert day_weather({}, day, TZ, 65, LATER).hdd is None


def test_solar_value_belongs_to_the_hour_before():
    midnight = day_bounds(date(2026, 11, 6), TZ)[0]
    solar = daily_solar([(midnight, 500.0), (midnight + H, 100.0)], TZ)
    assert solar == {date(2026, 11, 5): 0.5, date(2026, 11, 6): 0.1}


def test_unfinished_day_gets_no_hdd():
    day = date(2026, 11, 5)
    hourly = {h: (41, "wu") for h in hours_of(day)}
    evening = day_bounds(day, TZ)[1] - 2 * H
    w = day_weather(hourly, day, TZ, 65, evening)
    assert w.hdd is None and w.mean_f == 41
