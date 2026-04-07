"""Alert handlers for macOS notifications and WhatsApp.

Alert philosophy: catch problems EARLY with escalating severity.
- Situational alerts fire based on time-of-day + conditions
- Each alert type has its own cooldown so they don't block each other
- Evening alerts give 6+ hours of lead time
- Night alerts escalate as the situation worsens
"""

import os
import subprocess
import logging
import urllib.request
import urllib.parse
import json
from datetime import datetime, timedelta

from solar_monitor.forecast import BatteryForecast, OvernightForecast

logger = logging.getLogger(__name__)

# Per-alert-type cooldowns to avoid spam but allow different alerts to fire
_cooldowns: dict[str, datetime] = {}


def _cooldown_ok(alert_type: str, hours: float = 1.0) -> bool:
    last = _cooldowns.get(alert_type)
    if last is None:
        return True
    return (datetime.now() - last) > timedelta(hours=hours)


def _mark_sent(alert_type: str):
    _cooldowns[alert_type] = datetime.now()


def send_macos_notification(title: str, message: str, sound: str = "Sosumi"):
    """Send a macOS notification using osascript."""
    message = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    title = title.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display notification "{message}" with title "{title}" sound name "{sound}"'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        logger.info(f"macOS notification sent: {title}")
    except Exception as e:
        logger.error(f"Failed to send macOS notification: {e}")


def send_whatsapp_message(message: str, phone_number: str | None = None):
    """Open WhatsApp with a pre-filled message."""
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


def send_ntfy(title: str, message: str, priority: str = "default"):
    """Send push notification via ntfy.sh (free, shows on iOS/Android).

    Set NTFY_TOPIC in .env to a secret topic name (e.g. 'dan-solar-abc123').
    Install the ntfy app on iOS: https://apps.apple.com/app/ntfy/id1625396347
    Subscribe to your topic in the app.
    """
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return

    ntfy_url = os.getenv("NTFY_URL", "https://ntfy.sh")
    try:
        data = json.dumps(
            {
                "topic": topic,
                "title": title,
                "message": message,
                "priority": priority,
                "tags": ["battery", "solar"],
            }
        ).encode()
        req = urllib.request.Request(
            ntfy_url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        logger.info(f"ntfy push sent: {title}")
    except Exception as e:
        logger.error(f"Failed to send ntfy push: {e}")


def send_imessage(message: str, recipient: str | None = None):
    """Send iMessage via AppleScript — shows on all Apple devices.

    Set IMESSAGE_TO in .env (phone number or Apple ID email).
    Piggybacks on macOS Messages.app privilege.
    """
    to = recipient or os.getenv("IMESSAGE_TO")
    if not to:
        return

    escaped = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant "{to}" of targetService
        send "{escaped}" to targetBuddy
    end tell
    '''
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
        logger.info(f"iMessage sent to {to}")
    except Exception as e:
        logger.error(f"Failed to send iMessage: {e}")


def _send(
    alert_type: str,
    title: str,
    msg: str,
    sound: str = "Sosumi",
    whatsapp_phone: str | None = None,
):
    """Send to ALL configured notification channels."""
    logger.warning(f"ALERT [{alert_type}]: {title} — {msg}")

    # macOS notification (always)
    send_macos_notification(title, msg, sound=sound)

    # iOS push via ntfy.sh
    priority = "urgent" if "critical" in alert_type.lower() or "🚨" in title else "high"
    send_ntfy(title, msg, priority=priority)

    # iMessage (shows on iPhone/iPad/Watch)
    send_imessage(f"{title}\n{msg}")

    # WhatsApp
    if whatsapp_phone:
        send_whatsapp_message(f"{title}\n\n{msg}", whatsapp_phone)

    _mark_sent(alert_type)


# ── Realtime forecast alerts ──────────────────────────────────────


def check_and_alert(forecast: BatteryForecast, whatsapp_phone: str | None = None):
    """Check realtime forecast and send alerts if at risk."""
    if forecast.risk_level in ("ok", "watch"):
        return

    if not _cooldown_ok("realtime", hours=1):
        return

    hours = forecast.hours_until_empty
    hrs_str = f"{int(hours)}h{int((hours % 1) * 60)}m" if hours < 100 else "plenty"

    msg = (
        f"SOC: {forecast.current_soc:.0f}% | "
        f"Drain: {forecast.drain_rate_w:.0f}W | "
        f"Empty in: {hrs_str} | "
        f"SOC@sunrise: {forecast.estimated_soc_at_usable:.0f}%"
    )
    sound = "Basso" if forecast.risk_level == "critical" else "Sosumi"
    emoji = "🚨" if forecast.risk_level == "critical" else "⚠️"
    _send(
        "realtime",
        f"{emoji} Battery {forecast.risk_level.upper()}",
        msg,
        sound=sound,
        whatsapp_phone=whatsapp_phone,
    )


# ── Overnight forecast alerts (from 4pm) ──────────────────────────


def check_overnight_alert(
    overnight: OvernightForecast, whatsapp_phone: str | None = None
):
    """Evening overnight forecast alerts.

    Fires from 4pm onward to give 6+ hours of lead time.
    """
    if overnight.risk_level == "ok":
        return

    if not _cooldown_ok("overnight", hours=2):
        return

    if overnight.risk_level == "critical":
        title = "🚨 BATTERY WILL RUN OUT TONIGHT"
        sound = "Basso"
    elif overnight.risk_level == "warning":
        title = "⚠️ Battery Warning — Tonight"
        sound = "Basso"
    else:
        title = "👀 Battery Watch — Tonight"
        sound = "Sosumi"

    msg = (
        f"SOC: {overnight.soc_at_sunset:.0f}% | "
        f"Night load: {overnight.avg_overnight_load_w:.0f}W\n"
        f"Tomorrow clouds: {overnight.tomorrow_cloud_pct:.0f}% | "
        f"SOC@10am: {overnight.estimated_soc_at_10am:.0f}%\n"
        f"{overnight.action_needed}"
    )
    _send("overnight", title, msg, sound=sound, whatsapp_phone=whatsapp_phone)


# ── Situational alerts ─────────────────────────────────────────────


def check_situational_alerts(
    soc: float,
    load_power_w: float,
    pv_power_w: float,
    is_charging: bool,
    whatsapp_phone: str | None = None,
):
    """Smart alerts based on time-of-day and conditions.

    These catch problems that the forecast model might miss because
    they're based on instantaneous readings, not projections.
    """
    now = datetime.now()
    hour = now.hour

    # ── Heavy drain after sunset (6pm-midnight) ──
    # If draining > 10% per hour after sunset, that's 6-7 hours to empty
    # from 100%. From 70% that's only 4-5 hours = dead by midnight.
    if 18 <= hour <= 23 and not is_charging and load_power_w > 0:
        # Estimate drain rate as % per hour (using 15kWh = 150Wh per %)
        drain_pct_per_hour = load_power_w / 150  # Wh per % on 15kWh battery
        if drain_pct_per_hour > 10 and _cooldown_ok("heavy_evening_drain", hours=1):
            hours_to_empty = max(0, (soc - 20)) / drain_pct_per_hour
            _send(
                "heavy_evening_drain",
                "🔥 Heavy Evening Drain",
                (
                    f"Draining ~{drain_pct_per_hour:.0f}%/hr at {load_power_w:.0f}W\n"
                    f"SOC: {soc:.0f}% — empty by ~{(now + timedelta(hours=hours_to_empty)).strftime('%I:%M %p')}\n"
                    f"Consider turning off non-essentials"
                ),
                sound="Sosumi",
                whatsapp_phone=whatsapp_phone,
            )

    # ── Low SOC at 6pm (your 70% threshold) ──
    if hour == 18 and soc < 70 and _cooldown_ok("low_soc_6pm", hours=12):
        _send(
            "low_soc_6pm",
            "⚠️ Low Battery at Sunset",
            (
                f"SOC: {soc:.0f}% at 6pm — below 70% safety margin\n"
                f"Load: {load_power_w:.0f}W | "
                f"Historically this leads to outages in rainy season\n"
                f"Reduce evening usage now"
            ),
            sound="Basso",
            whatsapp_phone=whatsapp_phone,
        )

    # ── Low SOC at bedtime (your 50% threshold) ──
    if hour == 22 and soc < 50 and _cooldown_ok("low_soc_10pm", hours=12):
        hours_left = max(0, (soc - 20)) / max(1, load_power_w / 150)
        _send(
            "low_soc_10pm",
            "🚨 Low Battery at Bedtime",
            (
                f"SOC: {soc:.0f}% at 10pm with {load_power_w:.0f}W load\n"
                f"~{hours_left:.0f}h until cutoff\n"
                f"Turn off fans/projector or you'll lose power overnight"
            ),
            sound="Basso",
            whatsapp_phone=whatsapp_phone,
        )

    # ── Battery not charging when it should be (daytime, sunny, but SOC dropping) ──
    if 9 <= hour <= 15 and pv_power_w > 500 and not is_charging and soc < 90:
        if _cooldown_ok("not_charging_daytime", hours=2):
            _send(
                "not_charging_daytime",
                "🔧 Battery Not Charging",
                (
                    f"PV: {pv_power_w:.0f}W but battery not charging (SOC: {soc:.0f}%)\n"
                    f"Load: {load_power_w:.0f}W — load may be exceeding solar\n"
                    f"Check high-draw appliances"
                ),
                sound="Sosumi",
                whatsapp_phone=whatsapp_phone,
            )

    # ── Critically low SOC anytime (emergency) ──
    if soc <= 25 and not is_charging and _cooldown_ok("critical_soc", hours=0.5):
        _send(
            "critical_soc",
            "🚨 BATTERY CRITICAL",
            (
                f"SOC: {soc:.0f}% — approaching 20% safety cutoff\n"
                f"Load: {load_power_w:.0f}W\n"
                f"Shut down non-essential loads NOW"
            ),
            sound="Basso",
            whatsapp_phone=whatsapp_phone,
        )

    # ── No solar production during daytime (system problem?) ──
    if 8 <= hour <= 16 and pv_power_w == 0 and _cooldown_ok("no_solar", hours=4):
        _send(
            "no_solar",
            "☁️ No Solar Production",
            (
                f"PV: 0W at {now.strftime('%I:%M %p')}\n"
                f"Could be heavy clouds, or check inverter/panels"
            ),
            sound="Sosumi",
        )
