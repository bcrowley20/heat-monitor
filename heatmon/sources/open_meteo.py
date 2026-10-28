"""Open-Meteo hourly weather for the station's location.

Stored for every hour. It provides solar estimates (the station has no solar sensor)
and fills in outdoor temperature for hours the station is missing. Wherever the
station has a temperature, the calculations use the station's.
"""

from datetime import UTC, date, datetime

import httpx
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import WeatherObs
from heatmon.db.session import upsert

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"  # has the most recent days
HOURLY = ("temperature_2m,relative_humidity_2m,dew_point_2m,wind_speed_10m,wind_gusts_10m,"
          "shortwave_radiation,precipitation,pressure_msl")
HPA_TO_INHG = 0.0295300


async def _fetch(client: httpx.AsyncClient, cfg: Config, url: str, **params) -> list[dict]:
    r = await client.get(url, params={
        "latitude": cfg.latitude,
        "longitude": cfg.longitude,
        "hourly": HOURLY,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "UTC",
        **params,
    })
    r.raise_for_status()
    h = r.json()["hourly"]
    now = datetime.now(UTC)
    rows = []
    for i, t in enumerate(h["time"]):
        ts = datetime.fromisoformat(t).replace(tzinfo=UTC)
        if h["temperature_2m"][i] is None or ts > now:  # not published yet, or a forecast
            continue
        pressure = h["pressure_msl"][i]
        rows.append({
            "source": "open-meteo",
            "station_id": cfg.station_id,  # the station these rows stand in for
            "ts": ts,
            "temp_f": h["temperature_2m"][i],
            "humidity_pct": h["relative_humidity_2m"][i],
            "dewpoint_f": h["dew_point_2m"][i],
            "wind_mph": h["wind_speed_10m"][i],
            "gust_mph": h["wind_gusts_10m"][i],
            # Open-Meteo gives the average over the hour before ts.
            "solar_wm2": h["shortwave_radiation"][i],
            "precip_rate_inhr": h["precipitation"][i],
            "pressure_inhg": None if pressure is None else round(pressure * HPA_TO_INHG, 3),
        })
    return rows


async def fetch_archive(client: httpx.AsyncClient, cfg: Config, start: date, end: date) -> list[dict]:
    return await _fetch(client, cfg, ARCHIVE_URL,
                        start_date=start.isoformat(), end_date=end.isoformat())


async def fetch_recent(client: httpx.AsyncClient, cfg: Config) -> list[dict]:
    return await _fetch(client, cfg, FORECAST_URL, past_days=2, forecast_days=1)


class OpenMeteoSource:
    name = "open-meteo"

    def __init__(self, cfg: Config, sessions: sessionmaker[Session]):
        self.cfg = cfg
        self.sessions = sessions
        self.interval_s = cfg.intervals.open_meteo_s
        self.client = httpx.AsyncClient(timeout=30)

    async def poll(self) -> int:
        return upsert(self.sessions, WeatherObs, await fetch_recent(self.client, self.cfg))
