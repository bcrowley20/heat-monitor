"""Boiler: reads the Lochinvar Knight's front service port (Modbus RTU, 19200 8N1).

The port is a serial URL, so the same code works over the network and on a local cable:
rfc2217://boilerpi:2217 for a Pi running ser2net, or /dev/serial/by-id/... for a USB
cable plugged into this computer. Only function 3 (read holding registers) is ever sent:
this code never writes to the boiler.
"""

import asyncio
import csv
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import serial
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import BoilerCounters, BoilerCycle, BoilerParams, BoilerSample, Event
from heatmon.db.session import insert_ignore, read_setting, write_setting
from heatmon.processing.boiler import (PARAM_BLOCKS, STATUS_COUNT, STATUS_START, Status, Tracker, counters,
                                       setting_changes, settings_of)

log = logging.getLogger(__name__)

SLAVE = 1
READ_HOLDING = 3
PARAMS_EVERY = timedelta(hours=1)


class BoilerSettings(BaseModel):
    enabled: bool = False
    port: str = ""  # rfc2217://host:2217 or a serial device path


def load_settings(sessions: sessionmaker[Session]) -> BoilerSettings:
    return read_setting(sessions, "boiler", BoilerSettings)


def save_settings(sessions: sessionmaker[Session], s: BoilerSettings) -> None:
    write_setting(sessions, "boiler", s)


# ---------- Modbus ----------

def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def read_request(start: int, count: int) -> bytes:
    body = bytes([SLAVE, READ_HOLDING]) + start.to_bytes(2, "big") + count.to_bytes(2, "big")
    return body + crc16(body).to_bytes(2, "little")


def parse_response(frame: bytes, count: int) -> list[int]:
    if len(frame) < 5 or crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise IOError(f"bad or short reply from the boiler: {frame.hex()}")
    if frame[1] != READ_HOLDING:
        raise IOError(f"the boiler refused the read: {frame.hex()}")
    if frame[2] != 2 * count or len(frame) != 5 + 2 * count:
        raise IOError(f"wrong length reply from the boiler: {frame.hex()}")
    return [int.from_bytes(frame[3 + 2 * i:5 + 2 * i], "big") for i in range(count)]


def read_registers(port, start: int, count: int) -> list[int]:
    port.reset_input_buffer()
    port.write(read_request(start, count))
    head = port.read(3)
    rest = port.read(2 if len(head) == 3 and head[1] & 0x80 else 2 + 2 * count)
    return parse_response(head + rest, count)


def read_params(port) -> dict[int, int]:
    values = {}
    for start, count in PARAM_BLOCKS:
        values.update(zip(range(start, start + count), read_registers(port, start, count)))
        time.sleep(0.05)
    return values


def open_port(url: str):
    return serial.serial_for_url(url, baudrate=19200, bytesize=8, parity="N", stopbits=1, timeout=1)


# ---------- storing ----------

def store_params(sessions: sessionmaker[Session], ts: datetime, values: dict[int, int]) -> None:
    """Save the counters, and the settings when they differ from the last saved ones. A
    change also adds a setting_change Event, so charts and settings periods pick it up."""
    insert_ignore(sessions, BoilerCounters, [{"ts": ts, **counters(values)}])
    new = settings_of(values)
    with sessions.begin() as db:
        last = db.scalars(select(BoilerParams).where(BoilerParams.ts <= ts)
                          .order_by(BoilerParams.ts.desc()).limit(1)).first()
        old = {int(k): v for k, v in json.loads(last.values_json).items()} if last else None
        if old == new:
            return
        db.add(BoilerParams(ts=ts, values_json=json.dumps({str(k): v for k, v in sorted(new.items())})))
        if old is not None:
            diff = setting_changes(old, new)
            log.info("Boiler settings changed: %s", "; ".join(diff))
            db.add(Event(ts=ts, type="setting_change", title="Boiler settings changed (read from the boiler)",
                         details="; ".join(diff)))


def prune_raw(sessions: sessionmaker[Session], before: datetime) -> None:
    with sessions.begin() as db:
        db.execute(update(BoilerSample).where(BoilerSample.ts < before, BoilerSample.raw_json.is_not(None))
                   .values(raw_json=None))


def store(sessions: sessionmaker[Session], minutes: list[dict], cycles: list[dict]) -> int:
    return insert_ignore(sessions, BoilerSample, minutes) + insert_ignore(sessions, BoilerCycle, cycles)


class BoilerSource:
    name = "boiler"

    def __init__(self, cfg: Config, sessions: sessionmaker[Session]):
        self.cfg, self.sessions = cfg, sessions
        self.interval_s = cfg.intervals.boiler_s
        self.tracker = Tracker(cfg.boiler.input_btu_h)
        self.port = None
        self.url = ""
        self.next_params = datetime.min.replace(tzinfo=UTC)

    def close(self) -> None:
        if self.port is not None:
            self.port.close()
            self.port = None

    def _read(self, want_params: bool) -> tuple[list[int], dict[int, int] | None]:
        if self.port is None:
            self.port = open_port(self.url)
        status = read_registers(self.port, STATUS_START, STATUS_COUNT)
        return status, (read_params(self.port) if want_params else None)

    async def poll(self) -> int | None:
        """Read the status block, and the parameters once an hour. Returns rows stored, or
        None when collection is turned off."""
        s = load_settings(self.sessions)
        if not (s.enabled and s.port.strip()):
            self.close()
            return None
        if s.port.strip() != self.url:
            self.close()
            self.url = s.port.strip()
        now = datetime.now(UTC)
        try:
            raw, params = await asyncio.to_thread(self._read, now >= self.next_params)
        except Exception:
            self.close()  # reopen on the next poll
            raise
        minutes, cycles = self.tracker.add(Status(now, raw))
        n = store(self.sessions, minutes, cycles)
        if params is not None:
            self.next_params = now + PARAMS_EVERY
            store_params(self.sessions, now, params)
            n += 1
            prune_raw(self.sessions, now - timedelta(days=self.cfg.boiler.raw_days))
        return n


def last_sample_ts(sessions: sessionmaker[Session]) -> datetime | None:
    with sessions() as db:
        return db.scalar(select(BoilerSample.ts).order_by(BoilerSample.ts.desc()).limit(1))


def test_port(url: str) -> Status:
    """One status read, for the settings dialog's test button."""
    port = open_port(url)
    try:
        return Status(datetime.now(UTC), read_registers(port, STATUS_START, STATUS_COUNT))
    finally:
        port.close()


# ---------- CSV files from boiler/boiler_log.py ----------

def _rows(path: Path):
    with path.open(newline="") as f:
        r = csv.reader(f)
        header = next(r)
        regs = [int(h[1:]) for h in header[1:]]
        for row in r:
            if len(row) == len(header):
                yield datetime.fromisoformat(row[0]), dict(zip(regs, map(int, row[1:])))


def import_csv(cfg: Config, sessions: sessionmaker[Session], status_path: Path,
               params_path: Path | None = None) -> tuple[int, int, int]:
    """Load the logger's CSV files. Rows already saved are skipped, so running it twice is
    harmless. Returns (minutes, cycles, parameter reads) stored."""
    tracker = Tracker(cfg.boiler.input_btu_h)
    minutes, cycles = [], []
    regs = range(STATUS_START, STATUS_START + STATUS_COUNT)
    for ts, values in _rows(status_path):
        m, c = tracker.add(Status(ts.astimezone(UTC), [values[r] for r in regs]))
        minutes += m
        cycles += c
    minutes += tracker.flush()
    n_min = insert_ignore(sessions, BoilerSample, minutes)
    n_cyc = insert_ignore(sessions, BoilerCycle, cycles)
    n_par = 0
    if params_path:
        for ts, values in _rows(params_path):
            store_params(sessions, ts.astimezone(UTC), values)
            n_par += 1
    return n_min, n_cyc, n_par
