"""Records people enter by hand: events, propane invoices and boiler settings snapshots."""

import csv
import io
import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.db.models import BoilerSettingsSnapshot, Event, PropaneDelivery

EVENT_TYPES = ["setting_change", "service", "delivery", "fireplace", "away", "note"]
CSV_COLUMNS = ["date", "gallons", "price_per_gal", "total_cost", "tank_pct_before", "tank_pct_after", "notes"]
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"]


# ---------- events ----------

def save_event(sessions: sessionmaker[Session], ts: datetime, type_: str, title: str,
               details: str | None, event_id: int | None = None) -> None:
    if type_ not in EVENT_TYPES:
        raise ValueError(f"Unknown event type {type_!r}")
    if not title.strip():
        raise ValueError("An event needs a title")
    with sessions.begin() as db:
        e = db.get(Event, event_id) if event_id else Event()
        if e is None:
            raise ValueError("That event was deleted. Clear the form to add it again.")
        e.ts, e.type, e.title, e.details = ts, type_, title.strip(), (details or "").strip() or None
        db.add(e)


def delete_event(sessions: sessionmaker[Session], event_id: int) -> None:
    with sessions.begin() as db:
        if e := db.get(Event, event_id):
            db.delete(e)


# ---------- propane invoices ----------

def _number(text: str, field: str, errors: list[str]) -> float | None:
    """Accepts spreadsheet styles such as '$1,467.02' and '79%'."""
    t = text.strip().replace("$", "").replace(",", "").replace("%", "").strip()
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        errors.append(f"{field} {text.strip()!r} is not a number")
        return None


def _date(text: str, errors: list[str]) -> date | None:
    t = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    errors.append(f"date {t!r} is not a date (use YYYY-MM-DD or M/D/YY)" if t else "date is missing")
    return None


def parse_deliveries_csv(text: str) -> tuple[list[dict], list[str]]:
    """Returns the rows that parsed and a list of problems, each naming its line.

    Blank rows are skipped. Nothing should be saved while there are problems.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = [f.strip() for f in reader.fieldnames or []]
    missing = [c for c in ("date", "gallons") if c not in fields]
    if missing:
        return [], [f"The header row is missing: {', '.join(missing)}. Expected: {', '.join(CSV_COLUMNS)}"]
    rows, problems = [], []
    for line, raw in enumerate(reader, start=2):
        raw = {(k or "").strip(): (v or "") for k, v in raw.items()}
        if not any(v.strip() for v in raw.values()):
            continue
        errors: list[str] = []
        row = {
            "date": _date(raw.get("date", ""), errors),
            "gallons": _number(raw.get("gallons", ""), "gallons", errors),
            "price_per_gal": _number(raw.get("price_per_gal", ""), "price_per_gal", errors),
            "total_cost": _number(raw.get("total_cost", ""), "total_cost", errors),
            "tank_pct_before": _number(raw.get("tank_pct_before", ""), "tank_pct_before", errors),
            "tank_pct_after": _number(raw.get("tank_pct_after", ""), "tank_pct_after", errors),
            "notes": raw.get("notes", "").strip() or None,
        }
        if row["price_per_gal"] is None and row["total_cost"] and row["gallons"]:
            row["price_per_gal"] = round(row["total_cost"] / row["gallons"], 3)
        if row["gallons"] is None and not any("gallons" in e for e in errors):
            errors.append("gallons is missing")
        elif row["gallons"] is not None and row["gallons"] <= 0:
            errors.append("gallons must be more than 0")
        for f in ("tank_pct_before", "tank_pct_after"):
            if row[f] is not None and not 0 <= row[f] <= 100:
                errors.append(f"{f} must be between 0 and 100")
        if None not in (row["tank_pct_before"], row["tank_pct_after"]) and row["tank_pct_before"] > row["tank_pct_after"]:
            errors.append("tank_pct_before is higher than tank_pct_after")
        if errors:
            problems.append(f"Line {line}: " + "; ".join(errors))
        else:
            rows.append(row)
    return rows, problems


def delivery_warnings(rows: list[dict]) -> list[str]:
    """Rows that parse fine but look like data entry slips. Saving is still allowed."""
    out = []
    ds = sorted(rows, key=lambda r: r["date"])
    for i, a in enumerate(ds):
        for b in ds[i + 1:]:
            if a["gallons"] == b["gallons"] and a["total_cost"] is not None and a["total_cost"] == b["total_cost"]:
                out.append(f"{a['date']} and {b['date']} have the same gallons and cost. Is one a copy of the other?")
        if i + 1 < len(ds) and (ds[i + 1]["date"] - a["date"]).days < 7:
            out.append(f"{a['date']} and {ds[i + 1]['date']} are less than a week apart. Check both dates.")
    return out


def save_invoices(sessions: sessionmaker[Session], rows: list[dict]) -> tuple[int, int]:
    """Adds invoice deliveries. A row with the same date and gallons as a saved invoice is
    skipped, so importing the same file twice is harmless. Returns (added, skipped)."""
    added = skipped = 0
    with sessions.begin() as db:
        existing = {(d, g) for d, g in db.execute(
            select(PropaneDelivery.date, PropaneDelivery.gallons).where(PropaneDelivery.source == "invoice"))}
        for r in rows:
            if (r["date"], r["gallons"]) in existing:
                skipped += 1
                continue
            existing.add((r["date"], r["gallons"]))
            db.add(PropaneDelivery(**r, source="invoice"))
            added += 1
    return added, skipped


def delete_delivery(sessions: sessionmaker[Session], delivery_id: int) -> None:
    with sessions.begin() as db:
        if d := db.get(PropaneDelivery, delivery_id):
            db.delete(d)


# ---------- boiler settings snapshots ----------

def flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    """{'a': {'b': 1}} becomes {'a.b': 1}. Lists stay whole."""
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def unflatten(flat: dict[str, Any]) -> dict:
    out: dict = {}
    for key, v in flat.items():
        node = out
        *parents, last = key.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[last] = v
    return out


def parse_value(text: str) -> Any:
    """What someone types in a settings box: numbers, true/false, lists, or plain text."""
    t = text.strip()
    try:
        return json.loads(t)
    except ValueError:
        return t


def show_value(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v)


def settings_diff(a: dict, b: dict) -> list[dict]:
    fa, fb = flatten(a), flatten(b)
    return [{"setting": k, "before": show_value(fa[k]) if k in fa else "", "after": show_value(fb[k]) if k in fb else ""}
            for k in sorted(fa.keys() | fb.keys()) if fa.get(k, ...) != fb.get(k, ...)]


def save_snapshot(sessions: sessionmaker[Session], label: str, effective_from: datetime | None,
                  params: dict, snapshot_id: int | None = None) -> None:
    if not label.strip():
        raise ValueError("A snapshot needs a label")
    with sessions.begin() as db:
        s = db.get(BoilerSettingsSnapshot, snapshot_id) if snapshot_id else BoilerSettingsSnapshot()
        if s is None:
            raise ValueError("That snapshot was deleted. Start a new one instead.")
        s.label, s.effective_from, s.params_json = label.strip(), effective_from, json.dumps(params)
        db.add(s)


def delete_snapshot(sessions: sessionmaker[Session], snapshot_id: int) -> None:
    with sessions.begin() as db:
        if s := db.get(BoilerSettingsSnapshot, snapshot_id):
            db.delete(s)
