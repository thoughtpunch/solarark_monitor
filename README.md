# SolArk Monitor

Battery depletion forecasting and alerting for off-grid Sol-Ark solar systems.

Monitors your Sol-Ark battery system 24/7, predicts overnight depletion using weather forecasts and historical usage patterns, and sends advance alerts so you can act before losing power.

## Features

- **Real-time monitoring** — polls SolArk Cloud API every 5 minutes
- **Overnight forecast** — starting at 4pm, predicts if battery will survive until solar is usable the next morning
- **Smart alerts** — macOS notifications with escalating severity and independent cooldowns:
  - 🔥 Heavy evening drain (>10%/hr after sunset)
  - ⚠️ Low battery at sunset (<70% at 6pm)
  - 🚨 Low battery at bedtime (<50% at 10pm)
  - 🔧 Battery not charging when solar is available
  - 🚨 Critical SOC (<25%)
  - ☁️ No solar production during daytime
  - 🔮 Overnight depletion forecast (watch → warning → critical)
- **WhatsApp alerts** — optional, for when you're away from your laptop
- **SQLite storage** — every reading stored for historical analysis
- **Weather-aware** — uses OpenWeatherMap forecasts to adjust predictions (cloudy = solar delayed to 10am)
- **Web dashboard** — Vue.js + Chart.js with SOC/power charts, hourly usage, peak stats
- **macOS menu bar widget** — battery %, forecast risk, weather with emojis
- **Historical backfill** — import all available data from MySolArk
- **Outage analysis** — find every past outage, back-predict causes, simulate capacity upgrades
- **Auto-start** — launchd services for monitor, web server, and widget (all singleton-safe)

## The Problem

On cloudy/rainy days, the inverter safety cuts off at 20% SOC. Solar doesn't produce enough to recover until 9-10am. You lose power from ~4-7am until ~10am. This monitor warns you **the evening before** so you can reduce consumption.

## Quick Start

```bash
git clone https://github.com/thoughtpunch/solarark_monitor.git
cd solarark_monitor
python3 -m venv venv
source venv/bin/activate
pip install -e .

cp .env.example .env
# Edit .env with your MySolArk credentials and location

python -m solar_monitor          # Start monitoring
python -m solar_monitor.web      # Web dashboard at http://localhost:8077
cd widget && swift SolarWidget.swift  # Menu bar widget
```

## Configuration (.env)

```
# Required
SOLARK_USERNAME=your_email@example.com
SOLARK_PASSWORD=your_password
SOLARK_PLANT_ID=123456
LATITUDE=9.27
LONGITUDE=-83.79
TZ_OFFSET=-6
OPENWEATHER_API_KEY=your_key

# Battery config
BATTERY_CAPACITY_WH=15000         # Total Wh (e.g. 3x 5kWh = 15000)
BATTERY_SAFETY_CUTOFF=20          # Inverter cutoff %

# Optional
WHATSAPP_PHONE=+1234567890
CHECK_INTERVAL=300                # Seconds between checks
WEB_PORT=8077
PLANT_CREATED=2025-01-01          # For backfill start date
```

Find your plant ID in your MySolArk dashboard URL: `https://www.mysolark.com/plants/overview/{PLANT_ID}/2`

## Auto-Start (macOS launchd)

Edit the plist files to set your paths, then:

```bash
# Install all three services
cp com.solar-monitor.plist ~/Library/LaunchAgents/
# Create web and widget plists similarly (see docs)

# Load
launchctl load ~/Library/LaunchAgents/com.solar-monitor.plist
launchctl load ~/Library/LaunchAgents/com.solar-monitor-web.plist
launchctl load ~/Library/LaunchAgents/com.solar-monitor-widget.plist

# Check status
launchctl list | grep solar

# Logs
tail -f logs/monitor.log
```

All services auto-start on login, auto-restart on crash, and prevent duplicate instances.

## Historical Analysis

```bash
# Backfill all available history from MySolArk
python -m solar_monitor.backfill

# Analyze outage patterns and generate capacity planning report
python -m solar_monitor.analyze
```

The analyzer finds every historical outage, determines what SOC and load conditions led to it, and simulates whether adding battery capacity would have prevented it.

## Web Dashboard

```bash
python -m solar_monitor.web
# http://localhost:8077
```

- Battery SOC, solar production, load, hours remaining
- Forecast risk level (ok / watch / warning / critical)
- 24-hour SOC and power charts
- Average usage by hour of day
- Peak and average load stats
- Weather conditions

## Project Structure

```
src/solar_monitor/
  __init__.py       Package init (loads .env)
  __main__.py       Entry point
  monitor.py        Main monitoring loop
  forecast.py       Battery depletion + overnight prediction
  alerts.py         macOS notifications, WhatsApp, situational alerts
  database.py       SQLite storage and query functions
  weather.py        OpenWeatherMap integration
  web.py            HTTP API server for dashboard
  backfill.py       Historical data import from SolArk API
  analyze.py        Outage analysis & capacity planning
web/
  index.html        Vue 3 + Chart.js dashboard (single file)
widget/
  SolarWidget.swift macOS menu bar widget
```

## API Endpoints (Web Server)

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard |
| `GET /api/current` | Current state (SOC, power, forecast, weather) |
| `GET /api/readings` | Last 24h of readings |
| `GET /api/readings/{hours}` | Last N hours of readings |
| `GET /api/hourly` | Average usage by hour of day |
| `GET /api/peak` | 7-day peak and average stats |
| `GET /api/summary` | Today's summary |
| `GET /api/weather` | 7-day weather history |

## SolArk Cloud API

Uses the same API as the MySolArk web dashboard:

| Endpoint | Description |
|----------|-------------|
| `POST /oauth/token` | Login (password grant, client_id=csp-web) |
| `GET /api/v1/plant/energy/{id}/flow` | SOC, power flows, charge direction |
| `GET /api/v1/plant/{id}/realtime` | Today/month/year production stats |
| `GET /api/v1/plant/energy/{id}/day?date=YYYY-MM-DD` | 5-min interval time series |
| `GET /api/v1/plant/energy/{id}/month?date=YYYY-MM` | Daily totals per month |
| `GET /api/v1/plant/energy/{id}/generation/use` | Daily summary (PV, load, battery, grid) |

## License

MIT — see [LICENSE](LICENSE)
