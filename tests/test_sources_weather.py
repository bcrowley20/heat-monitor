from datetime import UTC, date, datetime, timedelta

import httpx
import respx
from sqlalchemy import select

from heatmon.backfill import backfill
from heatmon.db.models import WeatherObs
from heatmon.db.session import insert_ignore
from heatmon.sources import open_meteo, wunderground
from heatmon.sources.wunderground import PWS_URL, WundergroundSource, parse_obs
from tests.conftest import load_fixture


def test_parse_current():
    row = parse_obs(load_fixture("wu_current.json")["observations"][0])
    assert row["ts"] == datetime(2026, 10, 28, 13, 45, 9, tzinfo=UTC)
    assert (row["temp_f"], row["humidity_pct"], row["dewpoint_f"]) == (35, 95, 34)
    assert (row["wind_mph"], row["gust_mph"], row["pressure_inhg"]) == (0, 1, 29.93)
    assert row["solar_wm2"] is None and row["source"] == "wu"  # this station has no solar sensor


def test_parse_history():
    row = parse_obs(load_fixture("wu_history_all.json")["observations"][1])
    assert (row["temp_f"], row["humidity_pct"], row["dewpoint_f"]) == (36, 97, 35)
    assert (row["wind_mph"], row["gust_mph"], row["pressure_inhg"]) == (0, 0, 30.0)


@respx.mock
async def test_poll_stores_current(cfg, sessions):
    route = respx.get(f"{PWS_URL}/observations/current").respond(json=load_fixture("wu_current.json"))
    assert await WundergroundSource(cfg, sessions).poll() == 1
    assert route.calls.last.request.url.params["stationId"] == "KWACLEEL38"
    assert await WundergroundSource(cfg, sessions).poll() == 0  # same observation again


@respx.mock
async def test_fetch_history_204_means_no_data(cfg):
    respx.get(f"{PWS_URL}/history/all").respond(204)
    async with httpx.AsyncClient() as client:
        assert await wunderground.fetch_history_day(client, cfg, date(2020, 1, 1)) == []


@respx.mock
async def test_open_meteo_fetch(cfg):
    respx.get(open_meteo.ARCHIVE_URL).respond(json=load_fixture("open_meteo_archive.json"))
    async with httpx.AsyncClient() as client:
        rows = await open_meteo.fetch_hourly(client, cfg, date(2026, 10, 27), date(2026, 10, 27))
    assert len(rows) == 3  # the hour with no data yet is dropped
    assert rows[0]["ts"] == datetime(2026, 10, 27, 6, tzinfo=UTC)
    assert rows[0]["source"] == "open-meteo" and rows[0]["pressure_inhg"] == 30.401


@respx.mock
async def test_backfill_uses_station_first_and_fills_only_gaps(cfg, sessions):
    def history(request):
        if request.url.params["date"] == "20261027":
            return httpx.Response(200, json=load_fixture("wu_history_all.json"))
        return httpx.Response(204)

    hist = respx.get(f"{PWS_URL}/history/all").mock(side_effect=history)
    respx.get(open_meteo.ARCHIVE_URL).respond(json=load_fixture("open_meteo_archive.json"))

    await backfill(cfg, sessions, date(2026, 10, 26), date(2026, 10, 27), pause_s=0)

    assert hist.call_count == 2
    with sessions() as db:
        rows = db.execute(select(WeatherObs.source, WeatherObs.ts).order_by(WeatherObs.ts)).all()
    om_hours = [ts.hour for src, ts in rows if src == "open-meteo"]
    assert [src for src, _ in rows].count("wu") == 3
    assert om_hours == [6, 8]  # 07:00 UTC has station data, so Open-Meteo skips it


@respx.mock
async def test_backfill_skips_days_already_loaded(cfg, sessions):
    local_midnight = datetime(2024, 1, 15, 8, tzinfo=UTC)
    insert_ignore(sessions, WeatherObs, [
        {"source": "wu", "station_id": "KWACLEEL38", "ts": local_midnight + timedelta(minutes=5 * i)}
        for i in range(150)])
    hist = respx.get(f"{PWS_URL}/history/all").respond(204)
    respx.get(open_meteo.ARCHIVE_URL).respond(json=load_fixture("open_meteo_archive.json"))

    await backfill(cfg, sessions, date(2024, 1, 15), date(2024, 1, 15), pause_s=0)
    assert hist.call_count == 0
