# Heating Monitor: Implementation Plan

A small self-hosted Python web app that collects data from Nest thermostats, a Weather Underground personal weather station and a CentriConnect propane tank monitor, stores it in SQLite, and shows tables and charts in a browser. Its main purpose is to measure how boiler setting changes affect **propane usage** and **heating runtime**, normalized for weather.

This document is the brief for Claude Code. Build it in the phases at the end, checking in with the owner after each phase.

---

## 1. Background and goals

The house is about 1,850 sq ft in central Washington (Cle Elum area). It has radiant tubing in a concrete slab, five heating zones, and a Lochinvar Knight WHN110 propane condensing boiler that also heats domestic hot water through an indirect tank. A Taco zone controller opens zone actuators on an Uponor return manifold, and each zone is called by a Nest thermostat.

On **2026-10-25** the boiler settings were changed substantially: outdoor reset curve, ramp delay, offset and anti-cycling. More changes are planned: moving the air intake to the sidewall, and combining the bathroom, guest bedroom and laundry loops into one zone.

The owner wants to answer:

1. How much propane is used per day and week, and per heating degree day (HDD)?
2. How long does each zone call for heat, and how long does the system as a whole run, per day and per HDD?
3. Did a given settings change reduce propane per HDD or change runtime patterns?
4. How do room temperatures track their setpoints, especially overshoot after long burns?

Non-goals: controlling the thermostats, a mobile app, or multi-user accounts.

---

## 2. Data sources

### 2.1 Nest thermostats: Google Smart Device Management (SDM) API

- **Access:** Google Device Access program, with a one-time US$5 registration on a consumer Google account (not Workspace). It needs a Google Cloud project with the SDM API enabled, an OAuth 2.0 "Web application" client, and a Device Access Console project, which provides the project ID used in API calls. Reference: <https://developers.google.com/nest/device-access/get-started>
- **Auth:** a one-time OAuth consent in the browser yields a refresh token. Store it and refresh access tokens automatically. Provide a CLI command `heatmon auth nest` that runs the consent flow and saves the token.
- **Read call:** `GET https://smartdevicemanagement.googleapis.com/v1/enterprises/{project-id}/devices` returns all thermostats in one call.
- **Traits to record** (reference: <https://developers.google.com/nest/device-access/api/thermostat>):
  - `sdm.devices.traits.Info.customName`
  - `sdm.devices.traits.Temperature.ambientTemperatureCelsius`
  - `sdm.devices.traits.Humidity.ambientHumidityPercent`
  - `sdm.devices.traits.ThermostatTemperatureSetpoint.heatCelsius`
  - `sdm.devices.traits.ThermostatMode.mode` (HEAT/COOL/HEATCOOL/OFF)
  - `sdm.devices.traits.ThermostatEco.mode` (MANUAL_ECO/OFF)
  - `sdm.devices.traits.ThermostatHvac.status` (HEATING/COOLING/OFF): **the key field for runtime**
  - `sdm.devices.traits.Connectivity.status`
  - `sdm.devices.traits.Settings.temperatureScale`
- **Units:** the API is Celsius only. Store raw Celsius and display in °F.
- **Rate limits (sandbox):** `devices.list` 5 queries/min and `devices.get` 10 queries/min per user. Reference: <https://developers.google.com/nest/device-access/project/limits>
- **Polling interval:** **every 2 minutes** with one `devices.list` call. Hourly polling cannot measure runtime, because zone calls can be shorter than an hour. Make the interval configurable, with a 1-minute minimum.
- **No history:** the API has no historical data, so data starts when the collector starts. Start the collector as early as possible.
- **Optional later (Phase 5):** Pub/Sub events give exact HVAC status transitions. Google-hosted Pub/Sub topics are being discontinued, so any integration must use a **self-hosted topic** (deadline 2026-11-30). Use a pull subscription and keep polling as a fallback.

### 2.2 Outdoor weather: Weather Underground PWS `KWACLEEL38`

- **Confirmed:** KWACLEEL38 is the owner's own station, about 100 ft from the house. **It is the primary and authoritative outdoor temperature source.** As the station owner, the owner qualifies for a free API key at <https://www.wunderground.com/member/api-keys>.
- **API:** The Weather Company PWS API.
- **Endpoints to implement** (verify against the current API documentation during Phase 1):
  - Current: `https://api.weather.com/v2/pws/observations/current?stationId=KWACLEEL38&format=json&units=e&apiKey=KEY`
  - Daily history for backfill: `https://api.weather.com/v2/pws/history/all?stationId=KWACLEEL38&format=json&units=e&date=YYYYMMDD&apiKey=KEY`
- **Fields:** obsTimeUtc, temp, humidity, dewpt, windSpeed, windGust, winddir, solarRadiation, uv, precipRate, precipTotal, pressure. Solar radiation matters because sunny days visibly cut heating demand in this house.
- **Polling interval:** every 10 minutes.
- **Backfill from the owner's own station first.** Use the PWS history endpoints to pull past hourly or daily data for KWACLEEL38 back to the earliest propane invoice in the import. In Phase 1, find out how far back the history API returns data for this key, and record the result in the README.
- **Gap-filling source: Open-Meteo** (free, no key). Use it only for hours the station is missing: while the station is offline, or before its history begins. Mark each row's source and show the share of Open-Meteo hours in any period used for analysis.

### 2.3 Propane tank: CentriConnect / MyPropane

- **Library:** `aiocentriconnect==0.2.3`, the async Python client used by Home Assistant's CentriConnect integration (<https://www.home-assistant.io/integrations/centriconnect/>). Inspect the library's API during Phase 1; it is small and its documentation may be thin.
- **Credentials:** Device ID, Device Authentication Code (on the device setup card), and the CentriConnect/MyPropane User ID (from Account settings in the app).
- **Values:** tank level %, remaining volume, tank size, alert status, battery %, solar %, LTE signal, device temperature.
- **Cadence:** the monitor reports only **2–3 times per day**, and Home Assistant polls every 6 hours. Poll every **2 hours** and store a row only when the reading's timestamp or value changes.
- **Resolution:** a 1% change is several gallons, depending on tank size, so daily usage figures will be noisy. Analysis should work at the weekly level and use regression against HDD (section 5).

### 2.4 Manual inputs (in-app forms)

- **Events/annotations:** settings changes, propane deliveries, service visits, fireplace use, guests or travel, and free-text notes. These mark charts and define before/after comparison periods.
- **Boiler settings snapshots:** a dated record of all Knight parameters. Seed it with the two snapshots in section 7.
- **Propane delivery history import:** the owner has past invoices. Import a CSV (template: `deliveries_template.csv`) with these columns:
  - `date`: YYYY-MM-DD
  - `gallons`
  - `price_per_gal`: optional
  - `total_cost`: optional
  - `tank_pct_before`: optional
  - `tank_pct_after`: optional, usually the fill level, often 80%
  - `notes`

  This provides a pre-collector baseline of gallons per HDD for earlier winters, using the station's own weather history. Validate rows on import and show a preview before saving.

### 2.5 Optional hardware (not in initial scope)

Nest "HEATING" means a **zone is calling**, not that the **boiler is firing**. The boiler may be paused for anti-cycling, satisfied at setpoint, or heating domestic hot water. The boiler's PC port uses a proprietary Lochinvar cable and software, so don't attempt to read it. If true burner runtime is wanted later, a CT-clamp energy monitor on the boiler circuit (for example a Shelly EM) can detect firing by current draw. Design the data model so a `boiler_power` source can be added later.

---

## 3. Architecture

```
┌────────────────────────── one always-on machine on the home LAN ──────────────────────────┐
│                                                                                             │
│   collector (asyncio scheduler)                    web app (FastAPI)                        │
│   ├─ nest poller        every 2 min ──┐            ├─ HTML pages (Jinja2 + HTMX)            │
│   ├─ weather poller     every 10 min ─┼──► SQLite ◄┤─ JSON API for charts                    │
│   ├─ tank poller        every 2 h ────┤   (WAL)    ├─ CSV export                            │
│   └─ aggregator         hourly + nightly ┘          └─ forms: events, settings, CSV import    │
│                                                                                             │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Language:** Python 3.12.
- **Packages:** `fastapi`, `uvicorn`, `httpx`, `aiocentriconnect`, `sqlalchemy` 2.x (or plain `sqlite3`), `alembic` for migrations, `pydantic-settings`, `jinja2`, `apscheduler` (or a simple asyncio loop), `pandas` for aggregation, `pytest`, `respx` for HTTP mocking.
- **Charts:** Plotly.js or Chart.js from a CDN, pinned to an exact version. Server-rendered pages with HTMX for small interactions; no SPA build step.
- **Process model:** a single process runs both the FastAPI app and the scheduler (startup lifespan task). The collector and web app must also run separately (`heatmon collect`, `heatmon serve`) for debugging.
- **Deployment:** an always-on machine at home, such as a Raspberry Pi, NAS or home server. Provide a `Dockerfile` and `docker-compose.yml` with a persistent volume for the database and tokens, plus a systemd unit as an alternative.
- **Configuration:**
  - `.env` holds secrets: Google client ID and secret, SDM project ID, WU API key, CentriConnect credentials, optional basic-auth password.
  - `config.yaml` holds everything else: zone definitions, polling intervals, HDD base temperature, location, tank size, timezone (America/Los_Angeles).
- **Security:**
  - Never commit secrets; add `.env`, the token file and the database to `.gitignore`.
  - Bind to the LAN only, with optional HTTP basic auth.
  - Store the OAuth refresh token in a file with mode 600.
- **Resilience:**
  - Each poller is independent; one source failing must not stop the others.
  - Retry with exponential backoff and respect the rate limits.
  - Log to file and stdout.
  - Keep a `source_status` table (last success, last error) and show it on a Status page.

---

## 4. Data model (SQLite)

All timestamps are UTC ISO-8601 in the database and display in America/Los_Angeles.

```text
zones
  id, name, nest_device_id (nullable), loops, area_sqft, exposure (N/S/E/W), notes
  -- seed: Great Room (3 loops, 600 sq ft, N), Bathroom (1 loop, 130, N),
  --       Guest Bedroom (1 loop, 180, N), Hallway (2 loops + laundry loop, 280 + 70, S), ...
  -- zone membership changes over time (laundry loop moving): zone_membership table below

zone_membership
  id, zone_id, loop_name, effective_from, effective_to (nullable)

thermostat_readings
  ts, device_id, zone_id, ambient_c, setpoint_heat_c, humidity_pct,
  mode, eco_mode, hvac_status, connectivity
  -- PK (device_id, ts); index on (zone_id, ts)

zone_calls                                   -- derived from thermostat_readings
  id, zone_id, start_ts, end_ts, duration_s, start_ambient_c, end_ambient_c, setpoint_c,
  open (bool for an in-progress call)

weather_obs
  ts, station_id, source ('wu'|'open-meteo'), temp_f, humidity_pct, dewpoint_f,
  wind_mph, gust_mph, solar_wm2, precip_in, pressure_inhg
  -- PK (source, station_id, ts)

tank_readings
  ts (reading time reported by device if available, else fetch time), level_pct,
  remaining_gal, tank_size_gal, alert_status, battery_pct, raw_json
  -- store only when changed

propane_deliveries
  id, date, gallons, price_per_gal, total_cost, tank_pct_before, tank_pct_after,
  source ('invoice'|'detected'), notes
  -- 'detected' rows come from level jumps (section 5)

events
  id, ts, type ('setting_change'|'delivery'|'service'|'fireplace'|'away'|'note'), title, details

boiler_settings_snapshots
  id, effective_from, label, params_json

daily_summary                               -- rebuilt nightly, and on demand
  date, hdd65, mean_temp_f, min_temp_f, max_temp_f, solar_kwh_m2,
  any_zone_call_min, per_zone_call_min_json, zone_call_count,
  gal_used_est, gal_per_hdd, notes_count

source_status
  source, last_success_ts, last_error_ts, last_error_msg
```

---

## 5. Calculations

**Heating degree days (HDD), base 65°F (configurable):**
- From hourly mean outdoor temperatures, HDD for a day = Σ max(0, 65 − T_hour) ÷ 24.
- Prefer PWS data and fill hourly gaps from Open-Meteo.

**Zone calls and runtime:**
- Build `zone_calls` from consecutive `hvac_status` samples.
  - A call starts at the first HEATING sample and ends at the first non-HEATING sample.
  - Use the midpoint between samples as the transition time.
  - If the gap between samples exceeds 3× the poll interval, close the call at the last known sample and flag it.
- **System runtime proxy** = the union of all zone calls (minutes per day when any zone is calling). Label it clearly as "zone call time" or "system demand time", not burner runtime.
- Also compute total zone-minutes (the sum across zones) and the number of calls per zone per day. Many short calls indicate short cycling.

**Propane usage:**
- Gallons used between consecutive tank readings = tank_size × (level₁ − level₂) ÷ 100.
- **Delivery detection:** a level increase above a configurable threshold (default +5%) is a fill. Record it as `propane_deliveries.source='detected'` and exclude that interval from usage.
- Small positive changes (sensor noise, temperature effects on propane volume) count as zero usage and are flagged.
- Allocate usage between readings across days in proportion to HDD + base load (below), so daily figures are smooth estimates. Show readings themselves as points.

**Weather-normalized efficiency:**
- Fit gallons/day = a + b × HDD over a selected period, using weekly or multi-day bins.
  - **a** is the non-heating base load: domestic hot water, range, fireplace.
  - **b** is gallons per HDD, the heating efficiency metric.
- Compare **b** before and after any event, with R² and number of days. Warn when the period has fewer than about 14 days or a narrow HDD range.
- Do the same for zone call minutes per HDD.
- Show fireplace and away events as markers. Optionally exclude flagged days.

**Delivery-history baseline:**
- **Usage between deliveries:** for imported invoices, propane used between two deliveries is the gallons of the later delivery. If `tank_pct_before` and `tank_pct_after` are present, use tank_size × (after₁ − before₂) ÷ 100 instead, because it doesn't assume each fill topped up to the same level.
- **Regression:** fit gallons per interval = a × days + b × HDD across all delivery intervals, using HDD from KWACLEEL38 history. Here **b** is the pre-change gallons per HDD and **a** is the daily base load. This is the baseline the new settings are compared against.
- **Display:** show the baseline by heating season (Oct–Apr) on the Propane and Compare pages.
- **Cost:** where prices are available, show cost per HDD and cost per season. Value future usage at the most recent price per gallon (configurable).

---

## 6. Web UI

Use server-rendered pages with a shared date-range picker (default: last 14 days). Every chart shows event markers (vertical lines labeled with the event title). Every table has CSV export.

1. **Overview**
   - Current outdoor temperature and solar; each zone's temperature, setpoint and HEATING status; tank % and estimated gallons; data freshness per source.
   - Today versus yesterday: HDD, zone call minutes, estimated gallons.
   - Sparkline of gallons per HDD by week.
2. **Zones**
   - For each zone, a time-series chart of ambient temperature and setpoint with shaded bands where the zone is calling for heat, plus outdoor temperature on a secondary axis.
   - Table of calls: start, end, duration, temperature at start and end.
   - Overshoot metric: maximum temperature above setpoint in the 6 hours after a call ends.
3. **Runtime**
   - Stacked daily bars of call minutes per zone, with a line for the any-zone-calling time.
   - Scatter of daily any-zone-calling minutes against HDD, colored by settings period.
   - Calls per day per zone.
4. **Propane**
   - Tank level readings as points with detected deliveries marked.
   - Weekly gallons used.
   - Scatter of gallons per day against HDD with a fitted line per settings period.
   - Table of gallons, HDD and gallons/HDD by week.
5. **Compare periods**
   - Pick two date ranges, or "before/after event X".
   - Side-by-side: days, mean outdoor temperature, HDD, gallons, gallons/HDD (regression slope and simple ratio), call minutes/HDD, calls/day, average overshoot.
   - Plain-language summary with a confidence note.
6. **Weather:** outdoor temperature, solar and wind charts; daily table.
7. **Events and settings**
   - Add, edit and delete events.
   - View settings snapshots and diff two of them.
   - Import delivery CSV.
8. **Status:** each source's last success or error, row counts, database size, and the poll schedule.

Provide JSON endpoints behind every chart (`/api/...`) so the data can be reused.

---

## 7. Seed data

**Events:**
- `2026-10-25 20:30` setting_change: "Ramp delay enabled (15/20/25/30/40% at 5 min, step 6 = 60%)"
- `2026-10-25 21:35` setting_change: "Outdoor reset curve entered; SH1 offset 5→10°F; anti-cycling 1→10 min"
- `2026-10-25 21:55` setting_change: "Ramp steps 1–2 raised to 25%; SH1 set point at high outdoor temp raised to 90°F"
- `2026-10-26 14:00` service: "Tech visit: low-fire moaning attributed to over-length venting or dirty heat exchanger. Ramp steps 1–2 returned to 15/20%."

Planned events, added by the owner when they happen: intake moved to sidewall; heat exchanger cleaned; north-side zone combined (bathroom + guest bedroom + laundry).

**Boiler settings snapshot "2026-10-25 new settings":**

```json
{
  "outdoor_reset": {"low_outdoor_temp_f": 5, "sp_at_low_outdoor_f": 110,
                    "high_outdoor_temp_f": 65, "sp_at_high_outdoor_f": 90,
                    "outdoor_air_shutdown_f": 65},
  "sh1_setpoint_f": 120, "sh1_min_setpoint_f": 80, "sh1_max_setpoint_f": 120,
  "sh1_offset_f": 10, "sh1_differential_f": 20, "anti_cycling_min": 10,
  "ramp_delay": {"enabled": true,
                 "steps": [[15,5],[20,5],[25,5],[30,5],[40,5],[60,null]]},
  "boost_temp_f": "confirm", "sh_night_setback": "confirm"
}
```

**Boiler settings snapshot "before 2026-10-25":**

```json
{ "sh1_offset_f": 5, "anti_cycling_min": 1, "ramp_delay": {"enabled": false},
  "outdoor_reset": "unknown; target was ~106–107°F at ~40°F outdoor" }
```

**Boiler facts for display/tooltips:** Lochinvar Knight WHN110 on propane, 110,000 BTU/hr input, 89,000 net output, modulation 20–100% (minimum about 22,000 BTU/hr input). Ramp delay restarts after every domestic hot water call.

---

## 8. Project layout

```
heatmon/
  pyproject.toml
  README.md                  # setup, including step-by-step Nest Device Access walkthrough
  .env.example
  config.example.yaml
  Dockerfile
  docker-compose.yml
  heatmon/
    __init__.py
    cli.py                   # heatmon auth nest | collect | serve | backfill | rebuild-summaries | import-deliveries
    config.py
    db/ models.py, session.py, migrations/
    sources/
      base.py                # Source protocol: async fetch() -> list[Record]; name; interval
      nest.py
      wunderground.py
      open_meteo.py
      centriconnect.py
    processing/
      zone_calls.py
      degree_days.py
      propane.py             # usage, delivery detection, regression
      summaries.py
    web/
      app.py
      routes/ overview.py, zones.py, runtime.py, propane.py, compare.py, weather.py, events.py, status.py, api.py
      templates/
      static/
    scheduler.py
  tests/
    fixtures/                # recorded JSON responses (secrets scrubbed)
    test_degree_days.py
    test_zone_calls.py
    test_propane.py
    test_sources_*.py
```

---

## 9. Phases

Each phase ends with passing tests, a short demo for the owner, and a pause for feedback.

**Phase 1: Foundation and data collection**
- Project skeleton, config, database models and migrations, logging, `source_status`.
- Nest client with the OAuth CLI flow, token refresh, 2-minute polling, Celsius storage.
- Weather Underground client for KWACLEEL38, including a history backfill to the earliest invoice date, with Open-Meteo used only to fill gaps.
- CentriConnect client via `aiocentriconnect`, storing only changed readings.
- A Status page listing the latest raw readings.
- **Goal:** start collecting data as soon as possible, because Nest has no history.

**Phase 2: Processing**
- Zone call derivation, HDD, propane usage and delivery detection, daily summaries.
- Nightly rebuild job plus `heatmon rebuild-summaries`.
- Unit tests with synthetic data, covering:
  - a call spanning midnight;
  - a missed sample gap;
  - a tank refill;
  - flat tank readings for 2 days;
  - a DST transition (2026-11-01).

**Phase 3: UI**
- Overview, Zones, Runtime, Propane and Weather pages with charts and tables.
- CSV export, the shared date-range picker, and event markers on charts.

**Phase 4: Analysis and inputs**
- Events and settings forms, with the seed data loaded.
- Delivery CSV import.
- Compare Periods page with regression and plain-language summary.

**Phase 5: Optional enhancements**
- Nest Pub/Sub events using a self-hosted topic, for exact HVAC transitions.
- A CT-clamp boiler power source for true burner runtime.
- Alerts (tank low, a source stale for more than 6 hours, a room more than 2°F below setpoint for 2 hours) by email or push.
- A Home Assistant source adapter: if the owner runs Home Assistant, read Nest, weather and tank entities from its REST API instead of calling each service directly.

---

## 10. Acceptance criteria

- The collector runs unattended for 7 days with no gaps longer than 10 minutes in thermostat data, apart from upstream outages, which appear on the Status page.
- Zone call durations derived from 2-minute polling are within ±2 minutes of the Nest app's energy history for a sample day.
- Daily HDD matches a manual calculation from the stored hourly temperatures.
- A simulated 40% tank-level jump is recorded as a delivery and excluded from usage.
- The Compare page shows gallons/HDD and call minutes/HDD for "before/after 2026-10-25" (or any two ranges) and warns when data is insufficient.
- All secrets come from `.env`, and none appear in logs, the database `raw_json`, or test fixtures.

---

## 11. Open questions for the owner

Resolved: KWACLEEL38 is the owner's station and the primary weather source; propane invoices are available for import.

1. Get the free WU API key, and fill in `deliveries_template.csv` from the invoices.
2. What is the tank size?
3. What machine will run the app, and do you already run Home Assistant?
4. Which zone does each Nest control? Are all five zones on Nests?
5. Besides the boiler (heating and hot water), which appliances use propane: range, ovens, fireplace, dryer? These go into the base load (`a` in the regression).
