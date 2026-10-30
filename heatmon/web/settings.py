"""The settings button in the page header: alerts, the pump sensor, open alerts, and popups."""

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import httpx
from nicegui import ui
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.alerts import AlertSettings, load_settings, save_settings, send_pushover
from heatmon.config import Config
from heatmon.db.models import Alert
from heatmon.sources import pumps

POPUP_CHECK_S = 30


@dataclass
class Seen:
    """What one open page already knows about, so each change pops up once."""

    last_id: int = 0
    open_ids: set[int] = field(default_factory=set)


def open_alerts(sessions: sessionmaker[Session]) -> list[Alert]:
    with sessions() as db:
        return list(db.scalars(select(Alert).where(Alert.cleared_ts.is_(None)).order_by(Alert.started_ts)))


def changes(sessions: sessionmaker[Session], seen: Seen) -> list[tuple[str, str]]:
    """Alerts that started or cleared since the page last looked, as (popup type, message).
    Updates `seen`."""
    with sessions() as db:
        rows = db.scalars(select(Alert).where(or_(Alert.cleared_ts.is_(None), Alert.id > seen.last_id,
                                                  Alert.id.in_(seen.open_ids))).order_by(Alert.id)).all()
    out = []
    for a in rows:
        if a.id > seen.last_id:
            out.append(("negative", a.message))
        if a.cleared_ts and (a.id > seen.last_id or a.id in seen.open_ids):
            out.append(("positive", "Cleared: " + a.message))
    seen.last_id = max([seen.last_id] + [a.id for a in rows])
    seen.open_ids = {a.id for a in rows if a.cleared_ts is None}
    return out


def settings_button(cfg: Config, sessions: sessionmaker[Session]) -> None:
    tz = ZoneInfo(cfg.timezone)

    with ui.dialog() as dialog, ui.card().classes("w-[36rem] max-w-full"):
        # Built each time the dialog opens, so it never shows settings saved from another tab as stale.
        @ui.refreshable
        def form():
            ui.label("Settings").classes("text-lg font-semibold")
            rows = open_alerts(sessions)
            ui.label("Open alerts").classes("text-sm font-semibold")
            if not rows:
                ui.label("None.").classes("text-sm text-gray-500")
            for a in rows:
                ui.label(f"{a.started_ts.astimezone(tz):%b %-d %H:%M}  {a.message}").classes("text-sm text-red-700")

            s = load_settings(sessions)
            ui.label("Alerts").classes("text-sm font-semibold pt-2")
            with ui.row().classes("items-center gap-2"):
                tank_on = ui.switch("Propane tank below", value=s.tank_low_on)
                tank_pct = ui.number(value=s.tank_low_pct, suffix="%", min=0, max=100).props("dense").classes("w-24")
            with ui.row().classes("items-center gap-2"):
                stale_on = ui.switch("A data source sends nothing for", value=s.source_stale_on)
                stale_h = ui.number(value=s.source_stale_hours, suffix="hours", min=0.5).props("dense").classes("w-28")
            with ui.row().classes("items-center gap-2"):
                cold_on = ui.switch("A room is more than", value=s.room_cold_on)
                cold_f = ui.number(value=s.room_cold_f, suffix="°F", min=0.5).props("dense").classes("w-24")
                ui.label("below its setpoint for")
                cold_h = ui.number(value=s.room_cold_hours, suffix="hours", min=0.5).props("dense").classes("w-28")
            popups = ui.switch("Show popups on these pages", value=s.popups)

            ui.label("Pushover").classes("text-sm font-semibold pt-2")
            ui.label("Get your user key and create an application token at pushover.net. "
                     "Leave these blank to skip phone notifications.").classes("text-xs text-gray-500")
            token = ui.input("Application token", value=s.pushover_app_token, password=True,
                             password_toggle_button=True).classes("w-full")
            user = ui.input("User key", value=s.pushover_user_key, password=True,
                            password_toggle_button=True).classes("w-full")

            ps = pumps.load_settings(sessions)
            ui.label("Boiler pump sensor").classes("text-sm font-semibold pt-2")
            ui.label("The Cardputer that watches the System and DHW pumps. "
                     "See the README to set it up.").classes("text-xs text-gray-500")
            with ui.row().classes("w-full items-center gap-2"):
                pump_on = ui.switch("Collect from the sensor", value=ps.enabled)
                address = ui.input("Sensor address", value=ps.address, placeholder="192.168.1.50 or 192.168.1.50:8081") \
                    .classes("grow")

            async def test_sensor():
                try:
                    async with httpx.AsyncClient() as client:
                        reply = await pumps.fetch(client, address.value or "")
                except Exception as e:
                    ui.notify(f"No answer from the pump sensor: {type(e).__name__}: {e}", type="negative")
                    return
                states = ", ".join(f"{pumps.PUMP_NAMES.get(n, n)} {'on' if p.on else 'off'}"
                                   for n, p in reply.pumps.items())
                ui.notify(f"Pump sensor answered: {states}.", type="positive")

            def current() -> AlertSettings | None:
                try:
                    return AlertSettings(
                        pushover_app_token=(token.value or "").strip(), pushover_user_key=(user.value or "").strip(),
                        popups=popups.value, tank_low_on=tank_on.value, tank_low_pct=tank_pct.value,
                        source_stale_on=stale_on.value, source_stale_hours=stale_h.value,
                        room_cold_on=cold_on.value, room_cold_f=cold_f.value, room_cold_hours=cold_h.value)
                except ValidationError:
                    ui.notify("Percent must be 0 to 100, and degrees and hours must be above zero.", type="warning")
                    return None

            def save():
                if (s := current()) is None:
                    return
                if pump_on.value:
                    try:
                        pumps.base_url(address.value or "")
                    except ValueError:
                        ui.notify("Enter the sensor's address, or turn the pump sensor off.", type="warning")
                        return
                save_settings(sessions, s)
                pumps.save_settings(sessions, pumps.PumpSettings(enabled=pump_on.value,
                                                                 address=(address.value or "").strip()))
                ui.notify("Settings saved. Alerts are checked every few minutes.")
                dialog.close()

            async def test():
                if (s := current()) is None:
                    return
                if not (s.pushover_app_token and s.pushover_user_key):
                    ui.notify("Enter the application token and user key first.", type="warning")
                    return
                try:
                    await send_pushover(s, "Heating Monitor", "Test message from the heating monitor.")
                    ui.notify("Test message sent.", type="positive")
                except Exception as e:
                    ui.notify(f"Pushover did not accept the message: {type(e).__name__}", type="negative")

            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ui.button("Test sensor", on_click=test_sensor).props("flat")
                ui.button("Test Pushover", on_click=test).props("flat")
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Save", on_click=save).props("unelevated")
        form()

    def show():
        form.refresh()
        dialog.open()

    with ui.button(icon="settings", on_click=show).props("flat round dense").classes("ml-auto"):
        badge = ui.badge("", color="red").props("floating")

    seen = Seen()

    def check(popup: bool = True):
        new = changes(sessions, seen)
        badge.text, badge.visible = str(len(seen.open_ids)), bool(seen.open_ids)
        if popup and load_settings(sessions).popups:
            for kind, message in new:
                ui.notify(message, type=kind, timeout=0, close_button=True, position="top-right")
    check(popup=False)  # alerts already open when the page loads don't pop up
    ui.timer(POPUP_CHECK_S, check)
