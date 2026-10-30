"""Events and settings page: events, boiler settings snapshots and propane invoices."""

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from nicegui import events as ui_events
from nicegui import ui
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from heatmon import records
from heatmon.config import Config
from heatmon.db.models import BoilerSettingsSnapshot, Event, PropaneDelivery
from heatmon.web.components import CARD, frame, table


def _confirm(message: str, action) -> None:
    with ui.dialog() as dialog, ui.card():
        ui.label(message)
        with ui.row():
            ui.button("Delete", on_click=lambda: (action(), dialog.close())).props("color=negative unelevated")
            ui.button("Cancel", on_click=dialog.close).props("flat")
    dialog.open()


def register(cfg: Config, sessions: sessionmaker[Session]) -> None:
    tz = ZoneInfo(cfg.timezone)

    def to_utc(day: str, time: str) -> datetime:
        return datetime.fromisoformat(f"{day}T{time or '00:00'}").replace(tzinfo=tz).astimezone(UTC)

    def local(ts: datetime | None) -> tuple[str, str]:
        if ts is None:
            return "", ""
        t = ts.astimezone(tz)
        return t.strftime("%Y-%m-%d"), t.strftime("%H:%M")

    @ui.page("/events", title="Events & settings · Heating Monitor")
    def page():
        frame(cfg, sessions, "/events")

        # ---------- events ----------
        with ui.card().classes(CARD + " w-full"):
            ui.label("Events").classes("text-sm font-semibold")
            ui.label("Setting changes start a new settings period on the Runtime, Propane and Compare pages. "
                     "Fireplace and away days can be left out of comparisons.").classes("text-xs text-gray-500")
            editing: dict = {"id": None}
            with ui.row().classes("w-full items-end gap-2"):
                day = ui.input("Date").props("type=date dense")
                time = ui.input("Time").props("type=time dense")
                kind = ui.select(records.EVENT_TYPES, value="note", label="Type").classes("w-40")
                title = ui.input("Title").classes("grow")
                details = ui.input("Details (optional)").classes("grow")

            def reset():
                editing["id"] = None
                day.value = datetime.now(tz).strftime("%Y-%m-%d")
                time.value = datetime.now(tz).strftime("%H:%M")
                kind.value, title.value, details.value = "note", "", ""
                save_btn.text = "Add event"

            def save():
                try:
                    records.save_event(sessions, to_utc(day.value, time.value), kind.value, title.value or "",
                                       details.value, editing["id"])
                except ValueError as e:
                    ui.notify(str(e), type="warning")
                    return
                ui.notify("Event saved")
                reset()
                event_list.refresh()

            def edit(e: Event):
                editing["id"] = e.id
                day.value, time.value = local(e.ts)
                kind.value, title.value, details.value = e.type, e.title, e.details or ""
                save_btn.text = "Save changes"

            with ui.row():
                save_btn = ui.button("Add event", on_click=save).props("unelevated dense")
                ui.button("Clear", on_click=reset).props("flat dense")
            reset()

            @ui.refreshable
            def event_list():
                with sessions() as db:
                    rows = db.scalars(select(Event).order_by(Event.ts.desc())).all()
                with ui.grid(columns="auto auto 1fr auto").classes("w-full gap-x-4 gap-y-1 items-center text-sm"):
                    for e in rows:
                        ui.label(" ".join(local(e.ts)))
                        ui.label(e.type.replace("_", " ")).classes("text-gray-600")
                        ui.label(e.title + (f" ({e.details})" if e.details else ""))
                        with ui.row().classes("gap-1"):
                            ui.button(icon="edit", on_click=lambda e=e: edit(e)).props("flat dense size=sm")
                            ui.button(icon="delete", on_click=lambda e=e: _confirm(
                                f"Delete the event “{e.title}”?",
                                lambda: (records.delete_event(sessions, e.id), event_list.refresh()))) \
                                .props("flat dense size=sm")
            event_list()

        # ---------- boiler settings ----------
        with ui.card().classes(CARD + " w-full"):
            ui.label("Boiler settings snapshots").classes("text-sm font-semibold")
            ui.label("Each snapshot records every setting as of a date. Numbers, true/false and lists like "
                     "[[15, 5], [20, 5]] are kept as typed; anything else is saved as text.") \
                .classes("text-xs text-gray-500")
            form = ui.column().classes("w-full")

            def snapshots() -> list[BoilerSettingsSnapshot]:
                with sessions() as db:
                    return list(db.scalars(select(BoilerSettingsSnapshot).order_by(
                        BoilerSettingsSnapshot.effective_from.is_(None).desc(), BoilerSettingsSnapshot.effective_from)))

            def open_form(snap: BoilerSettingsSnapshot | None):
                """Edit a snapshot, or start a new one from a copy of the latest."""
                form.clear()
                all_snaps = snapshots()
                base = snap or (all_snaps[-1] if all_snaps else None)
                params = records.flatten(json.loads(base.params_json)) if base else {}
                with form:
                    ui.label("Edit snapshot" if snap else "New snapshot (starts as a copy of the latest)") \
                        .classes("text-sm font-semibold")
                    with ui.row().classes("items-end gap-2"):
                        label = ui.input("Label", value=snap.label if snap else "").classes("w-72")
                        d, t = local(snap.effective_from) if snap else (datetime.now(tz).strftime("%Y-%m-%d"), "")
                        eff_day = ui.input("In effect from (blank: unknown)", value=d).props("type=date dense")
                        eff_time = ui.input("Time", value=t).props("type=time dense")
                    boxes = {}
                    with ui.grid(columns=2).classes("w-full gap-x-4"):
                        for k, v in params.items():
                            boxes[k] = ui.input(k, value=records.show_value(v)).props("dense")
                    with ui.row().classes("items-end gap-2"):
                        new_key = ui.input("New setting name").props("dense")
                        new_val = ui.input("Value").props("dense")

                    def save():
                        values = {k: records.parse_value(b.value or "") for k, b in boxes.items() if (b.value or "").strip()}
                        if (new_key.value or "").strip():
                            values[new_key.value.strip()] = records.parse_value(new_val.value or "")
                        eff = to_utc(eff_day.value, eff_time.value) if eff_day.value else None
                        try:
                            records.save_snapshot(sessions, label.value or "", eff, records.unflatten(values),
                                                  snap.id if snap else None)
                        except ValueError as e:
                            ui.notify(str(e), type="warning")
                            return
                        ui.notify("Snapshot saved")
                        form.clear()
                        snapshot_list.refresh()

                    with ui.row():
                        ui.button("Save snapshot", on_click=save).props("unelevated dense")
                        ui.button("Cancel", on_click=form.clear).props("flat dense")

            @ui.refreshable
            def snapshot_list():
                snaps = snapshots()
                with ui.grid(columns="auto 1fr auto").classes("w-full gap-x-4 gap-y-1 items-center text-sm"):
                    for s in snaps:
                        ui.label(" ".join(local(s.effective_from)).strip() or "unknown start")
                        ui.label(s.label)
                        with ui.row().classes("gap-1"):
                            ui.button(icon="edit", on_click=lambda s=s: open_form(s)).props("flat dense size=sm")
                            ui.button(icon="delete", on_click=lambda s=s: _confirm(
                                f"Delete the snapshot “{s.label}”?",
                                lambda: (records.delete_snapshot(sessions, s.id), snapshot_list.refresh()))) \
                                .props("flat dense size=sm")
                ui.button("New snapshot", icon="add", on_click=lambda: open_form(None)).props("flat dense")
                if len(snaps) >= 2:
                    names = {s.id: s.label for s in snaps}
                    with ui.row().classes("items-end gap-2 pt-2"):
                        a = ui.select(names, value=snaps[-2].id, label="Compare").classes("w-64")
                        b = ui.select(names, value=snaps[-1].id, label="with").classes("w-64")
                    diff_box = ui.column().classes("w-full")

                    def show_diff():
                        diff_box.clear()
                        by_id = {s.id: json.loads(s.params_json) for s in snaps}
                        rows = records.settings_diff(by_id[a.value], by_id[b.value])
                        with diff_box:
                            table("What changed", rows, None, rows_per_page=0)
                    a.on_value_change(show_diff)
                    b.on_value_change(show_diff)
                    show_diff()
            snapshot_list()

        # ---------- propane invoices ----------
        with ui.card().classes(CARD + " w-full"):
            ui.label("Propane deliveries").classes("text-sm font-semibold")
            ui.label("Import a CSV with the columns: " + ", ".join(records.CSV_COLUMNS) + ". Dates can be "
                     "2025-10-30 or 10/30/25; dollar signs, commas and % signs are fine. Rows already imported "
                     "are skipped.").classes("text-xs text-gray-500")
            preview = ui.column().classes("w-full")

            async def uploaded(e: ui_events.UploadEventArguments):
                rows, problems = records.parse_deliveries_csv(await e.file.text())
                preview.clear()
                with preview:
                    for p in problems:
                        ui.label(p).classes("text-sm text-red-700")
                    if problems:
                        ui.label("Fix these lines in the file and upload it again. Nothing has been saved.") \
                            .classes("text-sm")
                        return
                    for w in records.delivery_warnings(rows):
                        ui.label("Check: " + w).classes("text-sm text-amber-800")
                    table(f"Preview: {len(rows)} deliveries in {e.file.name}",
                          [{**r, "date": r["date"].isoformat()} for r in rows], None)

                    def save():
                        added, skipped = records.save_invoices(sessions, rows)
                        ui.notify(f"Saved {added} deliveries" + (f", skipped {skipped} already saved" if skipped else ""))
                        preview.clear()
                        invoice_list.refresh()
                    ui.button(f"Save {len(rows)} deliveries", on_click=save).props("unelevated dense")

            ui.upload(label="Choose a CSV file", on_upload=uploaded, auto_upload=True, max_files=1) \
                .props("accept=.csv flat bordered").classes("w-96")

            @ui.refreshable
            def invoice_list():
                with sessions() as db:
                    rows = db.scalars(select(PropaneDelivery).order_by(PropaneDelivery.date.desc())).all()
                if not rows:
                    ui.label("No deliveries yet.").classes("text-sm text-gray-500")
                    return
                with ui.grid(columns="auto auto auto auto auto 1fr auto").classes("w-full gap-x-4 gap-y-1 items-center text-sm"):
                    for h in ("date", "gallons", "price", "level after", "source", "notes", ""):
                        ui.label(h).classes("text-xs text-gray-500")
                    for r in rows:
                        ui.label(r.date.isoformat())
                        ui.label(f"{r.gallons:,.1f}")
                        ui.label(f"${r.price_per_gal:.2f}" if r.price_per_gal else "")
                        ui.label(f"{r.tank_pct_after:.0f}%" if r.tank_pct_after is not None else "")
                        ui.label(r.source)
                        ui.label(r.notes or "")
                        if r.source == "invoice":
                            ui.button(icon="delete", on_click=lambda r=r: _confirm(
                                f"Delete the {r.date} delivery of {r.gallons:,.1f} gallons?",
                                lambda: (records.delete_delivery(sessions, r.id), invoice_list.refresh()))) \
                                .props("flat dense size=sm")
                        else:
                            ui.label("")  # detected fills are rebuilt from tank readings
            invoice_list()
