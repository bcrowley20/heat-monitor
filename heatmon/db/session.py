"""Engine setup, migrations, and small shared database helpers."""

from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Zone as ZoneConfig
from heatmon.db.models import Base, SourceStatus, Zone

MIGRATIONS = Path(__file__).parent / "migrations"


def _make_engine(db_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _record):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


def init_db(db_path: Path) -> sessionmaker[Session]:
    """Create or upgrade the database and return a session factory."""
    engine = _make_engine(db_path)
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    return sessionmaker(engine, expire_on_commit=False)


def sync_zones(sessions: sessionmaker[Session], zones: list[ZoneConfig]) -> None:
    """Insert or update zones from config. Zones removed from config are kept for history."""
    with sessions.begin() as db:
        for z in zones:
            row = db.scalar(select(Zone).where(Zone.name == z.name)) or Zone(name=z.name)
            for field in ("nest_device_id", "loops", "area_sqft", "exposure", "notes"):
                setattr(row, field, getattr(z, field))
            db.add(row)


def insert_ignore(sessions: sessionmaker[Session], model: type[Base], rows: list[dict]) -> int:
    """Insert rows, skipping any whose primary key already exists. Returns rows inserted."""
    if not rows:
        return 0
    with sessions.begin() as db:
        result = db.connection().execute(insert(model).on_conflict_do_nothing(), rows)
        return result.rowcount


def record_status(sessions: sessionmaker[Session], source: str, error: str | None = None) -> None:
    now = datetime.now(UTC)
    with sessions.begin() as db:
        row = db.get(SourceStatus, source) or SourceStatus(source=source)
        if error is None:
            row.last_success_ts = now
        else:
            row.last_error_ts, row.last_error_msg = now, error
        db.add(row)
