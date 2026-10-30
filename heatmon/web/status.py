"""Status page: source health, the latest raw readings and table sizes."""

from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.web.components import frame
from heatmon.web.data import c_to_f
from heatmon.db.models import (DailySummary, PropaneDelivery, SourceStatus, TankReading,
                               ThermostatReading, WeatherObs, Zone, ZoneCall)

SOURCES = {"nest": "nest_s", "wunderground": "weather_s", "centriconnect": "tank_s",
           "open-meteo": "open_meteo_s", "summaries": "summaries_s", "alerts": "alerts_s"}


def status_data(cfg: Config, sessions: sessionmaker[Session], active: set[str]) -> dict:
    tz = ZoneInfo(cfg.timezone)

    def local(ts: datetime | None) -> str:
        return ts.astimezone(tz).strftime("%Y-%m-%d %H:%M") if ts else ""

    with sessions() as db:
        statuses = {s.source: s for s in db.scalars(select(SourceStatus))}
        sources = []
        for name, interval_field in SOURCES.items():
            st = statuses.get(name)
            sources.append({
                "source": name,
                "collecting": "yes" if name in active else "no",
                "every_min": getattr(cfg.intervals, interval_field) // 60,
                "last_success": local(st.last_success_ts) if st else "",
                "last_error": local(st.last_error_ts) if st else "",
                "error": (st.last_error_msg or "") if st else "",
            })

        latest_ts = (select(ThermostatReading.device_id, func.max(ThermostatReading.ts).label("ts"))
                     .group_by(ThermostatReading.device_id).subquery())
        thermostats = [{
            "zone": zone_name or "(no zone)",
            "device": r.device_id[-8:],
            "time": local(r.ts),
            "temp_f": c_to_f(r.ambient_c),
            "setpoint_f": c_to_f(r.setpoint_heat_c),
            "humidity": r.humidity_pct,
            "mode": r.mode,
            "eco": r.eco_mode,
            "hvac": r.hvac_status,
            "online": r.connectivity,
        } for r, zone_name in db.execute(
            select(ThermostatReading, Zone.name)
            .join(latest_ts, (ThermostatReading.device_id == latest_ts.c.device_id)
                  & (ThermostatReading.ts == latest_ts.c.ts))
            .outerjoin(Zone, Zone.id == ThermostatReading.zone_id)
            .order_by(Zone.name))]

        weather = [{
            "time": local(w.ts), "temp_f": w.temp_f,
            "humidity": w.humidity_pct, "dewpoint_f": w.dewpoint_f, "wind_mph": w.wind_mph,
            "gust_mph": w.gust_mph, "solar_wm2": w.solar_wm2, "pressure_inhg": w.pressure_inhg,
        } for w in db.scalars(select(WeatherObs).where(WeatherObs.source == "wu")
                              .order_by(WeatherObs.ts.desc()).limit(6))]

        tank = [{
            "time": local(t.ts), "level_pct": t.level_pct, "remaining_gal": t.remaining_gal,
            "tank_size_gal": t.tank_size_gal, "alert": t.alert_status, "battery_pct": t.battery_pct,
        } for t in db.scalars(select(TankReading).order_by(TankReading.ts.desc()).limit(6))]

        counts = [{"table": m.__tablename__, "rows": db.scalar(select(func.count()).select_from(m))}
                  for m in (ThermostatReading, WeatherObs, TankReading, ZoneCall,
                            PropaneDelivery, DailySummary)]

    size_mb = sum(p.stat().st_size for p in cfg.data_dir.glob("heatmon.db*")) / 1e6
    return {"sources": sources, "thermostats": thermostats, "weather": weather, "tank": tank,
            "counts": counts, "db_size_mb": round(size_mb, 1)}


def _table(title: str, rows: list[dict]) -> None:
    ui.label(title).classes("text-lg font-medium mt-4")
    if not rows:
        ui.label("No data yet").classes("text-gray-500")
        return
    columns = [{"name": k, "label": k.replace("_", " "), "field": k, "align": "left"}
               for k in rows[0]]
    ui.table(columns=columns, rows=rows).props("dense flat bordered").classes("w-full")


def register(cfg: Config, sessions: sessionmaker[Session], active: set[str]) -> None:
    @ui.page("/status", title="Status · Heating Monitor")
    def status_page():
        @ui.refreshable
        def content():
            d = status_data(cfg, sessions, active)
            _table("Data sources", d["sources"])
            _table("Thermostats (latest)", d["thermostats"])
            _table("Outdoor weather from the station (latest)", d["weather"])
            _table("Propane tank (latest)", d["tank"])
            _table(f"Stored rows (database {d['db_size_mb']} MB)", d["counts"])

        frame(cfg, sessions, "/status")
        content()
        ui.timer(30, content.refresh)
