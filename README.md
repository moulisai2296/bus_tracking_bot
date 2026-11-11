# APSRTC Bus Alerts

This project provides a local FastAPI service for APSRTC station and bus lookups, plus two Telegram bots that manage route watches. The morning and evening profiles share one API process and one SQLite database. Postman and the bots call the same API.

The default routes are:

- Morning: Puttur to Pichator
- Evening: Pichator to Puttur

The API listens on `127.0.0.1` only. It is intended for local use and is not configured for public or LAN access.

## 1. Requirements

- Windows with PowerShell
- Python 3.11 or newer
- `uv`
- Two Telegram bots if you want both morning and evening alerts

Install `uv` from the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/). In PowerShell, the official standalone installer command is:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Open a new PowerShell terminal after installation. If Python 3.11 is not already available, install it with:

```powershell
uv python install 3.11
```

## 2. Create the virtual environment and install packages

Open PowerShell in the project folder (the folder containing `pyproject.toml`), then run:

```powershell
uv venv
uv sync --all-groups
```

This creates the project's `.venv` folder and installs the locked dependencies from `uv.lock`. Use `uv run` for the commands below; it runs them with this project environment.

## 3. Configure the app

Create `.env` from the sample file once:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Create two separate bots with [@BotFather](https://t.me/BotFather), then paste their tokens into `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=your_morning_bot_token
TELEGRAM_EVENING_BOT_TOKEN=your_evening_bot_token
```

Keep `.env` private. Do not share or commit it. Leave the BUSRTC_* settings from `.env.example` as supplied; you do not need to obtain separate APSRTC API keys for this setup.

The sample configuration uses these defaults:

```dotenv
ALERTS_API_URL=http://127.0.0.1:8000
MORNING_FROM_STATION=Puttur
MORNING_TO_STATION=Pichator
EVENING_FROM_STATION=Pichator
EVENING_TO_STATION=Puttur
POLL_SECONDS=60
MORNING_ALERT_START=08:00
MORNING_ALERT_END=11:00
EVENING_ALERT_START=15:00
EVENING_ALERT_END=19:00
```

Times use 24-hour format and are interpreted in IST. `POLL_SECONDS` controls how often the monitor checks for work; reports are deduplicated and sent on 15-minute boundaries during each profile's window.

## 4. Start the API and both bots

Keep three PowerShell terminals open in the project folder. Start the API first.

**Terminal 1 - API and alert monitor**

```powershell
uv run uvicorn apsrtc_alerts.api:app --host 127.0.0.1 --port 8000
```

Check that it started by opening [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health). Interactive API documentation is at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

**Terminal 2 - Morning Telegram bot**

```powershell
$env:BOT_PROFILE = "morning"
uv run python -m apsrtc_alerts.bot
```

**Terminal 3 - Evening Telegram bot**

```powershell
$env:BOT_PROFILE = "evening"
uv run python -m apsrtc_alerts.bot
```

Leave all three terminals running. The API owns APSRTC lookups, watch storage, and alert monitoring. Each bot polls Telegram and sends station/watch commands to the API.

## 5. Activate Telegram route watches

Open a chat with each bot and send `/start` once. The morning bot activates Puttur to Pichator; the evening bot activates Pichator to Puttur. The API saves each watch in SQLite. Sending `/start` again is not required unless the watch was removed.

Useful bot commands:

- `/start` - activate the profile's default route
- `/chatid` - show the Telegram chat ID for Postman
- `/route` - show the current route
- `/watch Puttur | Pichator` - set a route by station name
- `/watchid 12321 | 11791` - set a route by APSRTC place IDs
- `/places Pichator` - search APSRTC station names
- `/stop` - remove the current profile's watch
- `/help` - show command help

A watch is associated with the bot profile and chat ID. If you use different Telegram accounts, start a conversation with the bot from each account and create a watch for each chat ID.

## 6. Use Postman

Import [`postman/APSRTC Bus Alerts.postman_collection.json`](postman/APSRTC%20Bus%20Alerts.postman_collection.json) into Postman. The collection's `baseUrl` is already `http://127.0.0.1:8000` and its default station IDs are Puttur (`12321`) and Pichator (`11791`).

Recommended order:

1. Call **Health** and confirm the response has `"status": "ok"`.
2. Call the station searches to inspect APSRTC place IDs.
3. Call **Bus lookup** with `from_station_id` and `to_station_id`.
4. Copy a current bus's `serviceDocId` into the `todayServiceId` collection variable, then call **Today API**. `serviceDocId` changes by service date; do not rely on yesterday's example value.
5. Send `/chatid` to the Telegram bot. Put the returned ID in the collection's `chatId` variable.
6. Use **Create or replace morning watch** or **Create or replace evening watch** to create or replace a watch from Postman. The bot and Postman update the same watch record. The collection's `chatId` variable is blank until you set it.

To create a morning watch directly, use `POST /watches` with:

```json
{
  "bot_profile": "morning",
  "chat_id": "YOUR_TELEGRAM_CHAT_ID",
  "from_station_id": "12321",
  "to_station_id": "11791"
}
```

For evening, set `bot_profile` to `evening`, `from_station_id` to `11791`, and `to_station_id` to `12321`.

## API reference

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | API status and whether each Telegram token is configured |
| `GET` | `/stations?q=Puttur` | Search APSRTC stations |
| `GET` | `/buses?from_station_id=12321&to_station_id=11791` | List APSRTC services for a route |
| `GET` | `/buses/{service_id}/stops` | Read the separate waypoint history endpoint |
| `GET` | `/buses/{service_id}/today-stops` | Read today's `/servicewaypointdetails/bydocid` snapshot only |
| `POST` | `/watches` | Create or replace a profile-specific watch |
| `GET` | `/watches/{chat_id}?bot_profile=morning` | Read a watch |
| `DELETE` | `/watches/{chat_id}?bot_profile=evening` | Remove a watch |

The API returns APSRTC values in their original form. In particular, non-null `ETA` and `vtsDepartureTime` values are timestamps; `scheduleArrTime` is a scheduled time string.

## Alert behavior and configuration

- The API checks both active profiles; separate API instances are not needed.
- The morning profile reports Puttur to Pichator during `MORNING_ALERT_START`-`MORNING_ALERT_END` (default 08:00-11:00).
- The evening profile reports Pichator to Puttur during `EVENING_ALERT_START`-`EVENING_ALERT_END` (default 15:00-19:00).
- Each report lists buses with a boarding-stop ETA strictly after the report time and through the configured end time. For example, the 15:15 report can include arrivals after 15:15 through 19:00.
- If the boarding stop has no ETA, that bus is skipped for this report and checked again during the next 15-minute cycle. `scheduleArrTime` is not used as an alert fallback.
- The monitor uses today's waypoint snapshot only. Historical stop data is not used for alerts, and services whose earliest recorded `vtsDepartureTime` is from a previous date are skipped.
- APSRTC ETAs are estimates. Some services do not provide live tracking.

Change the routes, report windows, `POLL_SECONDS`, database path, or log folder in `.env`, then restart the API for changes to take effect. Successful bot messages are recorded in `logs/morning_log.json` and `logs/eveng_log.json`. Watches and alert deduplication state are stored in `bus_alerts.sqlite3` by default.

## Troubleshooting

- **`/health` says a profile is disabled:** check that the corresponding token is set in `.env`, then restart the API and that bot terminal.
- **The bot does not respond:** check that its terminal is running, `BOT_PROFILE` is correct for that terminal, and the token belongs to that bot.
- **The bot responds but no bus report arrives:** verify `/start` was sent to that bot, the route is active, the current time is inside the configured report window, and APSRTC has a boarding-stop ETA. Schedule-only rows are intentionally omitted.
- **Postman returns an APSRTC `502`:** check that the computer has network/DNS access to APSRTC and retry the request.
- **A bus appears in Today API but not in the alert:** alerts require a non-null ETA at the watched boarding stop and a service with a current journey date.
