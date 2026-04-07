"""Alert handlers for macOS notifications and WhatsApp."""

import subprocess
import logging
from datetime import datetime, timedelta
from solar_monitor.forecast import BatteryForecast, OvernightForecast

logger = logging.getLogger(__name__)

# Separate cooldowns for realtime vs overnight alerts
_last_realtime_alert: datetime | None = None
_last_overnight_alert: datetime | None = None
REALTIME_COOLDOWN = timedelta(hours=1)
OVERNIGHT_COOLDOWN = timedelta(hours=2)  # Don't spam overnight alerts


def _cooldown_ok(last_time: datetime | None, cooldown: timedelta) -> bool:
    if last_time is None:
        return True
    return (datetime.now() - last_time) > cooldown


def send_macos_notification(title: str, message: str, sound: str = "Sosumi"):
    """Send a macOS notification using osascript."""
    message = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    title = title.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display notification "{message}" with title "{title}" sound name "{sound}"'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        logger.info("macOS notification sent")
    except Exception as e:
        logger.error(f"Failed to send macOS notification: {e}")


def send_whatsapp_message(message: str, phone_number: str | None = None):
    """Open WhatsApp with a pre-filled message."""
    import os
    import urllib.parse

    phone = phone_number or os.getenv("WHATSAPP_PHONE")
    if not phone:
        return

    phone_clean = phone.lstrip("+")
    encoded_msg = urllib.parse.quote(message)
    url = f"https://wa.me/{phone_clean}?text={encoded_msg}"

    try:
        subprocess.run(["open", url], capture_output=True, timeout=10)
        logger.info(f"WhatsApp opened for {phone}")
    except Exception as e:
        logger.error(f"Failed to open WhatsApp: {e}")


def check_and_alert(forecast: BatteryForecast, whatsapp_phone: str | None = None):
    """Check realtime forecast and send alerts if at risk."""
    global _last_realtime_alert

    if forecast.risk_level in ("ok", "watch"):
        return

    if not _cooldown_ok(_last_realtime_alert, REALTIME_COOLDOWN):
        return

    hours = forecast.hours_until_empty
    hrs_str = f"{int(hours)}h{int((hours % 1) * 60)}m" if hours < 100 else "plenty"

    msg = (
        f"SOC: {forecast.current_soc:.0f}% | "
        f"Drain: {forecast.drain_rate_w:.0f}W | "
        f"Empty in: {hrs_str} | "
        f"SOC@10am: {forecast.estimated_soc_at_usable:.0f}%"
    )
    title = f"Battery {forecast.risk_level.upper()}"
    sound = "Basso" if forecast.risk_level == "critical" else "Sosumi"

    logger.warning(f"REALTIME ALERT [{forecast.risk_level.upper()}]: {msg}")
    send_macos_notification(title, msg, sound=sound)

    if whatsapp_phone:
        send_whatsapp_message(f"{title}\n{msg}", whatsapp_phone)

    _last_realtime_alert = datetime.now()


def check_overnight_alert(
    overnight: OvernightForecast, whatsapp_phone: str | None = None
):
    """Check overnight forecast and send advance warnings.

    This is the key alert — it fires in the evening (4pm+) to give you
    6+ hours of lead time before a potential outage at 4-7am.

    Alert thresholds tuned for Dan's experience:
    - Under 70% at 6pm = could be bad in rainy season
    - Under 50% at bedtime (10pm) with fans/projector = runs out ~6:30am
    """
    global _last_overnight_alert

    if overnight.risk_level == "ok":
        return

    if not _cooldown_ok(_last_overnight_alert, OVERNIGHT_COOLDOWN):
        return

    # Build alert message
    if overnight.risk_level == "critical":
        title = "BATTERY WILL RUN OUT TONIGHT"
        sound = "Basso"
    elif overnight.risk_level == "warning":
        title = "Battery Warning — Tonight"
        sound = "Basso"
    else:  # watch
        title = "Battery Watch — Tonight"
        sound = "Sosumi"

    msg = (
        f"SOC: {overnight.soc_at_sunset:.0f}% | "
        f"Night load: {overnight.avg_overnight_load_w:.0f}W\n"
        f"Tomorrow clouds: {overnight.tomorrow_cloud_pct:.0f}% | "
        f"SOC@10am: {overnight.estimated_soc_at_10am:.0f}%\n"
        f"{overnight.action_needed}"
    )

    logger.warning(f"OVERNIGHT ALERT [{overnight.risk_level.upper()}]: {msg}")
    send_macos_notification(title, msg, sound=sound)

    if whatsapp_phone:
        send_whatsapp_message(f"{title}\n\n{msg}", whatsapp_phone)

    _last_overnight_alert = datetime.now()
