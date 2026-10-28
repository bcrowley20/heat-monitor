# Heating Monitor

This app collects data from the Nest thermostats, the KWACLEEL38 weather station and the CentriConnect propane tank monitor. It stores everything in a SQLite database and serves web pages on the home network. The goal is to measure how boiler setting changes affect propane use and heating runtime, with the weather taken into account.

Phase 1 is done: the app collects data and shows it on a Status page. Charts and analysis come in later phases. See `HEATING_MONITOR_PLAN.md` for the full plan.

## Setup on the Fedora box

Install uv, then get the code and its packages. uv downloads Python 3.14 itself if needed.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone <this repo> ~/heat-monitor
cd ~/heat-monitor
uv sync
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 .env
```

Edit `config.yaml` to set the station's latitude and longitude. Zones are matched to thermostats by the Nest room name, so the four zone names should already match. If one doesn't, the log names the thermostat and you can set `nest_device_id` for that zone.

Put the API keys and passwords in `.env`. The next sections explain where each one comes from. The app collects from every source whose values are filled in and skips the rest, so you can add them one at a time.

## Weather Underground

Station owners get a free key at <https://www.wunderground.com/member/api-keys>. Put it in `HEATMON_WU_API_KEY`.

To load past weather, run a backfill from the date of your oldest propane invoice:

```sh
uv run heatmon backfill --start 2023-10-01
```

It loads the station's own history, then Open-Meteo's hourly data for the same dates, and then rebuilds the daily summaries. The key allows 1,500 calls a day and the backfill makes one call per day of history, so a range longer than about four years stops partway. Run it again the next day. Days already loaded are skipped.

KWACLEEL38's history starts on 2017-04-15 and has one reading every 15 minutes. The station has no solar sensor, so its solar readings are empty.

Open-Meteo is a free weather service that needs no key. The app stores its data for every hour, both from the backfill and from an hourly update while running. Solar figures come from Open-Meteo for every day. Outdoor temperature comes from the station, and Open-Meteo fills in only the hours the station missed. Each daily summary records how many of its hours came from Open-Meteo.

## Nest thermostats

Google charges a one-time US$5 fee for access. Use the same personal Google account that the Nest app uses. Workspace accounts don't work. Google's guide is at <https://developers.google.com/nest/device-access/get-started>, and these are the steps in order.

1. Open the Device Access Console at <https://console.nest.google.com/device-access>, accept the terms and pay the fee.
2. In the Google Cloud Console at <https://console.cloud.google.com>, create a project. Search for "Smart Device Management API" and enable it.
3. Set up the OAuth consent screen. Choose "External" and add your own email as a test user. Then set the publishing status to **In production**. If you leave it in "Testing", Google expires the sign-in after 7 days and collection stops. You'll see an "unverified app" warning when you sign in. That is expected for a personal project.
4. Under Credentials, create an OAuth client ID of type "Web application". Add `https://www.google.com` as an authorized redirect URI. Copy the client ID and secret into `HEATMON_GOOGLE_CLIENT_ID` and `HEATMON_GOOGLE_CLIENT_SECRET`.
5. Back in the Device Access Console, create a project and give it the OAuth client ID. Skip events for now (they are part of Phase 5). Copy the project ID into `HEATMON_SDM_PROJECT_ID`.
6. Run `uv run heatmon auth nest` and follow the prompts. You open a link, sign in and allow access to every thermostat. The browser then lands on google.com, and you paste that page's address back into the terminal.

The app saves the sign-in to `data/nest_token.json`, readable only by your user. You only do this once.

## CentriConnect propane monitor

The device ID and device authentication code are on the setup card that came with the monitor. The user ID is under Account settings in the MyPropane app. They go in the three `HEATMON_CENTRICONNECT_*` values.

## Running it

`uv run heatmon run` collects data and serves the web pages on port 8080. Open `http://<fedora-box>:8080` from any computer on the home network.

For debugging, `heatmon collect` only collects and `heatmon serve` only serves pages.

## Pages

**Overview** shows current conditions, today against yesterday, and propane per degree day by week. **Zones** has one chart per zone: room temperature and setpoint on top, with shading while the zone calls for heat, and outdoor temperature in a panel below. Its table lists every call with its overshoot, which is the highest the room rose above its setpoint in the 6 hours after the call ended. **Runtime** shows minutes calling per day and per zone, and how calling time tracks degree days. **Propane** shows tank readings, fills, weekly usage, and how usage tracks degree days. **Weather** shows temperature, sunshine and wind. **Status** shows each data source's health.

Zones, Runtime, Propane and Weather have a date range picker at the top. The default is the last 14 days. Grey lines with a small marker show events, such as setting changes. Hover over the marker to read the event.

The scatter charts split days into settings periods. Each setting change starts a new period, and changes on the same day count as one. The day of a change belongs to the period before it. Propane is plotted as 7-day averages, because the tank reports in whole percents and single days are too coarse.

**Compare** puts two periods side by side. Pick "before and after an event" to compare the days before a setting change with the days after it; the day of the change is left out of both. Or pick any two date ranges. It shows propane and calling time per degree day for each period, how each period did against the invoice baseline, and a short written summary that ends with how much to trust the result. Days with a fireplace or away event are left out unless you tick the box to include them.

**Events & settings** is where you add, edit and delete events, keep dated snapshots of the boiler's settings (and see what changed between two of them), and import propane invoices.

## Propane invoices and the baseline

Import invoices from a CSV on the Events & settings page, or from the command line:

```sh
uv run heatmon import-deliveries deliveries.csv
```

The columns are in `deliveries_template.csv`. Dates can be `2025-10-30` or `10/30/25`, and values like `$1,467.02` or `79%` are fine. Blank rows are skipped. A file with a bad line is not saved at all, and the error names the line. Rows that look like slips, such as two deliveries with the same gallons and cost, or two less than a week apart, get a warning but still save. Importing the same file again skips rows already saved. When the tank monitor detects a fill within 3 days of an invoiced delivery, only the invoice counts.

The invoices give a baseline from before the monitor existed. Usage between two deliveries is the later delivery's gallons, corrected when the two fills ended at different tank levels. The app then fits gallons used = base load × days + gallons per degree day × degree days across all delivery periods. The base load covers hot water, cooking and the fireplace. The baseline needs weather history covering the invoices, so run the backfill from the first invoice date. The Propane page shows the baseline, totals by heating season, and the usage between each pair of deliveries. Any period with an unusually high daily rate is flagged, because that usually means a wrong date or a copied row.

## Nest history from Google Takeout

The Nest API has no history, but a Google Takeout export of your Nest data does. It has a record for every 15 minutes going back about two years: minutes spent heating, room temperature, setpoint and humidity. Request it at <https://takeout.google.com> (choose only "Nest"), copy the zip to the server, and run:

```sh
uv run heatmon import-nest-takeout ~/takeout-20261025T123003Z-1-001.zip
```

It matches each thermostat to a zone by its Nest room name or label and skips any that don't match, such as a thermostat in another building. It then rebuilds the daily summaries. Running it again, or importing a newer export, only adds records it doesn't already have.

Days before live polling started use these records. Minutes calling per zone are exact. Call counts and "any zone calling" time are estimates, because a record doesn't say when in its 15 minutes the heating ran. So the Compare page leads with zone-minutes per degree day, which is exact for both kinds of data. A history day is only used when at least 90% of its records are there; an export's first and last days are usually partial.

A zone that no longer exists stays in `config.yaml` with the day it was retired, like Master Bath. Its history still counts up to that day.

## Data API

Every table has a "Download CSV" link. The same data is available as JSON at `/api/<name>?start=YYYY-MM-DD&end=YYYY-MM-DD`, or as CSV at `/api/<name>.csv`. The names are `daily`, `thermostat`, `zone_calls`, `weather_hourly`, `tank`, `deliveries`, `weekly_propane`, `events`, `delivery_intervals` and `baseline_seasons`.

## Calculations

Every hour the app rebuilds the last two days of results from the raw readings. That covers calls for heat, propane fills and the daily summaries. To rebuild a longer stretch, for example after changing a setting in `config.yaml`, run:

```sh
uv run heatmon rebuild-summaries --start 2026-10-01
```

Leave out `--start` to rebuild everything.

A zone's call for heat starts when its thermostat first reports HEATING and ends when it stops. Each change is placed halfway between the two readings around it. If readings stop for more than three poll intervals during a call, the call ends at the last reading and is flagged. The daily "any zone calling" time counts overlapping calls once. It shows when the system had demand, which is not the same as burner time.

Heating degree days use a 65°F base, set by `hdd_base_f`. Each hour adds max(0, 65 minus that hour's average temperature) divided by 24. A day needs at least 18 hours of temperatures to get a figure, so today's stays blank until evening.

The tank monitor reports only two or three times a day. Gallons used between two readings are spread across the days in between. Colder days get a bigger share, using the rough estimates under `propane` in `config.yaml`. A day only gets a figure when readings cover all of it. A rise of more than 5% is recorded as a fill, and that day gets no figure because part of its usage is unknown. Smaller rises are sensor noise and count as zero. A 1% change is 10 gallons on this tank, so daily figures are rough. Weekly totals are more reliable.

Fedora's firewall blocks the port until you open it:

```sh
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --reload
```

To keep the app running all the time and restart it after a reboot, install it as a systemd user service. The service file expects the code in `~/heat-monitor` and uv in `~/.local/bin`. Edit `deploy/heatmon.service` if either is somewhere else.

```sh
mkdir -p ~/.config/systemd/user
cp deploy/heatmon.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now heatmon
loginctl enable-linger $USER     # keep running when you're logged out
journalctl --user -u heatmon -f  # watch the log
```

The log is also written to `data/heatmon.log`. Secrets are masked as `***` in the log, in the database and on the Status page.

## Development

Run the tests with `uv run pytest`. They need no network access or credentials.

The app updates its database tables on startup. After changing `heatmon/db/models.py`, create a migration with:

```sh
uv run alembic revision --autogenerate -m "describe the change"
```
