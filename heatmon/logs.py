"""Logging to stdout and a file, with secret values masked."""

import logging
import logging.handlers
from pathlib import Path

_secrets: list[str] = []


def scrub(text: str) -> str:
    for s in _secrets:
        text = text.replace(s, "***")
    return text


class _ScrubFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def setup_logging(log_path: Path, secrets: list[str]) -> None:
    _secrets[:] = [s for s in secrets if s]
    fmt = _ScrubFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3),
    ]
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    # httpx logs every request URL at INFO, which is noisy every 2 minutes.
    logging.getLogger("httpx").setLevel(logging.WARNING)
