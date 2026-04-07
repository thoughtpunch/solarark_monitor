"""Weather integration using OpenWeatherMap (same API MySolArk uses)."""

import os
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

# MySolArk uses this exact API key — it's embedded in their frontend JS
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
LATITUDE = float(os.getenv("LATITUDE", "0"))
LONGITUDE = float(os.getenv("LONGITUDE", "0"))


def get_current_weather() -> dict | None:
    """Fetch current weather from OpenWeatherMap."""
    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={
                "lat": LATITUDE,
                "lon": LONGITUDE,
                "appid": OPENWEATHER_API_KEY,
                "units": "metric",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        return {
            "temp": data["main"]["temp"],
            "humidity": data["main"]["humidity"],
            "clouds": data["clouds"]["all"],  # Cloud cover %
            "description": data["weather"][0]["description"],
            "wind_speed": data.get("wind", {}).get("speed", 0),
            "sunrise": datetime.fromtimestamp(data["sys"]["sunrise"]).isoformat(),
            "sunset": datetime.fromtimestamp(data["sys"]["sunset"]).isoformat(),
        }
    except Exception as e:
        logger.error(f"Failed to fetch weather: {e}")
        return None


def get_weather_forecast() -> list[dict] | None:
    """Fetch 5-day/3-hour forecast from OpenWeatherMap."""
    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params={
                "lat": LATITUDE,
                "lon": LONGITUDE,
                "appid": OPENWEATHER_API_KEY,
                "units": "metric",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        forecasts = []
        for item in data["list"]:
            forecasts.append(
                {
                    "timestamp": item["dt_txt"],
                    "temp": item["main"]["temp"],
                    "humidity": item["main"]["humidity"],
                    "clouds": item["clouds"]["all"],
                    "description": item["weather"][0]["description"],
                    "wind_speed": item.get("wind", {}).get("speed", 0),
                }
            )
        return forecasts
    except Exception as e:
        logger.error(f"Failed to fetch weather forecast: {e}")
        return None


def estimate_solar_factor(cloud_cover: float) -> float:
    """Estimate solar production factor based on cloud cover.

    Returns a multiplier (0.0-1.0) for expected solar production.
    Based on empirical data: heavy clouds reduce output by ~75-80%.
    """
    # Linear interpolation with floor
    # 0% clouds = 1.0 factor, 100% clouds = 0.2 factor
    return max(0.2, 1.0 - (cloud_cover / 100.0) * 0.8)


def get_tomorrow_cloud_forecast() -> float:
    """Get average cloud cover for tomorrow's daylight hours (6am-6pm)."""
    forecasts = get_weather_forecast()
    if not forecasts:
        return 50.0  # Default to moderate clouds if we can't get data

    tomorrow = datetime.now().replace(hour=0, minute=0, second=0)
    from datetime import timedelta

    tomorrow = tomorrow + timedelta(days=1)
    tomorrow_end = tomorrow + timedelta(days=1)

    daylight_clouds = []
    for f in forecasts:
        ts = datetime.strptime(f["timestamp"], "%Y-%m-%d %H:%M:%S")
        if tomorrow <= ts < tomorrow_end and 6 <= ts.hour <= 18:
            daylight_clouds.append(f["clouds"])

    if daylight_clouds:
        return sum(daylight_clouds) / len(daylight_clouds)
    return 50.0
