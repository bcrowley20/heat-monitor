"""Shared page pieces: navigation, the date range picker, chart styling and tables."""

from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from nicegui import ui

from heatmon.config import Config

PAGES = [("/", "Overview"), ("/zones", "Zones"), ("/runtime", "Runtime"), ("/propane", "Propane"),
         ("/weather", "Weather"), ("/compare", "Compare"), ("/events", "Events & settings"), ("/status", "Status")]
RANGELESS = {"/compare", "/events", "/status"}  # pages that don't use the shared date range
DEFAULT_DAYS = 14

# Validated reference palette (light mode). Categorical slots are used in this order.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE, PAGE_BG = "#e1e0d9", "#c3c2b7", "#fcfcfb", "#f9f9f7"
HEATING_BAND = "rgba(235,104,52,0.14)"  # light wash of series orange
SUN_BAND = "rgba(237,161,0,0.16)"  # pale wash of series yellow
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
CARD = "p-3 shadow-none border border-gray-200"


def date_range(cfg: Config, start: str | None, end: str | None) -> tuple[date, date]:
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    try:
        e = date.fromisoformat(end) if end else today
        s = date.fromisoformat(start) if start else e - timedelta(days=DEFAULT_DAYS - 1)
    except ValueError:
        e, s = today, today - timedelta(days=DEFAULT_DAYS - 1)
    s, e = min(s, e), max(s, e)
    return min(s, today), min(e, today)  # nothing is recorded in the future


def _query(start: date, end: date) -> str:
    return "?" + urlencode({"start": start.isoformat(), "end": end.isoformat()})


def frame(cfg: Config, path: str, start: date | None = None, end: date | None = None) -> None:
    """Page header with navigation and, when a range is given, the range picker."""
    ui.query("body").style(f"background:{PAGE_BG}; font-family:{FONT}")
    q = _query(start, end) if start else ""
    with ui.row().classes("w-full items-center gap-4 pb-2 border-b"):
        ui.label("Heating Monitor").classes("text-lg font-semibold mr-4")
        for href, name in PAGES:
            link = ui.link(name, href + ("" if href in RANGELESS else q)).classes("no-underline")
            link.classes("font-semibold text-black" if href == path else "text-gray-600")
    if start is None:
        return

    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    season = date(today.year if today.month >= 7 else today.year - 1, 10, 1)
    presets = [("7 days", today - timedelta(days=6)), ("14 days", today - timedelta(days=13)),
               ("30 days", today - timedelta(days=29)), ("90 days", today - timedelta(days=89)),
               ("This season", season)]
    with ui.row().classes("w-full items-center gap-2 py-2"):
        for label, s in presets:
            selected = (start, end) == (s, today)
            ui.button(label, on_click=lambda s=s: ui.navigate.to(path + _query(s, today))) \
                .props(f"{'unelevated' if selected else 'flat'} dense no-caps size=sm")
        with ui.button(f"{start:%b %-d, %Y} – {end:%b %-d, %Y}", icon="event").props("flat dense no-caps size=sm"):
            with ui.menu():
                picker = ui.date({"from": start.isoformat().replace("-", "/"),
                                  "to": end.isoformat().replace("-", "/")}).props("range")

                def go():
                    v = picker.value  # {"from", "to"} for a range, or one "YYYY/MM/DD" for a single day
                    if isinstance(v, str) and v:
                        v = {"from": v, "to": v}
                    if isinstance(v, dict) and v.get("from") and v.get("to"):
                        s, e = (date.fromisoformat(v[k].replace("/", "-")) for k in ("from", "to"))
                        ui.navigate.to(path + _query(s, e))
                ui.button("Apply", on_click=go).props("flat dense")


def layout(height: int = 320, **extra) -> dict:
    axis = {"gridcolor": GRID, "linecolor": AXIS, "zeroline": False, "tickfont": {"color": MUTED},
            "title": {"font": {"color": INK_2, "size": 12}}}
    base = {
        "height": height,
        "font": {"family": FONT, "size": 12, "color": INK_2},
        "paper_bgcolor": SURFACE, "plot_bgcolor": SURFACE,
        "margin": {"l": 56, "r": 16, "t": 32, "b": 40},
        "xaxis": dict(axis), "yaxis": dict(axis),
        "legend": {"orientation": "h", "x": 0, "y": 1.02, "yanchor": "bottom", "font": {"color": INK_2}},
        "hovermode": "x unified",
        "hoverlabel": {"bgcolor": "white", "font": {"family": FONT, "color": INK}},
    }
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


def event_markers(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """A hairline at each event, with a small marker on top that shows the title on hover."""
    shapes = [{"type": "line", "xref": "x", "yref": "paper", "x0": e["time"], "x1": e["time"],
               "y0": 0, "y1": 1, "line": {"color": MUTED, "width": 1}} for e in events]
    notes = [{"x": e["time"], "xref": "x", "y": 1, "yref": "paper", "yanchor": "bottom",
              "text": "▼", "showarrow": False, "font": {"color": MUTED, "size": 10},
              "hovertext": f"{e['time']}: {e['title']}"} for e in events]
    return shapes, notes


def chart(title: str, data: list[dict], lay: dict) -> None:
    with ui.card().classes(CARD + " w-full"):
        ui.label(title).classes("text-sm font-semibold text-gray-800")
        ui.plotly({"data": data, "layout": lay, "config": {"displaylogo": False, "responsive": True}}) \
            .classes("w-full").style(f"height:{lay['height']}px")


def table(title: str, rows: list[dict], dataset: str | None, start: date | None = None,
          end: date | None = None, rows_per_page: int = 10) -> None:
    with ui.card().classes(CARD + " w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(title).classes("text-sm font-semibold text-gray-800")
            if dataset:
                ui.link("Download CSV", f"/api/{dataset}.csv" + (_query(start, end) if start else "")) \
                    .classes("text-sm")
        if not rows:
            ui.label("No data in this range yet.").classes("text-sm text-gray-500")
            return
        cols = [{"name": k, "label": k.replace("_", " "), "field": k, "align": "left", "sortable": True}
                for k in rows[0]]
        ui.table(columns=cols, rows=rows, pagination=rows_per_page).props("dense flat").classes("w-full")


def stat(label: str, value: str, sub: str = "") -> None:
    with ui.card().classes(CARD + " min-w-40"):
        ui.label(label).classes("text-xs text-gray-500")
        ui.label(value).classes("text-2xl font-semibold text-gray-900")
        if sub:
            ui.label(sub).classes("text-xs text-gray-500")
