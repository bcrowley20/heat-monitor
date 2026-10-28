"""Import Nest thermostat history from a Google Takeout zip.

Takeout has a line for every 15 minutes per thermostat in
Takeout/Nest/thermostats/<serial>/<year>/<month>/HvacRuntime.jsonl. Each line is JSON
wrapped in CSV quoting. Room names come from the device graph files in the same zip.
"""

import csv
import io
import json
import logging
import re
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.db.models import ThermostatHistory, Zone
from heatmon.db.session import insert_ignore
from heatmon.sources.nest import _match_zone

log = logging.getLogger(__name__)

RUNTIME = re.compile(r"Takeout/Nest/thermostats/([^/]+)/\d{4}/\d{2}/HvacRuntime\.jsonl$")
BATCH = 5000


def _records(z: zipfile.ZipFile, name: str):
    with z.open(name) as f:
        for row in csv.reader(io.TextIOWrapper(f, encoding="utf-8")):
            if row:
                try:
                    yield json.loads(row[0])
                except ValueError:
                    continue


def _first(z: zipfile.ZipFile, name: str) -> dict:
    return next(_records(z, name), {})


def device_names(z: zipfile.ZipFile) -> dict[str, set[str]]:
    """Serial number -> the names it goes by: its room and its own label, if any."""
    files = z.namelist()
    where: dict[str, str] = {}
    for name in files:
        if name.endswith("/structuredata/nest.trait.located.LocatedAnnotationsTrait.jsonl"):
            payload = _first(z, name).get("payload", {})
            for group in ("predefinedWheres", "customWheres"):
                items = payload.get(group, {})
                for w in items.values() if isinstance(items, dict) else items:
                    where[w["whereId"]["resourceId"]] = w["label"]["literal"]
    names: dict[str, set[str]] = {}
    devices = {m.group(1) for n in files if (m := re.search(r"(.*/DEVICE_[0-9A-F]+)/devicedata/", n))}
    for d in devices:
        def trait(suffix):
            match = [n for n in files if n.startswith(d + "/devicedata/") and n.endswith(suffix)]
            return _first(z, match[0]).get("payload", {}) if match else {}
        serial = trait("DeviceIdentityTrait.jsonl").get("serialNumber")
        if not serial:
            continue
        room = where.get(trait("DeviceLocatedSettingsTrait.jsonl").get("whereAnnotationRid", {}).get("resourceId"))
        names[serial] = {n for n in (room, trait("LabelSettingsTrait.jsonl").get("label")) if n}
    return names


def import_takeout(cfg: Config, sessions: sessionmaker[Session], path: Path) -> dict[str, dict]:
    """Store every 15-minute record for thermostats that match a zone. Safe to rerun."""
    with sessions() as db:
        zones = list(db.scalars(select(Zone)))
    retired = {z.name: z.retired for z in cfg.zones if z.retired}
    report: dict[str, dict] = {}
    with zipfile.ZipFile(path) as z:
        names = device_names(z)
        by_serial = defaultdict(list)
        for name in z.namelist():
            if m := RUNTIME.search(name):
                by_serial[m.group(1)].append(name)
        for serial, files in sorted(by_serial.items()):
            zone = _match_zone(serial, names.get(serial, set()), zones)
            report[serial] = {"names": sorted(names.get(serial, set())), "zone": zone.name if zone else None,
                              "added": 0, "records": 0}
            if zone is None:
                continue  # e.g. a thermostat for a building the boiler doesn't heat
            until = retired.get(zone.name)
            rows: list[dict] = []
            for name in sorted(files):
                for r in _records(z, name):
                    start = datetime.fromisoformat(r["interval_start"])
                    if until and start.date() >= until:
                        continue
                    rows.append({"device_id": serial, "interval_start": start, "zone_id": zone.id,
                                 "heating_s": int(r.get("heating_time") or 0), "indoor_c": r.get("indoor_temp"),
                                 "setpoint_c": r.get("heating_target"), "humidity_pct": r.get("indoor_humidity")})
                    if len(rows) >= BATCH:
                        report[serial]["added"] += insert_ignore(sessions, ThermostatHistory, rows)
                        report[serial]["records"] += len(rows)
                        rows = []
            report[serial]["added"] += insert_ignore(sessions, ThermostatHistory, rows)
            report[serial]["records"] += len(rows)
            log.info("Takeout %s (%s): %d records, %d new", serial, zone.name,
                     report[serial]["records"], report[serial]["added"])
    return report
