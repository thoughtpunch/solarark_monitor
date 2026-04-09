"""Battery depletion forecasting based on current trends and weather predictions."""

import os
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass

logger = logging.getLogger(__name__)


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


# Cache for real sunrise/sunset from weather API
_sun_cache: dict[str, datetime] = {}


def get_sunrise_sunset(date: datetime) -> tuple[datetime, datetime]:
    """Get sunrise/sunset, preferring real data from OpenWeatherMap.

    Falls back to sensible defaults for near-equator locations if API unavailable.
    """
    date_key = date.strftime("%Y-%m-%d")

    # Return cached if available for this date
    if f"{date_key}_sunrise" in _sun_cache:
        return _sun_cache[f"{date_key}_sunrise"], _sun_cache[f"{date_key}_sunset"]

    # Try to get from weather API
    try:
        from solar_monitor.weather import get_current_weather

        weather = get_current_weather()
        if weather and weather.get("sunrise") and weather.get("sunset"):
            sunrise = datetime.fromisoformat(weather["sunrise"])
            sunset = datetime.fromisoformat(weather["sunset"])
            # API returns today's times — shift to requested date if different
            target_date = date.date() if isinstance(date, datetime) else date
            if sunrise.date() != target_date:
                sunrise = sunrise.replace(year=target_date.year, month=target_date.month, day=target_date.day)
            if sunset.date() != target_date:
                sunset = sunset.replace(year=target_date.year, month=target_date.month, day=target_date.day)
            _sun_cache[f"{date_key}_sunrise"] = sunrise
            _sun_cache[f"{date_key}_sunset"] = sunset
            return sunrise, sunset
    except Exception:
        pass

    # Fallback: reasonable defaults for near-equator (Costa Rica ~5:30/17:45)
    sunrise = date.replace(hour=5, minute=30, second=0, microsecond=0)
    sunset = date.replace(hour=17, minute=45, second=0, microsecond=0)
    return sunrise, sunset


def set_sun_times(sunrise_iso: str, sunset_iso: str):
    """Set sunrise/sunset from external source (e.g. weather fetch in monitor)."""
    sunrise = datetime.fromisoformat(sunrise_iso)
    sunset = datetime.fromisoformat(sunset_iso)
    date_key = sunrise.strftime("%Y-%m-%d")
    _sun_cache[f"{date_key}_sunrise"] = sunrise
    _sun_cache[f"{date_key}_sunset"] = sunset


def estimate_usable_solar_hour(cloud_cover: float = 50.0) -> float:
    """When solar becomes usable. Rainy = 10am, clear = 8am, interpolated."""
    t = min(cloud_cover, 100.0) / 100.0
    return CLEAR_USABLE_SOLAR_HOUR + t * (
        CLOUDY_USABLE_SOLAR_HOUR - CLEAR_USABLE_SOLAR_HOUR
    )


def _load_hourly_profile() -> dict[int, float]:
    """Load the historical hourly load profile from the database.

    Returns {hour: avg_watts} for overnight hours. Falls back to sensible
    defaults if no data is available.
    """
    try:
        from solar_monitor.database import get_hourly_load_profile
        profile = get_hourly_load_profile(days=30)
        if profile and len(profile) >= 8:
            return profile
    except Exception:
        pass

    # Fallback: typical Costa Rica off-grid overnight curve
    return {
        17: 700, 18: 740, 19: 687, 20: 622, 21: 550, 22: 475,
        23: 428, 0: 397, 1: 373, 2: 359, 3: 349, 4: 342,
        5: 333, 6: 355, 7: 411, 8: 500,
    }


def integrate_hourly_drain(
    start: datetime,
    end: datetime,
    hourly_profile: dict[int, float],
    current_load_w: float,
    battery_capacity_wh: float = BATTERY_CAPACITY_WH,
) -> tuple[float, datetime | None]:
    """Walk forward hour-by-hour, integrating energy drain using historical load profile.

    Returns (total_soc_drop_pct, time_when_empty_or_None).
    For the current partial hour, uses the actual current load reading.
    For future hours, uses the historical profile.
    """
    total_wh = 0.0
    cursor = start

    while cursor < end:
        # How much of this hour remains?
        next_hour = (cursor + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        segment_end = min(next_hour, end)
        segment_hours = (segment_end - cursor).total_seconds() / 3600

        hour = cursor.hour
        # For the first segment, blend current load with historical
        if cursor == start:
            load_w = current_load_w
        else:
            load_w = hourly_profile.get(hour, current_load_w)

        segment_wh = load_w * segment_hours
        total_wh += segment_wh

        cursor = segment_end

    total_soc_drop = (total_wh / battery_capacity_wh) * 100
    return total_soc_drop, total_wh


def find_empty_time(
    start: datetime,
    end: datetime,
    starting_soc: float,
    hourly_profile: dict[int, float],
    current_load_w: float,
    battery_capacity_wh: float = BATTERY_CAPACITY_WH,
) -> datetime | None:
    """Walk forward to find when SOC hits the safety cutoff. Returns None if it won't."""
    usable_soc = max(0, starting_soc - BATTERY_SAFETY_CUTOFF)
    usable_wh = (usable_soc / 100.0) * battery_capacity_wh
    consumed_wh = 0.0
    cursor = start

    while cursor < end:
        next_hour = (cursor + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        segment_end = min(next_hour, end)
        segment_hours = (segment_end - cursor).total_seconds() / 3600

        hour = cursor.hour
        load_w = current_load_w if cursor == start else hourly_profile.get(hour, current_load_w)

        segment_wh = load_w * segment_hours
        if consumed_wh + segment_wh >= usable_wh:
            # Empty happens partway through this segment
            remaining_wh = usable_wh - consumed_wh
            hours_into_segment = remaining_wh / load_w if load_w > 0 else 0
            return cursor + timedelta(hours=hours_into_segment)

        consumed_wh += segment_wh
        cursor = segment_end

    return None  # Won't deplete before end


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
    """Real-time forecast: will battery last until solar becomes usable?

    Uses historical hourly load profiles for overnight drain prediction
    instead of flat-rate projection.
    """
    if now is None:
        now = datetime.now()

    tomorrow = now + timedelta(days=1)
    sunrise_today, sunset_today = get_sunrise_sunset(now)
    sunrise_tomorrow, _ = get_sunrise_sunset(tomorrow)

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

    is_after_sunset = now > sunset_today
    is_before_sunrise = now < sunrise_today
    is_nighttime = is_after_sunset or is_before_sunrise

    # Load hourly profile for integration
    hourly_profile = _load_hourly_profile()

    # Current net drain (for display)
    if is_nighttime:
        net_drain_w = load_power_w
    elif pv_power_w < load_power_w and not is_charging:
        net_drain_w = load_power_w - pv_power_w
    elif is_charging:
        net_drain_w = 0
    else:
        net_drain_w = load_power_w

    # Daytime with solar: battery is recovering
    if not is_nighttime and (is_charging or pv_power_w > 0):
        # Integrate overnight drain using hourly profile (sunset to usable solar)
        soc_drop_overnight, _ = integrate_hourly_drain(
            sunset_today, next_usable, hourly_profile, load_power_w, battery_capacity_wh
        )

        # Estimate SOC at sunset: current SOC + charging gains
        if is_charging and battery_power_w > 0:
            hours_to_sunset = max(0, (sunset_today - now).total_seconds() / 3600)
            soc_gain = (battery_power_w * hours_to_sunset / battery_capacity_wh) * 100
            estimated_soc_at_sunset = min(100, soc + soc_gain)
        else:
            estimated_soc_at_sunset = soc

        estimated_soc_at_sunrise = max(0, estimated_soc_at_sunset - soc_drop_overnight)
        estimated_soc_at_usable = estimated_soc_at_sunrise
        will_deplete = False
        risk_level = "ok"
        hours_until_empty = float("inf")
    else:
        # Nighttime: integrate from now using hourly profile
        soc_drop_sunrise, _ = integrate_hourly_drain(
            now, next_sunrise, hourly_profile, load_power_w, battery_capacity_wh
        )
        estimated_soc_at_sunrise = soc - soc_drop_sunrise

        soc_drop_usable, _ = integrate_hourly_drain(
            now, next_usable, hourly_profile, load_power_w, battery_capacity_wh
        )
        estimated_soc_at_usable = soc - soc_drop_usable

        # Find when battery will actually hit cutoff
        empty_time = find_empty_time(
            now, next_usable, soc, hourly_profile, load_power_w, battery_capacity_wh
        )

        if empty_time:
            hours_until_empty = (empty_time - now).total_seconds() / 3600
        else:
            usable_soc = max(0, soc - BATTERY_SAFETY_CUTOFF)
            usable_wh = (usable_soc / 100.0) * battery_capacity_wh
            hours_until_empty = usable_wh / net_drain_w if net_drain_w > 0 else float("inf")

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
    _, sunset_today = get_sunrise_sunset(now)
    sunrise_tomorrow, _ = get_sunrise_sunset(tomorrow)

    # When will solar actually produce enough to matter?
    usable_hour = estimate_usable_solar_hour(tomorrow_cloud_pct)
    usable_solar_time = tomorrow.replace(
        hour=int(usable_hour),
        minute=int((usable_hour % 1) * 60),
        second=0,
        microsecond=0,
    )

    # Hours on battery = sunset to usable solar
    drain_start = now if now > sunset_today else sunset_today
    hours_on_battery = max(0, (usable_solar_time - drain_start).total_seconds() / 3600)

    # Hourly profile integration instead of flat rate
    hourly_profile = _load_hourly_profile()
    soc_drop_pct, energy_needed_wh = integrate_hourly_drain(
        drain_start, usable_solar_time, hourly_profile,
        avg_overnight_load_w, battery_capacity_wh
    )

    energy_available_wh = (
        max(0, (current_soc - BATTERY_SAFETY_CUTOFF) / 100.0) * battery_capacity_wh
    )
    surplus_deficit_wh = energy_available_wh - energy_needed_wh

    estimated_soc_at_10am = current_soc - soc_drop_pct

    # When will battery hit cutoff?
    empty_time_dt = find_empty_time(
        drain_start, usable_solar_time, current_soc,
        hourly_profile, avg_overnight_load_w, battery_capacity_wh
    )
    estimated_empty_time = empty_time_dt.strftime("%H:%M") if empty_time_dt else "N/A"

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
