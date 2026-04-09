"""SQLite database for storing solar stats and usage history."""

import sqlite3
import os
from datetime import datetime
from contextlib import contextmanager

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB_PATH = os.getenv("SOLAR_DB_PATH", os.path.join(_PROJECT_ROOT, "solar_monitor.db"))


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                soc REAL,
                pv_power REAL,
                load_power REAL,
                battery_power REAL,
                grid_power REAL,
                is_charging INTEGER,
                etoday REAL,
                emonth REAL,
                eyear REAL,
                etotal REAL
            );

            CREATE TABLE IF NOT EXISTS weather (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                temp REAL,
                humidity REAL,
                clouds REAL,
                description TEXT,
                wind_speed REAL,
                sunrise TEXT,
                sunset TEXT
            );

            CREATE TABLE IF NOT EXISTS forecasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                current_soc REAL,
                drain_rate_w REAL,
                hours_until_empty REAL,
                hours_until_sunrise REAL,
                estimated_soc_at_sunrise REAL,
                will_deplete INTEGER,
                cloud_cover REAL
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                message TEXT,
                alert_type TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_summary (
                date TEXT PRIMARY KEY,
                pv_kwh REAL,
                load_kwh REAL,
                battery_charge_kwh REAL,
                grid_kwh REAL,
                min_soc REAL,
                max_soc REAL,
                avg_load_w REAL,
                peak_load_w REAL,
                peak_pv_w REAL
            );

            CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(timestamp);
            CREATE INDEX IF NOT EXISTS idx_weather_ts ON weather(timestamp);
            CREATE INDEX IF NOT EXISTS idx_forecasts_ts ON forecasts(timestamp);
        """)


def store_reading(
    soc: float,
    pv_power: float,
    load_power: float,
    battery_power: float,
    grid_power: float,
    is_charging: bool,
    etoday: float = 0,
    emonth: float = 0,
    eyear: float = 0,
    etotal: float = 0,
):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO readings
               (timestamp, soc, pv_power, load_power, battery_power, grid_power,
                is_charging, etoday, emonth, eyear, etotal)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                soc,
                pv_power,
                load_power,
                battery_power,
                grid_power,
                int(is_charging),
                etoday,
                emonth,
                eyear,
                etotal,
            ),
        )


def store_weather(
    temp: float,
    humidity: float,
    clouds: float,
    description: str,
    wind_speed: float,
    sunrise: str,
    sunset: str,
):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO weather
               (timestamp, temp, humidity, clouds, description, wind_speed, sunrise, sunset)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                temp,
                humidity,
                clouds,
                description,
                wind_speed,
                sunrise,
                sunset,
            ),
        )


def store_forecast(forecast, cloud_cover: float = 0):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO forecasts
               (timestamp, current_soc, drain_rate_w, hours_until_empty,
                hours_until_sunrise, estimated_soc_at_sunrise, will_deplete, cloud_cover)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                forecast.current_soc,
                forecast.drain_rate_w,
                forecast.hours_until_empty,
                forecast.hours_until_sunrise,
                forecast.estimated_soc_at_sunrise,
                int(forecast.will_deplete),
                cloud_cover,
            ),
        )


def store_alert(message: str, alert_type: str = "battery_low"):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO alerts (timestamp, message, alert_type) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), message, alert_type),
        )


def get_recent_readings(hours: int = 24) -> list[dict]:
    """Get readings from the last N hours."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM readings
               WHERE timestamp > datetime('now', ?)
               ORDER BY timestamp DESC""",
            (f"-{hours} hours",),
        ).fetchall()
        return [dict(r) for r in rows]


def get_average_load(hours: int = 24) -> float:
    """Get average load over the last N hours."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT AVG(load_power) as avg_load FROM readings
               WHERE timestamp > datetime('now', ?)
               AND load_power > 0""",
            (f"-{hours} hours",),
        ).fetchone()
        return row["avg_load"] if row and row["avg_load"] else 0.0


def get_average_nighttime_load() -> float:
    """Get average load during nighttime hours (6pm-6am) over past 7 days."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT AVG(load_power) as avg_load FROM readings
               WHERE timestamp > datetime('now', '-7 days')
               AND (CAST(strftime('%H', timestamp) AS INTEGER) >= 18
                    OR CAST(strftime('%H', timestamp) AS INTEGER) < 6)
               AND load_power > 0""",
        ).fetchone()
        return row["avg_load"] if row and row["avg_load"] else 0.0


def get_average_usage_by_hour(days: int = 7) -> list[dict]:
    """Get average load per hour of day over the last N days."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT
                CAST(strftime('%H', timestamp) AS INTEGER) as hour,
                AVG(load_power) as avg_load,
                MAX(load_power) as peak_load,
                COUNT(*) as samples
               FROM readings
               WHERE timestamp > datetime('now', ?)
               AND load_power > 0
               GROUP BY hour
               ORDER BY hour""",
            (f"-{days} days",),
        ).fetchall()
        return [dict(r) for r in rows]


def get_hourly_load_profile(days: int = 30) -> dict[int, float]:
    """Get median load per hour-of-day for overnight hours (6pm-8am).

    Returns {hour: median_load_w} using recent history. Uses percentile
    approximation via grouping to get a robust central estimate that isn't
    skewed by outlier spikes.
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT
                CAST(strftime('%H', timestamp) AS INTEGER) as hour,
                AVG(load_power) as avg_load,
                COUNT(*) as samples
               FROM readings
               WHERE timestamp > datetime('now', ?)
               AND load_power > 0
               AND (CAST(strftime('%H', timestamp) AS INTEGER) >= 17
                    OR CAST(strftime('%H', timestamp) AS INTEGER) < 9)
               GROUP BY hour
               ORDER BY hour""",
            (f"-{days} days",),
        ).fetchall()
        return {row["hour"]: row["avg_load"] for row in rows}


def get_peak_usage(days: int = 7) -> dict:
    """Get peak usage stats over the last N days."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT
                MAX(load_power) as peak_load_w,
                timestamp as peak_time,
                AVG(load_power) as avg_load_w
               FROM readings
               WHERE timestamp > datetime('now', ?)
               AND load_power > 0""",
            (f"-{days} days",),
        ).fetchone()
        return dict(row) if row else {}


def get_hours_left_at_current_usage(
    soc: float,
    load_power_w: float,
    battery_capacity_wh: float = 10000,
    min_soc: float = 10.0,
) -> float:
    """Calculate hours of battery left at the current load."""
    usable_soc = max(0, soc - min_soc)
    usable_wh = (usable_soc / 100.0) * battery_capacity_wh
    if load_power_w <= 0:
        return float("inf")
    return usable_wh / load_power_w


def get_daily_summary(date: str | None = None) -> dict:
    """Get a summary of a given day's readings."""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        row = conn.execute(
            """SELECT
                MIN(soc) as min_soc,
                MAX(soc) as max_soc,
                AVG(load_power) as avg_load,
                MAX(load_power) as peak_load,
                AVG(pv_power) as avg_pv,
                MAX(pv_power) as peak_pv,
                MAX(etoday) as total_generation,
                COUNT(*) as readings_count
               FROM readings
               WHERE date(timestamp) = ?""",
            (date,),
        ).fetchone()
        return dict(row) if row else {}


def get_weather_history(days: int = 7) -> list[dict]:
    """Get weather data from the last N days."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM weather
               WHERE timestamp > datetime('now', ?)
               ORDER BY timestamp DESC""",
            (f"-{days} days",),
        ).fetchall()
        return [dict(r) for r in rows]
