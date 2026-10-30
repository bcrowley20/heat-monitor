"""Boiler pump sensor: reads pump switches from the Cardputer running cardputer/src/main.cpp.

The code calls it "the Pi", after the first sensor. The Pi times each switch with its own
monotonic clock. Each reply also gives the Pi's clock reading at that moment, so a switch
happened (Pi now - switch time) before the reply arrived. The Pi's wall clock is never used.
"""

import logging
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import AppSetting, PumpEvent
from heatmon.db.session import read_setting, write_setting

log = logging.getLogger(__name__)

TIMEOUT_S = 3
PUMP_NAMES = {"boiler": "Boiler pump", "system": "System pump", "dhw": "DHW pump"}


class PumpSettings(BaseModel):
    enabled: bool = False
    address: str = ""  # host or host:port of the Pi


class PumpLink(BaseModel):
    """Where heatmon is in the Pi's switch log. Kept between restarts."""

    boot_id: str = ""
    last_seq: int = 0
    last_contact: datetime | None = None


class PiPump(BaseModel):
    on: bool
    since_mono: float


class PiEvent(BaseModel):
    seq: int
    pump: str
    on: bool
    mono: float


class PiReply(BaseModel):
    boot_id: str
    mono_now: float
    pumps: dict[str, PiPump]
    first_seq: int
    last_seq: int
    more: bool = False
    events: list[PiEvent]


def load_settings(sessions: sessionmaker[Session]) -> PumpSettings:
    return read_setting(sessions, "pump_sensor", PumpSettings)


def save_settings(sessions: sessionmaker[Session], s: PumpSettings) -> None:
    write_setting(sessions, "pump_sensor", s)


def base_url(address: str) -> str:
    """Accepts 192.168.1.50, 192.168.1.50:8081 or a full http:// address."""
    a = address.strip().rstrip("/")
    if not a:
        raise ValueError("No address set for the pump sensor")
    if "://" not in a:
        a = "http://" + a
    if ":" not in a.split("://", 1)[1]:
        a += ":8081"
    return a


async def fetch(client: httpx.AsyncClient, address: str, path: str = "/", params: dict | None = None) -> PiReply:
    """One reply from the Pi. Raises on no answer, an HTTP error or a reply that isn't the Pi's JSON."""
    r = await client.get(base_url(address) + path, params=params, timeout=TIMEOUT_S)
    r.raise_for_status()
    try:
        return PiReply.model_validate_json(r.content)
    except ValueError:
        raise RuntimeError(f"{address} answered, but not with pump sensor data") from None


def last_states(db: Session) -> dict[str, tuple[bool, datetime]]:
    """Each pump's most recent stored state and its time."""
    newest = select(PumpEvent.pump, func.max(PumpEvent.ts).label("ts")).group_by(PumpEvent.pump).subquery()
    rows = db.execute(select(PumpEvent.pump, PumpEvent.on, PumpEvent.ts)
                      .join(newest, (PumpEvent.pump == newest.c.pump) & (PumpEvent.ts == newest.c.ts))
                      .order_by(PumpEvent.id)).all()
    return {p: (on, ts) for p, on, ts in rows}  # newest id wins if two share a second


class PumpSource:
    name = "pumps"

    def __init__(self, cfg: Config, sessions: sessionmaker[Session]):
        self.sessions = sessions
        self.interval_s = cfg.intervals.pumps_s
        self.client = httpx.AsyncClient()

    async def poll(self) -> int | None:
        """Store new switches. Returns None when the sensor is turned off."""
        s = load_settings(self.sessions)
        if not (s.enabled and s.address.strip()):
            return None
        link = read_setting(self.sessions, "pump_link", PumpLink)
        stored = 0
        while True:
            reply = await fetch(self.client, s.address, "/events",
                                {"after": link.last_seq, "boot": link.boot_id})
            stored += self.store(reply, link, datetime.now(UTC))
            if not reply.more:
                return stored

    def store(self, reply: PiReply, link: PumpLink, now: datetime) -> int:
        """Save one reply's switches and update `link`, in one transaction so a retry never
        doubles the gap rows. Fills gaps when switches were missed."""
        def at(mono: float) -> datetime:
            return now - timedelta(seconds=max(0.0, reply.mono_now - mono))

        with self.sessions() as db:
            last = last_states(db)
        state = {p: on for p, (on, _) in last.items()}
        newest = {p: ts for p, (_, ts) in last.items()}
        rows = []

        def add(pump: str, ts: datetime, on: bool, gap: bool, seq: int | None = None):
            rows.append({"boot_id": reply.boot_id if seq else None, "seq": seq, "pump": pump, "ts": ts,
                         "on": on, "gap": gap})
            state[pump], newest[pump] = on, max(ts, newest.get(pump, ts))

        restarted = bool(link.boot_id) and reply.boot_id != link.boot_id
        fell_behind = reply.boot_id == link.boot_id and reply.first_seq > link.last_seq + 1
        if (restarted or fell_behind) and link.last_contact:
            # Switches were lost. A running pump is taken to have stopped when we last heard
            # from the Pi, unless the Pi says it has been on since before then. The current
            # state below starts it again if it is still on.
            log.warning("Pump sensor: %s; marking a gap", "the sensor restarted" if restarted else "missed switches")
            for p, on in list(state.items()):
                cur = reply.pumps.get(p)
                if on and not (cur and cur.on and at(cur.since_mono) <= link.last_contact):
                    add(p, link.last_contact, False, True)
        for e in reply.events:
            add(e.pump, at(e.mono), e.on, False, e.seq)
        if not reply.more:
            for p, cur in reply.pumps.items():  # the Pi's current state is the final word
                if state.get(p) != cur.on:
                    # Never before a stored row, or the newest row would disagree with the Pi forever.
                    add(p, max(at(cur.since_mono), newest.get(p, at(cur.since_mono))), cur.on, True)
        link.boot_id, link.last_contact = reply.boot_id, now
        link.last_seq = reply.events[-1].seq if reply.more and reply.events else reply.last_seq
        with self.sessions.begin() as db:
            n = db.connection().execute(insert(PumpEvent).on_conflict_do_nothing(), rows).rowcount if rows else 0
            db.merge(AppSetting(key="pump_link", value_json=link.model_dump_json()))
        return n
