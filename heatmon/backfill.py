"""Load past weather from the home station and Open-Meteo, then rebuild summaries."""

import asyncio
import logging
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import WeatherObs
from heatmon.db.session import insert_ignore, upsert
from heatmon.processing.days import day_bounds
from heatmon.processing.summaries import rebuild
from heatmon.sources.open_meteo import fetch_archive
from heatmon.sources.wunderground import fetch_history_day

log = logging.getLogger(__name__)

# KWACLEEL38's history has one row every 15 minutes (96 a day), and the live poller stores
# one every 10 minutes (144 a day). A day with this many rows is treated as done.
COMPLETE_DAY_ROWS = 80
# Owner API keys allow 30 calls a minute and 1,500 a day.
PAUSE_S = 2.5


def _station_rows_on(sessions: sessionmaker[Session], cfg: Config, day: date) -> int:
    start, end = day_bounds(day, ZoneInfo(cfg.timezone))
    with sessions() as db:
        return db.scalar(select(func.count()).select_from(WeatherObs).where(
            WeatherObs.source == "wu", WeatherObs.station_id == cfg.station_id,
            WeatherObs.ts >= start, WeatherObs.ts < end))


async def backfill(cfg: Config, sessions: sessionmaker[Session], start: date, end: date,
                   pause_s: float = PAUSE_S) -> None:
    """Safe to rerun: days already loaded are skipped."""
    async with httpx.AsyncClient(timeout=60) as client:
        if cfg.secrets.wu_api_key:
            day = start
            while day <= end:
                if _station_rows_on(sessions, cfg, day) < COMPLETE_DAY_ROWS:
                    rows = await fetch_history_day(client, cfg, day)
                    n = insert_ignore(sessions, WeatherObs, rows)
                    log.info("%s: %d station rows fetched, %d new", day, len(rows), n)
                    await asyncio.sleep(pause_s)
                day += timedelta(days=1)
        else:
            log.warning("No WU API key; skipping station history")
        n = upsert(sessions, WeatherObs, await fetch_archive(client, cfg, start, end))
        log.info("Open-Meteo: stored %d hours", n)
    rebuild(cfg, sessions, start)
