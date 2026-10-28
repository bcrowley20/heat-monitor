"""Weather Underground personal weather station through The Weather Company PWS API."""

from datetime import date, datetime

import httpx
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import WeatherObs
from heatmon.db.session import insert_ignore

PWS_URL = "https://api.weather.com/v2/pws"


def parse_obs(o: dict) -> dict:
    """Parse one observation from either the current or the history endpoint.

    Current observations have single values (temp). History rows summarize each upload
    interval (tempAvg, tempHigh, ...), so take the average where one exists.
    """
    imp = o.get("imperial") or {}

    def pick(d: dict, *keys):
        return next((d[k] for k in keys if d.get(k) is not None), None)

    p_max, p_min = imp.get("pressureMax"), imp.get("pressureMin")
    history_pressure = (p_max + p_min) / 2 if p_max is not None and p_min is not None else None
    return {
        "source": "wu",
        "station_id": o["stationID"],
        "ts": datetime.fromisoformat(o["obsTimeUtc"]),
        "temp_f": pick(imp, "temp", "tempAvg"),
        "humidity_pct": pick(o, "humidity", "humidityAvg"),
        "dewpoint_f": pick(imp, "dewpt", "dewptAvg"),
        "wind_mph": pick(imp, "windSpeed", "windspeedAvg"),
        "gust_mph": pick(imp, "windGust", "windgustHigh"),
        "solar_wm2": pick(o, "solarRadiation", "solarRadiationHigh"),
        "precip_rate_inhr": imp.get("precipRate"),
        "pressure_inhg": imp.get("pressure", history_pressure),
    }


async def fetch_history_day(client: httpx.AsyncClient, cfg: Config, day: date) -> list[dict]:
    r = await client.get(f"{PWS_URL}/history/all", params={
        "stationId": cfg.station_id,
        "format": "json",
        "units": "e",
        "date": day.strftime("%Y%m%d"),
        "apiKey": cfg.secrets.wu_api_key,
    })
    r.raise_for_status()
    if r.status_code == 204:  # no data for that day
        return []
    return [parse_obs(o) for o in r.json().get("observations", [])]


class WundergroundSource:
    name = "wunderground"

    def __init__(self, cfg: Config, sessions: sessionmaker[Session]):
        self.cfg = cfg
        self.sessions = sessions
        self.interval_s = cfg.intervals.weather_s
        self.client = httpx.AsyncClient(timeout=30)

    async def poll(self) -> int:
        r = await self.client.get(f"{PWS_URL}/observations/current", params={
            "stationId": self.cfg.station_id,
            "format": "json",
            "units": "e",
            "apiKey": self.cfg.secrets.wu_api_key,
        })
        r.raise_for_status()
        if r.status_code == 204:  # station has not reported recently
            raise RuntimeError(f"No current observation from {self.cfg.station_id}")
        rows = [parse_obs(o) for o in r.json().get("observations", [])]
        return insert_ignore(self.sessions, WeatherObs, rows)
