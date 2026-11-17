"""Boiler page: burner state, temperatures, cycles and fuel, read from the boiler itself."""

from nicegui import ui
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.processing.boiler import CONDENSING_F
from heatmon.web import data
from heatmon.web.components import CARD, INK, MUTED, SERIES, chart, date_range, frame, layout, stat, table
from heatmon.web.pages import _by_period, _fit_scatter, _fmt, _with_events

STALE_MIN = 5  # the "now" panel says the reading is old after this


def _burns(n: int) -> str:
    return f"{n} burn" + ("" if n == 1 else "s")


def register(cfg: Config, sessions: sessionmaker[Session]) -> None:
    @ui.page("/boiler", title="Boiler · Heating Monitor")
    def page(start: str | None = None, end: str | None = None):
        c = data.Ctx(cfg, sessions, *date_range(cfg, start, end))
        frame(cfg, sessions, "/boiler", c.start, c.end)
        if not data.has_boiler_data(c):
            with ui.card().classes(CARD + " w-full"):
                ui.label("No boiler data yet").classes("text-sm font-semibold")
                ui.label("Connect the boiler's service port (see the README), then turn on boiler collection "
                         "under the gear button at the top right.").classes("text-sm text-gray-500")
            return

        now = data.boiler_now(c)
        with ui.row().classes("w-full gap-3 pt-2"):
            old = now["age_min"] > STALE_MIN
            stat("Boiler", now["mode"] if not old else "No recent reading",
                 f"burner {now['state']}, read {now['time']}")
            stat("Firing rate", _fmt(now["rate_pct"] if now["flame"] else 0, "%"))
            stat("Supply / return", f"{_fmt(now['outlet_f'], '°F')} / {_fmt(now['inlet_f'], '°F')}",
                 f"target {_fmt(now['target_f'], '°F')}" if now["flame"] else "")
            stat("System supply", _fmt(now["system_f"], "°F"))
            stat("Hot water tank", _fmt(now["tank_f"], "°F"), f"set to {_fmt(now['dhw_setpoint_f'], '°F')}")
            stat("Outdoor (boiler sensor)", _fmt(now["outdoor_f"], "°F"))

        rows = data.boiler_rows(c)
        samples, days, cycles = data.boiler_samples(c, rows[0]), data.boiler_daily(c, rows), data.boiler_cycles(c, rows[1])
        events = data.events(c)
        x = [s["time"] for s in samples]

        def line(name, key, color, width=1.5, dash=None, axis="y"):
            return {"type": "scatter", "mode": "lines", "name": name, "x": x, "y": [s[key] for s in samples],
                    "yaxis": axis, "line": {"color": color, "width": width, **({"dash": dash} if dash else {})},
                    "hovertemplate": "%{y:.1f}°F" if key.endswith("_f") else "%{y:.0f}%"}

        chart("Boiler temperatures (°F)", [
            line("Supply", "outlet_f", SERIES[1], 2), line("Return", "inlet_f", SERIES[0], 2),
            line("System supply", "system_f", SERIES[2]), {**line("Target", "target_f", INK, 1, "dot"),
             "y": [s["target_f"] if s["firing_pct"] else None for s in samples]},
            line("Flue", "flue_f", MUTED, 1), line("Hot water tank", "tank_f", SERIES[3], 1)],
            _with_events(layout(360, yaxis={"title": {"text": "°F"}}), events))
        chart("Firing rate and outdoor temperature", [
            {**line("Firing rate", "rate_pct", SERIES[1]), "fill": "tozeroy", "fillcolor": "rgba(235,104,52,0.14)",
             "y": [s["rate_pct"] if s["firing_pct"] else 0 for s in samples]},
            line("Outdoor", "outdoor_f", SERIES[0], 1.5, axis="y2")],
            _with_events(layout(280, margin={"r": 56}, yaxis={"title": {"text": "% of full fire"}, "range": [0, 105]},
                                yaxis2={"overlaying": "y", "side": "right", "showgrid": False,
                                        "tickfont": {"color": MUTED}, "title": {"text": "outdoor °F"}}), events))

        if days:
            heat = [d for d in days if d["sh_cycles"] or d["sh_burn_h"]]
            short = sum(d["short_cycles"] for d in days)
            firing = sum(d["sh_burn_h"] * 60 + d["dhw_burn_min"] for d in days)
            with ui.row().classes("w-full gap-3"):
                stat("Heating burner time", _fmt(sum(d["sh_burn_h"] for d in days), " h", 1),
                     _burns(sum(d["sh_cycles"] for d in days)))
                stat("Hot water burner time", _fmt(sum(d["dhw_burn_min"] for d in days), " min"),
                     _burns(sum(d["dhw_cycles"] for d in days)))
                stat("Short heating burns", f"{short}", "under 10 min, stopped on setpoint met")
                stat("Time at minimum fire", _fmt(sum(d["min_fire_h"] for d in days), " h", 1),
                     "where the low-fire noise happens")
                cond = [d["condensing_pct"] * (d["sh_burn_h"] * 60 + d["dhw_burn_min"]) for d in days
                        if d["condensing_pct"] is not None]
                stat("Condensing", _fmt(sum(cond) / firing if firing else None, "%"),
                     f"of firing time with return below {CONDENSING_F}°F")
                stat("Propane burned (est.)", _fmt(sum(d["boiler_gal_est"] for d in days), " gal", 1),
                     "from the firing rate")
            dx = [d["date"] for d in days]
            chart("Burner time per day (today so far)", [
                {"type": "bar", "name": "Heating", "x": dx, "y": [d["sh_burn_h"] for d in days],
                 "marker": {"color": SERIES[1]}, "hovertemplate": "%{y:.1f} h"},
                {"type": "bar", "name": "Hot water", "x": dx, "y": [round(d["dhw_burn_min"] / 60, 2) for d in days],
                 "marker": {"color": SERIES[0]}, "hovertemplate": "%{y:.2f} h"},
                {"type": "scatter", "mode": "lines+markers", "name": "Heating burns", "x": dx,
                 "y": [d["sh_cycles"] for d in days], "yaxis": "y2", "line": {"color": INK, "width": 2},
                 "hovertemplate": "%{y} burns"}],
                layout(320, barmode="stack", bargap=0.25, margin={"r": 56}, xaxis={"type": "category"},
                       yaxis={"title": {"text": "hours"}},
                       yaxis2={"overlaying": "y", "side": "right", "showgrid": False, "rangemode": "tozero",
                               "tickfont": {"color": MUTED}, "title": {"text": "burns"}}))
            traces, notes = _fit_scatter(_by_period(c, heat, "sh_burn_h"), " h")
            chart("Heating burner hours per day against degree days, by settings period", traces,
                  layout(340, hovermode="closest", xaxis={"title": {"text": "heating degree days"}},
                         yaxis={"title": {"text": "burner hours per day"}}))
            for n in notes:
                ui.label(n).classes("text-sm text-gray-700")
            chart("Propane per day: boiler firing rate against the tank (est., gallons)", [
                {"type": "bar", "name": "Boiler, heating", "x": dx, "y": [d["sh_gal_est"] for d in days],
                 "marker": {"color": SERIES[1]}},
                {"type": "bar", "name": "Boiler, hot water", "x": dx, "y": [d["dhw_gal_est"] for d in days],
                 "marker": {"color": SERIES[0]}},
                {"type": "scatter", "mode": "markers", "name": "Tank estimate", "x": dx,
                 "y": [d["tank_gal_est"] for d in days], "marker": {"color": INK, "size": 8}}],
                layout(280, barmode="stack", bargap=0.25, xaxis={"type": "category"},
                       yaxis={"title": {"text": "gallons"}}))
            ui.label(f"Boiler gallons assume {cfg.boiler.input_btu_h:,.0f} BTU/h at full fire, fuel in proportion "
                     f"to the firing rate, and a fuel factor of {cfg.boiler.fuel_factor:g}. The tank reads in "
                     f"{cfg.propane.tank_size_gal / 100:.0f}-gallon steps, so compare totals over weeks, not days.") \
                .classes("text-xs text-gray-500")

        if cycles:
            kinds = [("heating", SERIES[1]), ("hot water", SERIES[0])]
            chart("Length of each burn (short burns ringed)", [
                {"type": "scatter", "mode": "markers", "name": name, "x": [k["start"] for k in cycles if k["mode"] == name],
                 "y": [k["minutes"] for k in cycles if k["mode"] == name],
                 "text": [k["ended"] for k in cycles if k["mode"] == name],
                 "marker": {"color": color, "size": 8, "line": {"color": [INK if k["short"] else "white" for k in cycles
                                                                          if k["mode"] == name], "width": 2}},
                 "hovertemplate": "%{x}: %{y:.1f} min, %{text}<extra></extra>"} for name, color in kinds],
                _with_events(layout(280, hovermode="closest", yaxis={"title": {"text": "minutes"},
                                                                     "type": "log"}), events))

        curve = [s for s in samples if s["mode"] == "sh" and s["firing_pct"] and s["outdoor_f"] is not None]
        if curve:
            chart("Outdoor reset check: heating target against outdoor temperature", [{
                "type": "scatter", "mode": "markers", "x": [s["outdoor_f"] for s in curve],
                "y": [s["target_f"] for s in curve], "marker": {"color": SERIES[1], "size": 5},
                "hovertemplate": "%{x:.0f}°F outside: target %{y:.1f}°F<extra></extra>"}],
                layout(260, showlegend=False, hovermode="closest", xaxis={"title": {"text": "outdoor °F"}},
                       yaxis={"title": {"text": "target °F"}}))

        table("Boiler by day", days[::-1], "boiler_daily", c.start, c.end)
        table("Burns", cycles[::-1], "boiler_cycles", c.start, c.end)
        table("Boiler setting changes",
              [e for e in events[::-1] if e["title"].startswith("Boiler settings changed")], None, rows_per_page=5)
