#!/usr/bin/env python3
"""Solar battery monitor — alerts when battery is trending towards depletion."""

import os
import sys
import time
import json
import logging
from datetime import datetime
from dataclasses import asdict
from dotenv import load_dotenv
from pysolark import SolArkClient

from solar_monitor.forecast import forecast_battery, forecast_overnight
from solar_monitor.alerts import check_and_alert, check_overnight_alert
from solar_monitor.database import (
    init_db,
    store_reading,
    store_weather,
    store_forecast,
    get_average_nighttime_load,
)
from solar_monitor.weather import get_current_weather, get_tomorrow_cloud_forecast

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Config
USERNAME = os.getenv("SOLARK_USERNAME")
PASSWORD = os.getenv("SOLARK_PASSWORD")
PLANT_ID = int(os.getenv("SOLARK_PLANT_ID", "0"))
WHATSAPP_PHONE = os.getenv("WHATSAPP_PHONE")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # 5 minutes
WEATHER_INTERVAL = 1800  # 30 min

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WIDGET_DATA_PATH = os.path.join(_PROJECT_ROOT, "widget_data.json")

_last_weather_check = 0
_last_weather: dict | None = None
_last_overnight_forecast_date: str | None = (
    None  # Track which day we've run overnight for
)


def fetch_weather() -> dict | None:
    """Fetch and store weather, throttled to WEATHER_INTERVAL."""
    global _last_weather_check, _last_weather
    now = time.time()
    if now - _last_weather_check < WEATHER_INTERVAL and _last_weather:
        return _last_weather

    weather = get_current_weather()
    if weather:
        store_weather(**weather)
        _last_weather = weather
        _last_weather_check = now
        logger.info(
            f"Weather: {weather['description']}, {weather['temp']}°C, "
            f"clouds {weather['clouds']}% | "
            f"Sunrise {weather['sunrise'][11:16]} Sunset {weather['sunset'][11:16]}"
        )
    return weather


def write_widget_data(
    soc,
    pv_power,
    load_power,
    battery_power,
    is_charging,
    forecast,
    weather,
    overnight=None,
):
    """Write current state to JSON for the macOS widget and web dashboard."""
    data = {
        "updated": datetime.now().isoformat(),
        "soc": soc,
        "pv_power": pv_power,
        "load_power": load_power,
        "battery_power": battery_power,
        "is_charging": is_charging,
        "forecast": {
            "soc_at_sunrise": forecast.estimated_soc_at_sunrise,
            "soc_at_usable": forecast.estimated_soc_at_usable,
            "hours_until_sunrise": forecast.hours_until_sunrise,
            "hours_until_usable_solar": forecast.hours_until_usable_solar,
            "hours_until_empty": min(forecast.hours_until_empty, 999),
            "will_deplete": forecast.will_deplete,
            "drain_rate_w": forecast.drain_rate_w,
            "risk_level": forecast.risk_level,
        },
        "weather": weather,
    }
    if overnight:
        data["overnight"] = asdict(overnight)
    with open(WIDGET_DATA_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)


def run_overnight_forecast(soc: float, weather: dict | None) -> object | None:
    """Run the evening/overnight forecast using tomorrow's weather.

    This is the critical prediction. Starting at 4pm, we forecast:
    - How much battery we'll drain overnight (sunset -> usable solar)
    - Whether we'll hit 20% cutoff before solar kicks in
    - What load reduction is needed if at risk

    Uses historical avg nighttime load from SQLite for better accuracy.
    """
    global _last_overnight_forecast_date

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    # Run overnight forecast from 4pm onwards, refresh every check cycle
    if now.hour < 16:
        return None

    # Get tomorrow's cloud forecast
    tomorrow_clouds = get_tomorrow_cloud_forecast()

    # Get historical overnight load (last 7 days, 6pm-6am)
    avg_night_load = get_average_nighttime_load()
    if avg_night_load <= 0:
        avg_night_load = 500.0  # Sensible default until we have history
        logger.info(f"No overnight load history yet, using default: {avg_night_load}W")

    overnight = forecast_overnight(
        current_soc=soc,
        avg_overnight_load_w=avg_night_load,
        tomorrow_cloud_pct=tomorrow_clouds,
        now=now,
    )

    if _last_overnight_forecast_date != today:
        # First overnight forecast of the day — log it prominently
        logger.info("=" * 60)
        logger.info("OVERNIGHT FORECAST")
        logger.info(f"  SOC now:            {soc:.0f}%")
        logger.info(f"  Avg night load:     {avg_night_load:.0f}W")
        logger.info(f"  Tomorrow clouds:    {tomorrow_clouds:.0f}%")
        logger.info(
            f"  Usable solar at:    {overnight.tomorrow_usable_solar_hour:.0f}:00"
        )
        logger.info(f"  Hours on battery:   {overnight.hours_on_battery:.1f}h")
        logger.info(f"  Energy needed:      {overnight.energy_needed_wh:.0f}Wh")
        logger.info(f"  Energy available:   {overnight.energy_available_wh:.0f}Wh")
        logger.info(f"  Surplus/deficit:    {overnight.surplus_deficit_wh:+.0f}Wh")
        logger.info(f"  SOC at 10am:        {overnight.estimated_soc_at_10am:.0f}%")
        logger.info(f"  Empty at:           {overnight.estimated_empty_time}")
        logger.info(f"  Risk:               {overnight.risk_level.upper()}")
        logger.info(f"  >> {overnight.action_needed}")
        logger.info("=" * 60)
        _last_overnight_forecast_date = today
    else:
        logger.info(
            f"Overnight: SOC@10am ~{overnight.estimated_soc_at_10am:.0f}% | "
            f"Risk: {overnight.risk_level.upper()} | "
            f"Empty at: {overnight.estimated_empty_time}"
        )

    return overnight


def check_battery(client: SolArkClient) -> None:
    """Fetch current data, store it, forecast, and alert."""
    try:
        flow = client.get_plant_energy_flow(PLANT_ID)
        realtime = client.get_plant_realtime(PLANT_ID)

        soc = flow.soc
        pv_power = flow.pv_power
        load_power = flow.load_power
        battery_power = flow.battery_power
        grid_power = flow.grid_power
        is_charging = flow.to_battery

        store_reading(
            soc=soc,
            pv_power=pv_power,
            load_power=load_power,
            battery_power=battery_power,
            grid_power=grid_power,
            is_charging=is_charging,
            etoday=realtime.etoday,
            emonth=realtime.emonth,
            eyear=realtime.eyear,
            etotal=realtime.etotal,
        )

        logger.info(
            f"SOC: {soc}% | PV: {pv_power}W | Load: {load_power}W | "
            f"Batt: {battery_power}W ({'↑chrg' if is_charging else '↓drain'}) | "
            f"Today: {realtime.etoday}kWh"
        )

        # Use historical nighttime load for better prediction
        avg_night_load = get_average_nighttime_load()
        effective_load = avg_night_load if avg_night_load > 0 else load_power

        # Current weather + cloud cover
        weather = fetch_weather()
        cloud_cover = weather["clouds"] if weather else 50.0

        # Real-time forecast
        fc = forecast_battery(
            soc=soc,
            load_power_w=effective_load,
            pv_power_w=pv_power,
            battery_power_w=battery_power,
            is_charging=is_charging,
            now=datetime.now(),
            cloud_cover=cloud_cover,
        )
        store_forecast(fc, cloud_cover=cloud_cover)

        logger.info(
            f"Realtime: SOC@usable ~{fc.estimated_soc_at_usable:.0f}% | "
            f"Empty in {fc.hours_until_empty:.1f}h | "
            f"Risk: {fc.risk_level.upper()}"
        )

        # Overnight forecast (runs from 4pm onward)
        overnight = run_overnight_forecast(soc, weather)

        # Write widget data
        write_widget_data(
            soc,
            pv_power,
            load_power,
            battery_power,
            is_charging,
            fc,
            weather,
            overnight,
        )

        # Send alerts (checks both realtime and overnight forecasts)
        check_and_alert(fc, whatsapp_phone=WHATSAPP_PHONE)
        if overnight:
            check_overnight_alert(overnight, whatsapp_phone=WHATSAPP_PHONE)

    except Exception as e:
        logger.error(f"Error checking battery: {e}", exc_info=True)


def main():
    if not USERNAME or not PASSWORD:
        print("Set SOLARK_USERNAME and SOLARK_PASSWORD in .env")
        sys.exit(1)

    init_db()

    logger.info(f"Solar Monitor v0.1.0 — Plant {PLANT_ID}")
    logger.info(f"Check interval: {CHECK_INTERVAL}s")
    logger.info(f"WhatsApp: {'enabled' if WHATSAPP_PHONE else 'disabled'}")

    client = SolArkClient(username=USERNAME, password=PASSWORD)
    client.login()
    logger.info("Connected to SolArk API")

    while True:
        check_battery(client)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
