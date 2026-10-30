"""Runs each source on its own loop so one failing source never stops the others."""

import asyncio
import logging

from sqlalchemy.orm import Session, sessionmaker

from heatmon.alerts import AlertsJob
from heatmon.config import Config
from heatmon.db.session import record_status
from heatmon.logs import scrub
from heatmon.processing.summaries import SummariesJob
from heatmon.sources.base import Source
from heatmon.sources.centriconnect import CentriConnectSource
from heatmon.sources.nest import NestSource
from heatmon.sources.open_meteo import OpenMeteoSource
from heatmon.sources.wunderground import WundergroundSource

log = logging.getLogger(__name__)

MAX_BACKOFF_S = 900


def build_sources(cfg: Config, sessions: sessionmaker[Session]) -> list[Source]:
    """Return the jobs to run: sources whose credentials are set, plus the keyless ones."""
    s = cfg.secrets
    candidates = [
        (NestSource, [s.google_client_id, s.google_client_secret, s.sdm_project_id]),
        (WundergroundSource, [s.wu_api_key]),
        (CentriConnectSource, [s.centriconnect_user_id, s.centriconnect_device_id,
                               s.centriconnect_device_auth]),
    ]
    sources = []
    for cls, required in candidates:
        if all(required):
            sources.append(cls(cfg, sessions))
        else:
            log.warning("%s: credentials missing from .env; not collecting", cls.name)
    sources.append(OpenMeteoSource(cfg, sessions))
    alerts = AlertsJob(cfg, sessions, {s.name for s in sources})  # data sources only, not jobs
    return sources + [SummariesJob(cfg, sessions), alerts]


def next_delay(interval_s: int, failures: int) -> int:
    """Back off exponentially after failures, but never wait less than the normal interval."""
    if failures == 0:
        return interval_s
    return max(interval_s, min(interval_s * 2 ** failures, MAX_BACKOFF_S))


async def run_source(source: Source, sessions: sessionmaker[Session]) -> None:
    failures = 0
    while True:
        try:
            n = await source.poll()
            failures = 0
            record_status(sessions, source.name)
            log.info("%s: stored %d rows", source.name, n)
        except Exception as e:
            failures += 1
            msg = scrub(f"{type(e).__name__}: {e}")
            log.warning("%s: poll failed (%d in a row): %s", source.name, failures, msg)
            record_status(sessions, source.name, error=msg)
        await asyncio.sleep(next_delay(source.interval_s, failures))


async def run_all(sources: list[Source], sessions: sessionmaker[Session]) -> None:
    await asyncio.gather(*(run_source(s, sessions) for s in sources))
