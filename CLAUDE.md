# Solar Monitor

## Project Overview
Monitor Sol-Ark solar/battery system via the SolArk Cloud API. Sends alerts (macOS + WhatsApp) when battery is trending towards depletion before usable solar arrives. Built for off-grid living.

## Key context
- Inverter safety cuts off at 20% SOC — this is the critical threshold
- On rainy/cloudy days, usable solar doesn't arrive until 9-10am (not sunrise at ~6am)
- Typical failure mode: battery runs out 4-7am, no power until ~10am
- All personal config (credentials, location, plant ID) in `.env`

## Setup
- Python 3.11+ with venv at `./venv`
- Credentials in `.env` (see `.env.example`)
- API: https://api.solarkcloud.com (via PySolark library + direct)
- Auth: OAuth2 password grant, client_id=csp-web
- Weather: OpenWeatherMap API
- DB: SQLite at `solar_monitor.db`

## Architecture
```
src/solar_monitor/
  monitor.py    — main loop (checks every CHECK_INTERVAL seconds)
  forecast.py   — battery depletion prediction, sunrise calc, risk levels
  alerts.py     — macOS notifications (osascript) + WhatsApp (wa.me)
  database.py   — SQLite: readings, weather, forecasts, alerts tables
  weather.py    — OpenWeatherMap current + forecast
  web.py        — HTTP server for dashboard API
  backfill.py   — Historical data import from SolArk API
  analyze.py    — Outage analysis and capacity planning
web/index.html  — Vue 3 + Chart.js dashboard (single file)
widget/         — macOS menu bar widget (Swift)
```

## Commands
- Run monitor: `source venv/bin/activate && python -m solar_monitor`
- Run web: `python -m solar_monitor.web` (port 8077)
- Run widget: `cd widget && swift SolarWidget.swift`
- Backfill: `python -m solar_monitor.backfill`
- Analyze: `python -m solar_monitor.analyze`
- Install: `pip install -e .`
- Service: `launchctl load ~/Library/LaunchAgents/com.solar-monitor.plist`
- Logs: `tail -f logs/monitor.log`
- Lint: `ruff check src/`

## Key API Endpoints (SolArk Cloud)
- `POST /oauth/token` — login (username, password, grant_type=password, client_id=csp-web)
- `GET /api/v1/plant/energy/{id}/flow` — SOC, power flows, directions
- `GET /api/v1/plant/{id}/realtime` — today/month/year production stats
- `GET /api/v1/plant/energy/{id}/day?date=YYYY-MM-DD` — 5-min interval time series
- `GET /api/v1/plant/energy/{id}/month?date=YYYY-MM` — daily totals per month
- `GET /api/v1/plant/energy/{id}/generation/use` — daily totals (pv, load, battery, grid)

## Risk levels
- **ok**: SOC at usable solar >= 50%
- **watch**: 30-50%
- **warning**: 20-30% (alerts sent)
- **critical**: <20% (alerts sent, will hit safety cutoff)
