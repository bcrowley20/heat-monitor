"""Database tables. Timestamps are stored as UTC ISO-8601 text."""

from datetime import UTC, datetime

from sqlalchemy import ForeignKey, Index, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UtcDateTime(TypeDecorator):
    """Aware datetime stored as text like 2026-10-28T13:00:00+00:00."""

    impl = String(25)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat(timespec="seconds")

    def process_result_value(self, value: str | None, dialect):
        return None if value is None else datetime.fromisoformat(value)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UtcDateTime}


class Zone(Base):
    __tablename__ = "zones"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    nest_device_id: Mapped[str | None]
    loops: Mapped[int | None]
    area_sqft: Mapped[float | None]
    exposure: Mapped[str | None]
    notes: Mapped[str | None]


class ThermostatReading(Base):
    __tablename__ = "thermostat_readings"
    device_id: Mapped[str] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    zone_id: Mapped[int | None] = mapped_column(ForeignKey("zones.id"))
    ambient_c: Mapped[float | None]
    setpoint_heat_c: Mapped[float | None]
    humidity_pct: Mapped[float | None]
    mode: Mapped[str | None]
    eco_mode: Mapped[str | None]
    hvac_status: Mapped[str | None]
    connectivity: Mapped[str | None]
    __table_args__ = (Index("ix_thermostat_readings_zone_ts", "zone_id", "ts"),)


class WeatherObs(Base):
    __tablename__ = "weather_obs"
    source: Mapped[str] = mapped_column(primary_key=True)  # 'wu' or 'open-meteo'
    station_id: Mapped[str] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    temp_f: Mapped[float | None]
    humidity_pct: Mapped[float | None]
    dewpoint_f: Mapped[float | None]
    wind_mph: Mapped[float | None]
    gust_mph: Mapped[float | None]
    solar_wm2: Mapped[float | None]
    precip_rate_inhr: Mapped[float | None]
    pressure_inhg: Mapped[float | None]
    __table_args__ = (Index("ix_weather_obs_ts", "ts"),)


class TankReading(Base):
    __tablename__ = "tank_readings"
    ts: Mapped[datetime] = mapped_column(primary_key=True)  # the device's own report time
    level_pct: Mapped[float]
    remaining_gal: Mapped[float | None]
    tank_size_gal: Mapped[float | None]
    alert_status: Mapped[str | None]
    battery_pct: Mapped[float | None]
    raw_json: Mapped[str | None] = mapped_column(Text)


class SourceStatus(Base):
    __tablename__ = "source_status"
    source: Mapped[str] = mapped_column(primary_key=True)
    last_success_ts: Mapped[datetime | None]
    last_error_ts: Mapped[datetime | None]
    last_error_msg: Mapped[str | None] = mapped_column(Text)
