"""Overview, Zones, Runtime, Propane and Weather pages."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from nicegui import ui
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.web import data
from heatmon.web.components import (AXIS, CARD, GRID, HEATING_BAND, INK, INK_2, MUTED, SERIES, chart, date_range,
                                    event_markers, frame, layout, stat, table)

MIN_FIT_DAYS = 14


def _fmt(v, unit: str = "", digits: int = 0) -> str:
    return "—" if v is None else f"{v:,.{digits}f}{unit}"


def _with_events(lay: dict, events: list[dict]) -> dict:
    shapes, notes = event_markers(events)
    lay["shapes"] = lay.get("shapes", []) + shapes
    lay["annotations"] = lay.get("annotations", []) + notes
    return lay


def _by_period(c: data.Ctx, days: list[dict], y_key: str) -> dict[str, list[dict]]:
    """Days that have both HDD and y, grouped by settings period, oldest period first."""
    periods = data.settings_periods(c)
    groups = {p.label: [] for p in periods}
    for d in days:
        if d["hdd"] is not None and d[y_key] is not None:
            groups[data.period_of_day(date.fromisoformat(d["date"]), periods, c.tz)].append(
                {"label": d["date"], "days": 1, "hdd": d["hdd"], "y": d[y_key]})
    return groups


def _bins(groups: dict[str, list[dict]], size: int = 7, min_days: int = 4) -> dict[str, list[dict]]:
    """Average consecutive days in groups of `size`, never mixing periods. A short last
    group is kept only if it has at least `min_days` days."""
    out = {}
    for label, pts in groups.items():
        chunks = [pts[i:i + size] for i in range(0, len(pts), size)]
        out[label] = [{"label": f"{ch[0]['label']} to {ch[-1]['label']}", "days": len(ch),
                       "hdd": sum(p["hdd"] for p in ch) / len(ch), "y": sum(p["y"] for p in ch) / len(ch)}
                      for ch in chunks if len(ch) >= min_days]
    return out


def _fit_scatter(groups: dict[str, list[dict]], unit: str) -> tuple[list[dict], list[str]]:
    """Points per settings period with a fitted line each, plus one summary line per period."""
    labels = list(groups)
    colors = {label: (SERIES[i] if i < 2 else MUTED) for i, label in enumerate(reversed(labels))}
    traces, notes = [], []
    for label in labels:
        pts = groups[label]
        if not pts:
            notes.append(f"{label}: not enough data in this range yet.")
            continue
        xs, ys = [p["hdd"] for p in pts], [p["y"] for p in pts]
        days = sum(p["days"] for p in pts)
        traces.append({"type": "scatter", "mode": "markers", "name": label, "x": xs, "y": ys,
                       "text": [p["label"] for p in pts],
                       "hovertemplate": "%{text}<br>%{x:.1f} HDD a day, %{y:.1f}" + unit + " a day<extra></extra>",
                       "marker": {"color": colors[label], "size": 10, "line": {"color": "white", "width": 2}}})
        fit = data.fit_line(xs, ys)
        if fit is None:
            notes.append(f"{label}: {len(pts)} points from {days} days, not enough for a fitted line.")
            continue
        lo, hi = min(xs), max(xs)
        traces.append({"type": "scatter", "mode": "lines", "name": f"{label} fit", "showlegend": False,
                       "x": [lo, hi], "y": [fit.intercept + fit.slope * lo, fit.intercept + fit.slope * hi],
                       "line": {"color": colors[label], "width": 2}, "hoverinfo": "skip"})
        caution = " Fewer than 14 days, so treat this as rough." if days < MIN_FIT_DAYS else ""
        notes.append(f"{label}: {fit.slope:.2f}{unit} per HDD, and {fit.intercept:.1f}{unit} a day at zero HDD "
                     f"(R² {fit.r2:.2f}, {fit.n} points from {days} days).{caution}")
    return traces, notes


def register(cfg: Config, sessions: sessionmaker[Session]) -> None:
    def ctx(start, end) -> data.Ctx:
        return data.Ctx(cfg, sessions, *date_range(cfg, start, end))

    @ui.page("/", title="Overview · Heating Monitor")
    def overview():
        frame(cfg, "/")

        @ui.refreshable
        def body():
            today = datetime.now(ZoneInfo(cfg.timezone)).date()
            c = data.Ctx(cfg, sessions, today - timedelta(weeks=12), today)
            now = data.latest(c)
            with ui.row().classes("w-full gap-3 pt-2"):
                stat("Outdoor", _fmt(now["outdoor_f"], "°F"), f"station, {now['outdoor_time'] or 'no data'}")
                stat("Sun", _fmt(now["solar_wm2"], " W/m²"), "Open-Meteo estimate, last hour")
                stat("Propane", _fmt(now["tank_pct"], "%"),
                     f"about {_fmt(now['tank_gal'], ' gal')}, read {now['tank_time'] or 'never'}")
            with ui.row().classes("w-full gap-3"):
                for z in now["zones"]:
                    with ui.card().classes(CARD + " min-w-48"):
                        ui.label(z["zone"]).classes("text-xs text-gray-500")
                        ui.label(_fmt(z["temp_f"], "°F", 1)).classes("text-2xl font-semibold")
                        ui.label(f"set to {_fmt(z['setpoint_f'], '°F', 1)}").classes("text-xs text-gray-500")
                        with ui.row().classes("items-center gap-1"):
                            if z["heating"]:
                                ui.icon("local_fire_department").style(f"color:{SERIES[1]}")
                                ui.label("Calling for heat").classes("text-sm")
                            else:
                                ui.icon("pause_circle").classes("text-gray-400")
                                ui.label("Not calling").classes("text-sm text-gray-500")

            days = {d["date"]: d for d in data.daily(data.Ctx(cfg, sessions, today - timedelta(days=1), today))}
            t, y = days.get(today.isoformat(), {}), days.get((today - timedelta(days=1)).isoformat(), {})
            ui.label("Today so far and yesterday").classes("text-sm font-semibold pt-2")
            with ui.row().classes("w-full gap-3"):
                stat("Heating degree days", f"{_fmt(t.get('hdd'), digits=1)} / {_fmt(y.get('hdd'), digits=1)}",
                     "today's appears once the day is over")
                stat("Any zone calling", f"{_fmt(t.get('any_zone_call_min'), ' min')} / "
                                         f"{_fmt(y.get('any_zone_call_min'), ' min')}", "updated hourly")
                stat("Propane used (est.)", f"{_fmt(t.get('gal_used_est'), ' gal', 1)} / "
                                            f"{_fmt(y.get('gal_used_est'), ' gal', 1)}",
                     "needs tank readings that cover the whole day")

            weeks = [w for w in data.weekly_propane(c) if w["gal_per_hdd"] is not None]
            with ui.row().classes("w-full gap-3 items-stretch"):
                with ui.column().classes("grow"):
                    if weeks:
                        chart("Propane per degree day, by week", [{
                            "type": "scatter", "mode": "lines+markers", "name": "gal/HDD",
                            "x": [w["week_of"] for w in weeks], "y": [w["gal_per_hdd"] for w in weeks],
                            "line": {"color": SERIES[0], "width": 2}, "marker": {"size": 8},
                            "hovertemplate": "week of %{x}: %{y:.3f} gal/HDD<extra></extra>"}],
                            layout(180, showlegend=False, margin={"t": 8, "b": 32}))
                    else:
                        with ui.card().classes(CARD + " w-full"):
                            ui.label("Propane per degree day, by week").classes("text-sm font-semibold")
                            ui.label("Appears after the first full week with tank readings.") \
                                .classes("text-sm text-gray-500")
                with ui.card().classes(CARD + " min-w-64"):
                    ui.label("Data freshness").classes("text-sm font-semibold")
                    sources = data.freshness(c)
                    if not sources:
                        ui.label("No source has run yet.").classes("text-sm text-gray-500")
                    for s in sources:
                        ago = "never" if s["minutes_ago"] is None else f"{s['minutes_ago']} min ago"
                        with ui.row().classes("items-center gap-2"):
                            ui.icon("error" if s["failing"] else "check_circle") \
                                .style(f"color:{'#d03b3b' if s['failing'] else '#0ca30c'}")
                            ui.label(f"{s['source']}: {ago}{', failing' if s['failing'] else ''}").classes("text-sm")

        body()
        ui.timer(60, body.refresh)

    @ui.page("/zones", title="Zones · Heating Monitor")
    def zones(start: str | None = None, end: str | None = None):
        c = ctx(start, end)
        frame(cfg, "/zones", c.start, c.end)
        readings, calls, events = data.thermostat(c), data.zone_calls(c), data.events(c)
        outdoor = [h for h in data.weather_hourly(c) if h["temp_f"] is not None]
        for i, z in enumerate(data.zone_names(c)):
            rs = [r for r in readings if r["zone"] == z]
            zc = [k for k in calls if k["zone"] == z]
            vals = [v for r in rs for v in (r["temp_f"], r["setpoint_f"]) if v is not None]
            lo, hi = (min(vals) - 1, max(vals) + 1) if vals else (60, 75)
            band_x, band_y = [], []
            for k in zc:
                band_x += [k["start"], k["start"], k["end"], k["end"], k["start"], None]
                band_y += [lo, hi, hi, lo, lo, None]
            traces = [
                {"type": "scatter", "mode": "lines", "name": "Calling for heat", "x": band_x, "y": band_y,
                 "fill": "toself", "fillcolor": HEATING_BAND, "line": {"width": 0}, "hoverinfo": "skip"},
                {"type": "scatter", "mode": "lines", "name": "Setpoint", "x": [r["time"] for r in rs],
                 "y": [r["setpoint_f"] for r in rs], "line": {"color": INK_2, "width": 1.5, "shape": "hv"}},
                {"type": "scatter", "mode": "lines", "name": "Room", "x": [r["time"] for r in rs],
                 "y": [r["temp_f"] for r in rs], "line": {"color": SERIES[i], "width": 2}},
                {"type": "scatter", "mode": "lines", "name": "Outdoor", "x": [h["time"] for h in outdoor],
                 "y": [h["temp_f"] for h in outdoor], "yaxis": "y2", "line": {"color": MUTED, "width": 1.5}},
            ]
            lay = layout(380, xaxis={"anchor": "y2"},
                         yaxis={"domain": [0.36, 1], "range": [lo, hi], "title": {"text": "Indoor °F"}},
                         yaxis2={"domain": [0, 0.26], "title": {"text": "Outdoor °F"}, "gridcolor": GRID,
                                 "linecolor": AXIS, "zeroline": False, "tickfont": {"color": MUTED}})
            hours = sum(k["minutes"] for k in zc) / 60
            overs = [k["overshoot_f"] for k in zc if k["overshoot_f"] is not None]
            title = (f"{z}: {len(zc)} calls, {hours:.1f} h calling"
                     + (f", average overshoot {sum(overs) / len(overs):.1f}°F" if overs else ""))
            chart(title, traces, _with_events(lay, events))
        table("Calls for heat (overshoot: highest °F above setpoint in the 6 hours after a call)",
              calls, "zone_calls", c.start, c.end)
        table("Events", events, "events", c.start, c.end, 5)

    @ui.page("/runtime", title="Runtime · Heating Monitor")
    def runtime(start: str | None = None, end: str | None = None):
        c = ctx(start, end)
        frame(cfg, "/runtime", c.start, c.end)
        days, events, names = data.daily(c), data.events(c), data.zone_names(c)
        x = [d["date"] for d in days]
        bars = [{"type": "bar", "name": z, "x": x, "y": [d[f"{z} min"] for d in days],
                 "marker": {"color": SERIES[i], "line": {"color": "white", "width": 1}},
                 "hovertemplate": "%{y:.0f} min"} for i, z in enumerate(names)]
        bars.append({"type": "scatter", "mode": "lines+markers", "name": "Any zone calling", "x": x,
                     "y": [d["any_zone_call_min"] for d in days], "line": {"color": INK, "width": 2},
                     "marker": {"size": 6}, "hovertemplate": "%{y:.0f} min"})
        chart("Minutes calling for heat per day (today so far). Bars add up each zone; "
              "the line counts overlapping calls once.",
              bars, _with_events(layout(340, barmode="stack", bargap=0.25, legend={"traceorder": "normal"},
                                        yaxis={"title": {"text": "minutes"}}), events))

        traces, notes = _fit_scatter(_by_period(c, days, "any_zone_call_min"), " min")
        chart("Any-zone calling time against degree days, one point per day, by settings period", traces,
              layout(340, hovermode="closest", xaxis={"title": {"text": "heating degree days"}},
                     yaxis={"title": {"text": "minutes per day"}}))
        for n in notes:
            ui.label(n).classes("text-sm text-gray-700")

        counts = [{"type": "scatter", "mode": "lines+markers", "name": z, "x": x,
                   "y": [d[f"{z} calls"] for d in days], "line": {"color": SERIES[i], "width": 2},
                   "marker": {"size": 6}} for i, z in enumerate(names)]
        chart("Calls per day by zone, today so far (many short calls suggest short cycling)", counts,
              _with_events(layout(300, yaxis={"title": {"text": "calls"}}), events))
        keep = ["date", "hdd", "any_zone_call_min", "total_zone_call_min", "zone_calls"]
        keep += [f"{z} {kind}" for z in names for kind in ("min", "calls")]
        table("Daily runtime", [{k: d[k] for k in keep} for d in days], "daily", c.start, c.end)

    @ui.page("/propane", title="Propane · Heating Monitor")
    def propane(start: str | None = None, end: str | None = None):
        c = ctx(start, end)
        frame(cfg, "/propane", c.start, c.end)
        readings, fills, events = data.tank(c), data.deliveries(c), data.events(c)
        traces = [{"type": "scatter", "mode": "lines+markers", "name": "Tank level",
                   "x": [r["time"] for r in readings], "y": [r["level_pct"] for r in readings],
                   "line": {"color": SERIES[0], "width": 1.5}, "marker": {"size": 8},
                   "hovertemplate": "%{y:.0f}%<extra></extra>"}]
        if fills:
            def fill_time(f):  # the first reading on that day at the new level
                return next((r["time"] for r in readings if r["time"].startswith(f["date"])
                             and r["level_pct"] == f["tank_pct_after"]), f["date"])
            traces.append({"type": "scatter", "mode": "markers", "name": "Fill",
                           "x": [fill_time(f) for f in fills], "y": [f["tank_pct_after"] for f in fills],
                           "text": [f"+{f['gallons']:.0f} gal ({f['source']})" for f in fills],
                           "hovertemplate": "%{text}<extra></extra>",
                           "marker": {"symbol": "triangle-up", "size": 12, "color": SERIES[2]}})
        chart(f"Tank level, %. Each 1% is about {cfg.propane.tank_size_gal / 100:.0f} gallons.", traces,
              _with_events(layout(300, yaxis={"title": {"text": "% full"}}), events))

        days = data.daily(c)
        weeks = data.weekly_propane(c, days)
        chart("Gallons used per week (days with a full estimate only)", [{
            "type": "bar", "name": "gallons", "x": [f"{date.fromisoformat(w['week_of']):%b %-d}" for w in weeks],
            "y": [w["gallons"] for w in weeks],
            "text": [f"{w['days_with_data']} days" for w in weeks],
            "hovertemplate": "week of %{x}: %{y:.0f} gal from %{text}<extra></extra>", "textposition": "none",
            "marker": {"color": SERIES[0]}}],
            layout(280, showlegend=False, xaxis={"type": "category", "title": {"text": "week starting"}}))

        traces, notes = _fit_scatter(_bins(_by_period(c, days, "gal_used_est")), " gal")
        chart("Gallons per day against degree days, averaged over 7-day stretches, by settings period", traces,
              layout(340, hovermode="closest", xaxis={"title": {"text": "heating degree days"}},
                     yaxis={"title": {"text": "gallons per day"}}))
        for n in notes:
            ui.label(n).classes("text-sm text-gray-700")
        ui.label("Daily gallons are rough: the tank reports in whole percents. Weekly totals are more reliable.") \
            .classes("text-sm text-gray-500")
        table("By week", weeks, "weekly_propane", c.start, c.end)
        table("Fills", fills, "deliveries", c.start, c.end, 5)
        table("Tank readings", readings, "tank", c.start, c.end)

    @ui.page("/weather", title="Weather · Heating Monitor")
    def weather(start: str | None = None, end: str | None = None):
        c = ctx(start, end)
        frame(cfg, "/weather", c.start, c.end)
        hours, events = data.weather_hourly(c), data.events(c)
        x = [h["time"] for h in hours]
        filled = [h for h in hours if h["temp_source"] == "open-meteo"]
        temp = [{"type": "scatter", "mode": "lines", "name": "Outdoor", "x": x, "y": [h["temp_f"] for h in hours],
                 "line": {"color": SERIES[0], "width": 2}, "hovertemplate": "%{y:.1f}°F"}]
        if filled:
            temp.append({"type": "scatter", "mode": "markers", "name": "Hours filled from Open-Meteo",
                         "x": [h["time"] for h in filled], "y": [h["temp_f"] for h in filled],
                         "marker": {"color": MUTED, "size": 6}, "hoverinfo": "skip"})
        chart("Outdoor temperature, hourly (°F)", temp, _with_events(layout(300), events))
        chart("Sunshine, hourly (W/m², Open-Meteo estimate)", [{
            "type": "scatter", "mode": "lines", "name": "Sun", "x": x, "y": [h["solar_wm2"] for h in hours],
            "fill": "tozeroy", "fillcolor": "rgba(42,120,214,0.15)", "line": {"color": SERIES[0], "width": 1.5},
            "hovertemplate": "%{y:.0f} W/m²"}], _with_events(layout(240, showlegend=False), events))
        chart("Wind at the station, hourly (mph)", [
            {"type": "scatter", "mode": "lines", "name": "Average", "x": x, "y": [h["wind_mph"] for h in hours],
             "line": {"color": SERIES[0], "width": 2}},
            {"type": "scatter", "mode": "lines", "name": "Highest gust", "x": x, "y": [h["gust_mph"] for h in hours],
             "line": {"color": SERIES[1], "width": 1.5}}], _with_events(layout(260), events))
        keep = ("date", "hdd", "mean_temp_f", "min_temp_f", "max_temp_f", "solar_kwh_m2", "weather_hours",
                "open_meteo_hours")
        table("Daily weather", [{k: d[k] for k in keep} for d in data.daily(c)], "daily", c.start, c.end)
