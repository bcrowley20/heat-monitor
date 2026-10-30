"""Compare two periods, such as before and after a settings change."""

from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from nicegui import ui
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import Event
from heatmon.processing.days import local_date
from heatmon.web import data
from heatmon.web.components import CARD, date_range, frame

DEFAULT_DAYS = 14


def _parse(d: str | None) -> date | None:
    try:
        return date.fromisoformat(d) if d else None
    except ValueError:
        return None


def register(cfg: Config, sessions: sessionmaker[Session]) -> None:
    tz = ZoneInfo(cfg.timezone)

    @ui.page("/compare", title="Compare · Heating Monitor")
    def page(event: str | None = None, days: int = DEFAULT_DAYS, a1: str | None = None, a2: str | None = None,
             b1: str | None = None, b2: str | None = None, keep: int = 0):
        frame(cfg, sessions, "/compare")
        today = datetime.now(tz).date()
        with sessions() as db:
            evs = db.scalars(select(Event).where(Event.type.in_(("setting_change", "service")))
                             .order_by(Event.ts.desc())).all()
        ev_names = {e.id: f"{e.ts.astimezone(tz):%b %-d, %Y}: {e.title[:70]}" for e in evs}
        ranges = all(_parse(x) for x in (a1, a2, b1, b2))
        days = max(1, min(days, 365))
        event_id = int(event) if event and event.isdigit() else None
        chosen = next((e for e in evs if e.id == event_id), None) or next(
            (e for e in evs if e.type == "setting_change"), None)

        if ranges:
            (sa, ea), (sb, eb) = date_range(cfg, a1, a2), date_range(cfg, b1, b2)
            a_name, b_name = "Period A", "Period B"
        elif chosen:
            change = local_date(chosen.ts, tz)  # the changeover day itself is in neither period
            sa, ea = change - timedelta(days=days), change - timedelta(days=1)
            sb, eb = change + timedelta(days=1), min(change + timedelta(days=days), today)
            a_name, b_name = "Before", "After"
        else:
            sa = ea = sb = eb = None

        # ---------- controls ----------
        with ui.card().classes(CARD + " w-full"):
            mode = ui.toggle({"event": "Before and after an event", "ranges": "Two date ranges"},
                             value="ranges" if ranges else "event")
            with ui.row().classes("items-end gap-2") as event_row:
                ev_pick = ui.select(ev_names, value=chosen.id if chosen else None, label="Event").classes("w-[32rem]")
                n_days = ui.number("Days each side", value=days, min=1, max=365, precision=0).classes("w-32")
            with ui.row().classes("items-end gap-2") as range_row:
                inputs = [ui.input(lbl, value=(v.isoformat() if v else "")).props("type=date dense")
                          for lbl, v in (("A from", sa), ("A to", ea), ("B from", sb), ("B to", eb))]
            event_row.bind_visibility_from(mode, "value", value="event")
            range_row.bind_visibility_from(mode, "value", value="ranges")
            keep_box = ui.checkbox("Include days with fireplace or away events", value=bool(keep))

            def go():
                q = {"keep": int(keep_box.value)}
                if mode.value == "event":
                    q |= {"days": int(n_days.value or DEFAULT_DAYS)}
                    if ev_pick.value:
                        q["event"] = ev_pick.value
                else:
                    q |= dict(zip(("a1", "a2", "b1", "b2"), (i.value for i in inputs)))
                ui.navigate.to("/compare?" + urlencode(q))
            ui.button("Compare", on_click=go).props("unelevated dense")

        if sa is None:
            ui.label("Add a setting change on the Events & settings page, or pick two date ranges.")
            return

        # ---------- results ----------
        exclude = set() if keep else data.excluded_days(data.Ctx(cfg, sessions, sa, eb))
        ctx = data.Ctx(cfg, sessions, sa, eb)
        fit, price = data.invoice_baseline(ctx).fit, data.price_per_gal(ctx)
        a = data.period_stats(data.Ctx(cfg, sessions, sa, ea), exclude, fit, price)
        b = data.period_stats(data.Ctx(cfg, sessions, sb, eb), exclude, fit, price)

        with ui.card().classes(CARD + " w-full"):
            ui.label(f"{a_name}: {sa:%b %-d} to {ea:%b %-d, %Y}.  {b_name}: {sb:%b %-d} to {eb:%b %-d, %Y}.") \
                .classes("text-sm font-semibold")
            for sentence in data.compare_summary(a, b, a_name, b_name):
                ui.label(sentence).classes("text-sm")

        def fmt(v, unit=""):
            if v is None:
                return ""
            return f"${v:,.2f}" if unit == "$" else f"{v:,}{unit}"

        price_note = f" at ${price:.2f}/gal" if price else ""
        rows = [
            ("Days compared", "days", ""), ("Days with propane estimates", "days_with_propane", ""),
            ("Days with thermostat data", "days_with_thermostats", ""), ("Average outdoor temperature", "mean_temp_f", "°F"),
            ("Heating degree days", "hdd", ""), ("Gallons used (estimated)", "gallons", ""),
            ("Gallons per HDD", "gal_per_hdd", ""), ("Gallons per HDD, fitted slope", "gal_per_hdd_fit", ""),
            ("Invoice baseline predicts (gallons)", "baseline_gallons", ""),
            ("Difference from invoice baseline", "vs_baseline_pct", "%"),
            (f"Cost per HDD{price_note}", "cost_per_hdd", "$"),
            ("Zone-minutes per HDD, all zones added up", "zone_min_per_hdd", ""),
            ("Zone-minutes per HDD, fitted slope", "zone_min_per_hdd_fit", ""),
            ("Minutes with any zone calling per HDD", "call_min_per_hdd", ""),
            ("Days of thermostat data from Google Takeout", "estimated_days", ""),
            ("Calls per day, all zones", "calls_per_day", ""), ("Average overshoot", "overshoot_f", "°F"),
        ]
        base_col = {"gal_per_hdd_fit": fmt(round(fit.gal_per_hdd, 3)) if fit else ""}
        table_rows = [{"measure": label, "a": fmt(a[k], u), "b": fmt(b[k], u), "base": base_col.get(k, "")}
                      for label, k, u in rows]
        if fit:
            table_rows.insert(8, {"measure": "Base load, gallons a day", "a": "", "b": "",
                                  "base": fmt(round(fit.base_gal_per_day, 2))})
        cols = [{"name": "measure", "label": "", "field": "measure", "align": "left"},
                {"name": "a", "label": a_name, "field": "a", "align": "right"},
                {"name": "b", "label": b_name, "field": "b", "align": "right"},
                {"name": "base", "label": "Invoice baseline", "field": "base", "align": "right"}]
        with ui.card().classes(CARD + " w-full"):
            ui.table(columns=cols, rows=table_rows, row_key="measure").props("dense flat hide-bottom") \
                .classes("w-full")
            ui.label("Days with a fireplace or away event are left out unless you include them. Gallons per HDD "
                     "here includes hot water and cooking; the invoice baseline's slope is heating only, so "
                     "compare the periods with each other, or each period with the baseline's prediction.") \
                .classes("text-xs text-gray-500")
            if not fit:
                ui.label("The invoice baseline appears after deliveries are imported and the weather backfill "
                         "covers them.").classes("text-xs text-gray-500")
