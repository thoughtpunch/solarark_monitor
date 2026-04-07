# SolArk Monitor

Battery depletion forecasting and alerting for Sol-Ark solar systems. Built for off-grid living.

## What it does

- Polls your Sol-Ark system every 5 minutes via the SolArk Cloud API
- Predicts whether your battery will last until solar becomes usable the next morning
- Sends macOS notifications and WhatsApp alerts when battery is trending towards depletion
- Stores all readings in SQLite for historical analysis
- Uses weather forecasts to adjust predictions (cloudy days = later usable solar)
- Web dashboard with real-time stats, charts, and hourly usage breakdown
- macOS menu bar widget
- Historical data backfill and outage analysis

## The problem it solves

On rainy days, the inverter safety cuts off at 20% SOC. Solar doesn't produce enough to recover above 20% until 9-10am on cloudy days. You lose power from ~4-7am until ~10am. This monitor warns you **the evening before** so you can reduce consumption or prepare.

## Quick start

```bash
git clone https://github.com/thoughtpunch/solarark_monitor.git
cd solarark_monitor
python3 -m venv venv
source venv/bin/activate
pip install -e .

# Configure — fill in your MySolArk credentials and location
cp .env.example .env
# Edit .env

# Run
python -m solar_monitor          # Monitor (checks every 5 min)
python -m solar_monitor.web      # Web dashboard at http://localhost:8077
```

## Configuration (.env)

```
SOLARK_USERNAME=your_email@example.com
SOLARK_PASSWORD=your_password
SOLARK_PLANT_ID=123456
LATITUDE=9.27
LONGITUDE=-83.79
OPENWEATHER_API_KEY=your_key

# Optional
WHATSAPP_PHONE=+1234567890
CHECK_INTERVAL=300
WEB_PORT=8077
PLANT_CREATED=2025-01-01
```

Get your plant ID from your MySolArk dashboard URL: `https://www.mysolark.com/plants/overview/{PLANT_ID}/2`

Get a free OpenWeatherMap API key at https://openweathermap.org/api (or leave blank to skip weather features).

## Run as a service (macOS launchd)

Edit `com.solar-monitor.plist` to set the correct paths for your system, then:

```bash
cp com.solar-monitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.solar-monitor.plist

# Check status
launchctl list | grep solar

# View logs
tail -f logs/monitor.log

# Stop
launchctl unload ~/Library/LaunchAgents/com.solar-monitor.plist
```

## Menu bar widget (macOS)

```bash
cd widget && swift SolarWidget.swift
```

Shows battery %, charging status, forecast risk level, and weather in the macOS menu bar.

## Web dashboard

```bash
python -m solar_monitor.web
# Open http://localhost:8077
```

Shows:
- Battery SOC, solar production, load, hours remaining
- Forecast risk level (ok / watch / warning / critical)
- 24-hour SOC and power charts
- Average usage by hour of day
- Peak and average load stats
- Weather conditions

## Historical data & analysis

```bash
# Backfill all available history from MySolArk
python -m solar_monitor.backfill

# Analyze outage patterns and generate capacity planning report
python -m solar_monitor.analyze
```

The analyzer finds every historical outage, back-predicts what caused it, and simulates whether adding battery capacity would have prevented it.

## Overnight forecast

Starting at 4pm each day, the monitor runs an overnight forecast:

1. Takes current SOC + historical average overnight load + tomorrow's weather forecast
2. Calculates: "From sunset to usable-solar, do I have enough battery?"
3. Sends alerts with specific recommendations (e.g., "Reduce load to 400W to survive the night")

Risk levels:
- **ok**: Comfortably lasting the night
- **watch**: Marginal — consider reducing load
- **warning**: Battery will likely run out — reduce load now
- **critical**: Battery WILL run out — shut down non-essentials

## Project structure

```
src/solar_monitor/
  __init__.py       # Package init
  __main__.py       # Entry point
  monitor.py        # Main monitoring loop
  forecast.py       # Battery depletion prediction
  alerts.py         # macOS + WhatsApp notifications
  database.py       # SQLite storage and queries
  weather.py        # OpenWeatherMap integration
  web.py            # Web API server
  backfill.py       # Historical data import
  analyze.py        # Outage analysis & capacity planning
web/
  index.html        # Vue.js dashboard (single file)
widget/
  SolarWidget.swift # macOS menu bar widget
```

## License

MIT — see [LICENSE](LICENSE)
