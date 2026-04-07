"""Battery depletion forecasting based on current trends and weather predictions."""

import os
import math
from datetime import datetime, timedelta
from dataclasses import dataclass


@dataclass
class BatteryForecast:
    current_soc: float  # Current battery % (0-100)
    drain_rate_w: float  # Net drain rate in watts (positive = draining)
    hours_until_empty: float  # Estimated hours until battery hits safety cutoff
    hours_until_sunrise: float  # Hours until next sunrise
    hours_until_usable_solar: float  # Hours until PV output is meaningful
    will_deplete: bool  # True if battery will run out before usable solar
    estimated_soc_at_sunrise: float  # Projected SOC at sunrise
    estimated_soc_at_usable: float  # Projected SOC when solar becomes usable
    risk_level: str  # "ok", "watch", "warning", "critical"


@dataclass
class OvernightForecast:
    """Evening prediction for the full overnight period through next morning."""

    generated_at: str  # When this forecast was made
    soc_at_sunset: float  # SOC when sun went down
    avg_overnight_load_w: float  # Expected overnight drain (from history)
    tomorrow_cloud_pct: float  # Tomorrow's forecast cloud cover
    tomorrow_usable_solar_hour: float  # When solar will be usable tomorrow
    hours_on_battery: float  # Total hours running on battery only
    energy_needed_wh: float  # Total Wh needed to survive the night
    energy_available_wh: float  # Usable Wh in battery (above 20%)
    surplus_deficit_wh: float  # Positive = surplus, negative = deficit
    estimated_soc_at_10am: float  # Projected SOC at 10am
    estimated_empty_time: str  # "03:47" or "N/A" if won't empty
    will_survive: bool  # Will battery last until usable solar?
    risk_level: str  # "ok", "watch", "warning", "critical"
    action_needed: str  # Human-readable recommendation


LATITUDE = float(os.environ.get("LATITUDE", "0"))
LONGITUDE = float(os.environ.get("LONGITUDE", "0"))

# Battery config — inverter safety cuts off at 20% SOC
BATTERY_SAFETY_CUTOFF = float(os.environ.get("BATTERY_SAFETY_CUTOFF", "20"))
BATTERY_CAPACITY_WH = float(os.environ.get("BATTERY_CAPACITY_WH", "15000"))
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", "-6"))  # UTC offset

# Usable solar timing
CLOUDY_USABLE_SOLAR_HOUR = 10.0  # 10am on rainy/overcast days
CLEAR_USABLE_SOLAR_HOUR = 8.0  # 8am on clear days


def estimate_sunrise_sunset(lat: float, date: datetime) -> tuple[datetime, datetime]:
    """Sunrise/sunset estimate. Near equator: sunrise ~5:30-6:00, sunset ~17:30-18:00."""
    day_of_year = date.timetuple().tm_yday
    declination = -23.45 * math.cos(math.radians(360 / 365 * (day_of_year + 10)))
    hour_angle = math.degrees(
        math.acos(-math.tan(math.radians(lat)) * math.tan(math.radians(declination)))
    )

    sunrise_hour = 12 - hour_angle / 15
    sunset_hour = 12 + hour_angle / 15

    tz_offset = TZ_OFFSET
    lng_correction = (LONGITUDE - (tz_offset * 15)) / 15

    sunrise_hour = (sunrise_hour - lng_correction + tz_offset + 12) % 24
    sunset_hour = (sunset_hour - lng_correction + tz_offset + 12) % 24

    sunrise = date.replace(
        hour=int(sunrise_hour),
        minute=int((sunrise_hour % 1) * 60),
        second=0,
        microsecond=0,
    )
    sunset = date.replace(
        hour=int(sunset_hour),
        minute=int((sunset_hour % 1) * 60),
        second=0,
        microsecond=0,
    )
    return sunrise, sunset


def estimate_usable_solar_hour(cloud_cover: float = 50.0) -> float:
    """When solar becomes usable. Rainy = 10am, clear = 8am, interpolated."""
    t = min(cloud_cover, 100.0) / 100.0
    return CLEAR_USABLE_SOLAR_HOUR + t * (
        CLOUDY_USABLE_SOLAR_HOUR - CLEAR_USABLE_SOLAR_HOUR
    )


def forecast_battery(
    soc: float,
    load_power_w: float,
    pv_power_w: float,
    battery_power_w: float,
    is_charging: bool,
    now: datetime | None = None,
    battery_capacity_wh: float = BATTERY_CAPACITY_WH,
    cloud_cover: float = 50.0,
) -> BatteryForecast:
    """Real-time forecast: will battery last until solar becomes usable?"""
    if now is None:
        now = datetime.now()

    tomorrow = now + timedelta(days=1)
    sunrise_today, sunset_today = estimate_sunrise_sunset(LATITUDE, now)
    sunrise_tomorrow, _ = estimate_sunrise_sunset(LATITUDE, tomorrow)

    next_sunrise = sunrise_today if now < sunrise_today else sunrise_tomorrow
    hours_until_sunrise = max(0, (next_sunrise - now).total_seconds() / 3600)

    usable_hour = estimate_usable_solar_hour(cloud_cover)
    next_usable = next_sunrise.replace(
        hour=int(usable_hour), minute=int((usable_hour % 1) * 60)
    )

    if now.hour >= usable_hour and now < sunset_today:
        hours_until_usable = 0
    else:
        hours_until_usable = max(0, (next_usable - now).total_seconds() / 3600)

    # Net drain rate
    is_after_sunset = now > sunset_today
    is_before_sunrise = now < sunrise_today
    is_nighttime = is_after_sunset or is_before_sunrise

    if is_nighttime:
        net_drain_w = load_power_w
    elif pv_power_w < load_power_w and not is_charging:
        net_drain_w = load_power_w - pv_power_w
    elif is_charging:
        net_drain_w = 0
    else:
        net_drain_w = load_power_w  # Use load as overnight estimate

    usable_soc = max(0, soc - BATTERY_SAFETY_CUTOFF)
    usable_wh = (usable_soc / 100.0) * battery_capacity_wh

    hours_until_empty = usable_wh / net_drain_w if net_drain_w > 0 else float("inf")

    soc_drop_sunrise = (net_drain_w * hours_until_sunrise / battery_capacity_wh) * 100
    estimated_soc_at_sunrise = soc - soc_drop_sunrise

    soc_drop_usable = (net_drain_w * hours_until_usable / battery_capacity_wh) * 100
    estimated_soc_at_usable = soc - soc_drop_usable

    will_deplete = estimated_soc_at_usable < BATTERY_SAFETY_CUTOFF

    if estimated_soc_at_usable >= 50:
        risk_level = "ok"
    elif estimated_soc_at_usable >= 30:
        risk_level = "watch"
    elif estimated_soc_at_usable >= BATTERY_SAFETY_CUTOFF:
        risk_level = "warning"
    else:
        risk_level = "critical"

    return BatteryForecast(
        current_soc=soc,
        drain_rate_w=net_drain_w,
        hours_until_empty=hours_until_empty,
        hours_until_sunrise=hours_until_sunrise,
        hours_until_usable_solar=hours_until_usable,
        will_deplete=will_deplete,
        estimated_soc_at_sunrise=max(0, estimated_soc_at_sunrise),
        estimated_soc_at_usable=max(0, estimated_soc_at_usable),
        risk_level=risk_level,
    )


def forecast_overnight(
    current_soc: float,
    avg_overnight_load_w: float,
    tomorrow_cloud_pct: float,
    now: datetime | None = None,
    battery_capacity_wh: float = BATTERY_CAPACITY_WH,
) -> OvernightForecast:
    """Evening prediction: will we make it through tonight?

    This is the KEY forecast. Run it starting around 4-5pm when you can see
    what SOC you'll have at sunset, and you know tomorrow's weather.

    The question: from sunset tonight to usable-solar tomorrow morning,
    do we have enough battery to survive?

    Timeline:
      sunset (~6pm) -> midnight -> sunrise (~6am) -> usable solar (8-10am)
      |<------------ battery only, no solar input ------------>|
    """
    if now is None:
        now = datetime.now()

    tomorrow = now + timedelta(days=1)
    _, sunset_today = estimate_sunrise_sunset(LATITUDE, now)
    sunrise_tomorrow, _ = estimate_sunrise_sunset(LATITUDE, tomorrow)

    # When will solar actually produce enough to matter?
    usable_hour = estimate_usable_solar_hour(tomorrow_cloud_pct)
    usable_solar_time = tomorrow.replace(
        hour=int(usable_hour),
        minute=int((usable_hour % 1) * 60),
        second=0,
        microsecond=0,
    )

    # Hours on battery = sunset to usable solar
    if now > sunset_today:
        # Already past sunset, count from now
        hours_on_battery = (usable_solar_time - now).total_seconds() / 3600
    else:
        # Still daylight, count from sunset
        hours_on_battery = (usable_solar_time - sunset_today).total_seconds() / 3600

    hours_on_battery = max(0, hours_on_battery)

    # Energy math
    energy_needed_wh = avg_overnight_load_w * hours_on_battery
    energy_available_wh = (
        max(0, (current_soc - BATTERY_SAFETY_CUTOFF) / 100.0) * battery_capacity_wh
    )
    surplus_deficit_wh = energy_available_wh - energy_needed_wh

    # Projected SOC at 10am (worst case)
    soc_drop = (energy_needed_wh / battery_capacity_wh) * 100
    estimated_soc_at_10am = current_soc - soc_drop

    # When will battery hit cutoff?
    if avg_overnight_load_w > 0 and energy_available_wh < energy_needed_wh:
        hours_to_empty = energy_available_wh / avg_overnight_load_w
        if now > sunset_today:
            empty_time = now + timedelta(hours=hours_to_empty)
        else:
            empty_time = sunset_today + timedelta(hours=hours_to_empty)
        estimated_empty_time = empty_time.strftime("%H:%M")
    else:
        estimated_empty_time = "N/A"

    will_survive = estimated_soc_at_10am >= BATTERY_SAFETY_CUTOFF

    # Risk level with wider bands for advance warning
    if estimated_soc_at_10am >= 50:
        risk_level = "ok"
    elif estimated_soc_at_10am >= 35:
        risk_level = "watch"
    elif estimated_soc_at_10am >= BATTERY_SAFETY_CUTOFF:
        risk_level = "warning"
    else:
        risk_level = "critical"

    # Actionable recommendation
    if risk_level == "ok":
        action_needed = "All good. Battery will comfortably last through the night."
    elif risk_level == "watch":
        action_needed = (
            (
                f"Marginal. Consider reducing overnight load. "
                f"Current load: {avg_overnight_load_w:.0f}W. "
                f"You'd need to drop to {energy_available_wh / hours_on_battery:.0f}W to be safe."
            )
            if hours_on_battery > 0
            else "Monitoring."
        )
    elif risk_level == "warning":
        safe_load = (
            energy_available_wh / hours_on_battery if hours_on_battery > 0 else 0
        )
        action_needed = (
            f"WARNING: Battery likely runs out at {estimated_empty_time}. "
            f"Reduce load to {safe_load:.0f}W or below NOW. "
            f"Turn off non-essentials."
        )
    else:
        action_needed = (
            f"CRITICAL: Battery WILL run out at {estimated_empty_time}. "
            f"Deficit: {abs(surplus_deficit_wh):.0f}Wh. "
            f"Shut down everything non-essential immediately. "
            f"No power until ~{int(usable_hour)}:00 tomorrow."
        )

    return OvernightForecast(
        generated_at=now.isoformat(),
        soc_at_sunset=current_soc,
        avg_overnight_load_w=avg_overnight_load_w,
        tomorrow_cloud_pct=tomorrow_cloud_pct,
        tomorrow_usable_solar_hour=usable_hour,
        hours_on_battery=hours_on_battery,
        energy_needed_wh=energy_needed_wh,
        energy_available_wh=energy_available_wh,
        surplus_deficit_wh=surplus_deficit_wh,
        estimated_soc_at_10am=max(0, estimated_soc_at_10am),
        estimated_empty_time=estimated_empty_time,
        will_survive=will_survive,
        risk_level=risk_level,
        action_needed=action_needed,
    )
