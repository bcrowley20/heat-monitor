"""Open-Meteo hourly weather, used only for hours the home station is missing."""

from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import WeatherObs
from heatmon.db.session import insert_ignore

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = ("temperature_2m,relative_humidity_2m,dew_point_2m,wind_speed_10m,wind_gusts_10m,"
          "shortwave_radiation,precipitation,pressure_msl")
HPA_TO_INHG = 0.0295300


async def fetch_hourly(client: httpx.AsyncClient, cfg: Config, start: date, end: date) -> list[dict]:
    r = await client.get(ARCHIVE_URL, params={
        "latitude": cfg.latitude,
        "longitude": cfg.longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": HOURLY,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "UTC",
    })
    r.raise_for_status()
    h = r.json()["hourly"]
    rows = []
    for i, t in enumerate(h["time"]):
        if h["temperature_2m"][i] is None:  # the newest few days may not be published yet
            continue
        pressure = h["pressure_msl"][i]
        rows.append({
            "source": "open-meteo",
            "station_id": cfg.station_id,  # the station these rows stand in for
            "ts": datetime.fromisoformat(t).replace(tzinfo=UTC),
            "temp_f": h["temperature_2m"][i],
            "humidity_pct": h["relative_humidity_2m"][i],
            "dewpoint_f": h["dew_point_2m"][i],
            "wind_mph": h["wind_speed_10m"][i],
            "gust_mph": h["wind_gusts_10m"][i],
            "solar_wm2": h["shortwave_radiation"][i],
            "precip_rate_inhr": h["precipitation"][i],
            "pressure_inhg": None if pressure is None else round(pressure * HPA_TO_INHG, 3),
        })
    return rows


def _hours_with_station_data(sessions: sessionmaker[Session], cfg: Config,
                             start: datetime, end: datetime) -> set[datetime]:
    with sessions() as db:
        stamps = db.scalars(select(WeatherObs.ts).where(
            WeatherObs.source == "wu",
            WeatherObs.station_id == cfg.station_id,
            WeatherObs.ts >= start,
            WeatherObs.ts < end,
        ))
        return {ts.replace(minute=0, second=0) for ts in stamps}


async def fill_gaps(client: httpx.AsyncClient, cfg: Config, sessions: sessionmaker[Session],
                    start: date, end: date) -> int:
    """Store Open-Meteo rows for each hour in [start, end] that has no station data."""
    rows = await fetch_hourly(client, cfg, start, end)
    have = _hours_with_station_data(
        sessions, cfg,
        datetime.combine(start, datetime.min.time(), UTC),
        datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC),
    )
    return insert_ignore(sessions, WeatherObs, [r for r in rows if r["ts"] not in have])
