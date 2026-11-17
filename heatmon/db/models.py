"""Database tables. Timestamps are stored as UTC ISO-8601 text."""

import datetime as dt
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


class ZoneCall(Base):
    """A period when a zone's thermostat called for heat. Rebuilt from thermostat_readings."""

    __tablename__ = "zone_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    zone_id: Mapped[int] = mapped_column(ForeignKey("zones.id"))
    start_ts: Mapped[datetime]
    end_ts: Mapped[datetime]
    duration_s: Mapped[int]
    start_ambient_c: Mapped[float | None]
    end_ambient_c: Mapped[float | None]
    setpoint_c: Mapped[float | None]
    open: Mapped[bool]  # still calling at the latest reading
    gap_closed: Mapped[bool]  # readings stopped mid-call, so the end time is the last reading
    __table_args__ = (Index("ix_zone_calls_zone_start", "zone_id", "start_ts"),
                      Index("ix_zone_calls_end", "end_ts"))


class PropaneDelivery(Base):
    __tablename__ = "propane_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date]
    gallons: Mapped[float]
    price_per_gal: Mapped[float | None]
    total_cost: Mapped[float | None]
    tank_pct_before: Mapped[float | None]
    tank_pct_after: Mapped[float | None]
    source: Mapped[str]  # 'invoice' or 'detected' (a jump in tank level)
    notes: Mapped[str | None]


class DailySummary(Base):
    """One row per local day. Rebuilt from the raw tables."""

    __tablename__ = "daily_summary"
    date: Mapped[dt.date] = mapped_column(primary_key=True)
    hdd: Mapped[float | None]
    mean_temp_f: Mapped[float | None]
    min_temp_f: Mapped[float | None]
    max_temp_f: Mapped[float | None]
    solar_kwh_m2: Mapped[float | None]
    weather_hours: Mapped[int]  # hours with an outdoor temperature
    open_meteo_hours: Mapped[int]  # of those, hours filled from Open-Meteo
    any_zone_call_min: Mapped[float]
    total_zone_call_min: Mapped[float]
    zone_call_count: Mapped[int]
    per_zone_call_min_json: Mapped[str] = mapped_column(Text)
    per_zone_call_count_json: Mapped[str] = mapped_column(Text)
    gal_used_est: Mapped[float | None]
    gal_per_hdd: Mapped[float | None]
    # Where the zone figures came from: 'live' polling, 'history' (Google Takeout's
    # 15-minute records), or None when there is no thermostat data for the day.
    runtime_source: Mapped[str | None]


class Event(Base):
    """Something that happened to the house or heating system. Marked on charts."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime]
    type: Mapped[str]  # setting_change, delivery, service, fireplace, away, note
    title: Mapped[str]
    details: Mapped[str | None] = mapped_column(Text)


class BoilerSettingsSnapshot(Base):
    """A dated record of the boiler's settings."""

    __tablename__ = "boiler_settings_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    effective_from: Mapped[datetime | None]  # None: in place since before records began
    label: Mapped[str]
    params_json: Mapped[str] = mapped_column(Text)


class ThermostatHistory(Base):
    """15-minute records from a Google Takeout export of Nest data."""

    __tablename__ = "thermostat_history"
    device_id: Mapped[str] = mapped_column(primary_key=True)  # the thermostat's serial number
    interval_start: Mapped[datetime] = mapped_column(primary_key=True)
    zone_id: Mapped[int | None] = mapped_column(ForeignKey("zones.id"))
    heating_s: Mapped[int]  # seconds of the 15 minutes spent calling for heat
    indoor_c: Mapped[float | None]
    setpoint_c: Mapped[float | None]
    humidity_pct: Mapped[float | None]
    __table_args__ = (Index("ix_thermostat_history_zone_start", "zone_id", "interval_start"),)


class AppSetting(Base):
    """Settings edited in the app, stored as JSON by name."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)


class Alert(Base):
    """A condition that needed attention. Open until cleared_ts is set."""

    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]  # tank_low, source_stale, room_cold
    subject: Mapped[str]  # what it is about: 'tank', a source name or a zone name
    message: Mapped[str] = mapped_column(Text)
    started_ts: Mapped[datetime]
    cleared_ts: Mapped[datetime | None]
    # Whether Pushover has the start and clear messages. Unsent ones are retried.
    start_sent: Mapped[bool] = mapped_column(default=False)
    clear_sent: Mapped[bool] = mapped_column(default=False)


class PumpEvent(Base):
    """A boiler pump switching on or off, from the Pi pump sensor."""

    __tablename__ = "pump_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    # The Pi's run ID and switch number. Unset for a switch heatmon added to fill a gap.
    boot_id: Mapped[str | None]
    seq: Mapped[int | None]
    pump: Mapped[str]  # boiler, system or dhw
    ts: Mapped[datetime]
    on: Mapped[bool]
    gap: Mapped[bool] = mapped_column(default=False)  # time is a guess: contact with the Pi was lost
    __table_args__ = (Index("ux_pump_events_boot_seq", "boot_id", "seq", unique=True),
                      Index("ix_pump_events_pump_ts", "pump", "ts"))


class BoilerSample(Base):
    """One minute of boiler status: averages of the reads in that minute, plus the last read's
    state. raw_json keeps the last read's whole register block for a while, for decoding."""

    __tablename__ = "boiler_samples"
    ts: Mapped[datetime] = mapped_column(primary_key=True)  # start of the minute
    samples: Mapped[int]
    outlet_f: Mapped[float | None]
    inlet_f: Mapped[float | None]
    system_f: Mapped[float | None]
    flue_f: Mapped[float | None]
    tank_f: Mapped[float | None]
    outdoor_f: Mapped[float | None]
    target_f: Mapped[float | None]
    sh_setpoint_f: Mapped[float | None]
    dhw_setpoint_f: Mapped[float | None]
    rate_pct: Mapped[float | None]
    fan_rpm: Mapped[float | None]
    pump_pct: Mapped[float | None]
    state: Mapped[int]  # burner state code
    mode: Mapped[str]  # sh, dhw or idle
    flame_s: Mapped[float]  # seconds of flame in the minute
    btu: Mapped[float]  # fuel burned in the minute, from the firing rate
    sh_demand: Mapped[bool]
    dhw_demand: Mapped[bool]
    raw_json: Mapped[str | None] = mapped_column(Text)


class BoilerCycle(Base):
    """One burner run, from flame on to flame off."""

    __tablename__ = "boiler_cycles"
    start_ts: Mapped[datetime] = mapped_column(primary_key=True)
    end_ts: Mapped[datetime]
    mode: Mapped[str]  # sh, dhw or mixed
    end_reason: Mapped[str]  # demand_ended, setpoint_met (demand still on) or gap (contact lost)
    partial: Mapped[bool]  # the start or end wasn't seen
    avg_rate_pct: Mapped[float | None]
    min_fire_s: Mapped[float]
    btu: Mapped[float]
    max_outlet_f: Mapped[float | None]
    avg_inlet_f: Mapped[float | None]
    avg_flue_f: Mapped[float | None]


class BoilerParams(Base):
    """The boiler's parameter registers, saved when any setting changes."""

    __tablename__ = "boiler_params"
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    values_json: Mapped[str] = mapped_column(Text)  # {"1000": value, ...}, counters left out


class BoilerCounters(Base):
    """The boiler's lifetime counters, read hourly."""

    __tablename__ = "boiler_counters"
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    power_on_h: Mapped[int]
    burner_starts: Mapped[int]
    ignitions: Mapped[int]  # successful ones, probably
    burner_h: Mapped[int]  # probably burner hours; burner_h_2 rises with it
    burner_h_2: Mapped[int]
    sh_cycles: Mapped[int]
    dhw_cycles: Mapped[int]
