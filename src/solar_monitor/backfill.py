#!/usr/bin/env python3
"""Backfill historical data from SolArk API into SQLite.

Fetches daily 5-minute interval power data (PV, Load, Battery, Grid, SOC)
for every day since the plant was created.
"""

import os
import time
import logging
import requests
from datetime import datetime, timedelta

from solar_monitor.database import init_db, get_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

API_BASE = "https://api.solarkcloud.com"
PLANT_ID = int(os.getenv("SOLARK_PLANT_ID", "0"))
USERNAME = os.getenv("SOLARK_USERNAME")
PASSWORD = os.getenv("SOLARK_PASSWORD")
PLANT_CREATED = os.getenv("PLANT_CREATED", "2025-01-01")

# Rate limit: pause between requests to be nice to the API
REQUEST_DELAY = 1.0  # seconds


class SolArkSession:
    """Lightweight session that handles auth and token refresh."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "application/json",
                "origin": "https://www.mysolark.com",
                "referer": "https://www.mysolark.com/",
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            }
        )
        self.token_expires = 0

    def login(self):
        resp = self.session.post(
            f"{API_BASE}/oauth/token",
            json={
                "username": USERNAME,
                "password": PASSWORD,
                "grant_type": "password",
                "client_id": "csp-web",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Login failed: {data.get('msg')}")

        token = data["data"]["access_token"]
        expires_in = data["data"].get("expires_in", 3600)
        self.session.headers["authorization"] = f"Bearer {token}"
        self.token_expires = time.time() + expires_in - 60  # Refresh 1 min early
        logger.info("Logged in to SolArk API")

    def ensure_auth(self):
        if time.time() > self.token_expires:
            logger.info("Token expired, re-logging in...")
            self.login()

    def get_day_power(self, date_str: str) -> dict | None:
        """Fetch 5-min interval power data for a specific date."""
        self.ensure_auth()
        resp = self.session.get(
            f"{API_BASE}/api/v1/plant/energy/{PLANT_ID}/day",
            params={"lan": "en", "date": date_str, "id": PLANT_ID},
        )
        if resp.status_code != 200:
            logger.error(f"HTTP {resp.status_code} for {date_str}")
            return None
        data = resp.json()
        if data.get("code") != 0:
            logger.error(f"API error for {date_str}: {data.get('msg')}")
            return None
        return data.get("data")

    def get_day_flow(self, date_str: str) -> dict | None:
        """Fetch energy flow for a specific date (includes SOC)."""
        self.ensure_auth()
        resp = self.session.get(
            f"{API_BASE}/api/v1/plant/energy/{PLANT_ID}/flow",
            params={"date": date_str},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("code") != 0:
            return None
        return data.get("data")


def parse_series(infos: list[dict]) -> dict[str, list[dict]]:
    """Parse the infos array into labeled series."""
    series = {}
    for info in infos:
        label = (info.get("label") or info.get("name") or "unknown").lower()
        records = info.get("records", [])
        series[label] = records
    return series


def store_historical_readings(date_str: str, series: dict[str, list[dict]]):
    """Store 5-min interval data as historical readings."""
    pv = {r["time"]: float(r["value"] or 0) for r in series.get("pv", [])}
    load = {
        r["time"]: float(r["value"] or 0)
        for r in series.get("load", series.get("consumption", []))
    }
    battery = {
        r["time"]: float(r["value"] or 0)
        for r in series.get("battery", series.get("batt", []))
    }
    grid = {r["time"]: float(r["value"] or 0) for r in series.get("grid", [])}
    soc_series = {r["time"]: float(r["value"] or 0) for r in series.get("soc", [])}

    # Get all unique timestamps
    all_times = sorted(set(list(pv.keys()) + list(load.keys())))
    if not all_times:
        return 0

    rows = []
    for t in all_times:
        timestamp = f"{date_str}T{t}:00"
        pv_val = pv.get(t, 0)
        load_val = load.get(t, 0)
        batt_val = battery.get(t, 0)
        grid_val = grid.get(t, 0)
        soc_val = soc_series.get(t)

        # Skip rows where everything is zero (no data)
        if pv_val == 0 and load_val == 0 and batt_val == 0:
            continue

        # Determine if charging: positive battery = charging, negative = draining
        # (convention varies, but generally battery power > 0 when charging)
        is_charging = 1 if batt_val > 0 else 0

        rows.append(
            (
                timestamp,
                soc_val,
                pv_val,
                load_val,
                abs(batt_val),
                grid_val,
                is_charging,
                0,
                0,
                0,
                0,
            )
        )

    if not rows:
        return 0

    with get_db() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO readings
               (timestamp, soc, pv_power, load_power, battery_power, grid_power,
                is_charging, etoday, emonth, eyear, etotal)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    return len(rows)


def compute_daily_summaries():
    """Compute daily summaries from 5-min readings."""
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO daily_summary
                (date, pv_kwh, load_kwh, battery_charge_kwh, grid_kwh,
                 min_soc, max_soc, avg_load_w, peak_load_w, peak_pv_w)
            SELECT
                date(timestamp) as date,
                SUM(pv_power) * 5.0 / 60.0 / 1000.0 as pv_kwh,
                SUM(load_power) * 5.0 / 60.0 / 1000.0 as load_kwh,
                SUM(CASE WHEN is_charging = 1 THEN battery_power ELSE 0 END) * 5.0 / 60.0 / 1000.0 as battery_charge_kwh,
                SUM(grid_power) * 5.0 / 60.0 / 1000.0 as grid_kwh,
                MIN(CASE WHEN soc > 0 THEN soc END) as min_soc,
                MAX(soc) as max_soc,
                AVG(load_power) as avg_load_w,
                MAX(load_power) as peak_load_w,
                MAX(pv_power) as peak_pv_w
            FROM readings
            GROUP BY date(timestamp)
        """)
        count = conn.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0]
        logger.info(f"Computed {count} daily summaries")


def backfill_monthly(session: "SolArkSession"):
    """Fetch monthly summary data (daily totals per month)."""
    logger.info("Fetching monthly summaries...")

    # Get all months from plant creation to now
    start = datetime.strptime(PLANT_CREATED, "%Y-%m-%d")
    now = datetime.now()

    month = start.replace(day=1)
    while month <= now:
        date_str = month.strftime("%Y-%m")
        try:
            session.ensure_auth()
            resp = session.session.get(
                f"{API_BASE}/api/v1/plant/energy/{PLANT_ID}/month",
                params={"lan": "en", "date": date_str, "id": PLANT_ID},
            )
            data = resp.json().get("data", {})
            infos = data.get("infos", [])

            if infos:
                for info in infos:
                    label = (info.get("label") or "").lower()
                    records = info.get("records", [])
                    nonzero = [
                        r for r in records if r.get("value") and float(r["value"]) > 0
                    ]
                    if label == "pv" and nonzero:
                        total = sum(float(r["value"]) for r in nonzero)
                        logger.info(
                            f"  {date_str}: PV {total:.1f}kWh over {len(nonzero)} days"
                        )

        except Exception as e:
            logger.error(f"  {date_str}: ERROR {e}")

        # Next month
        if month.month == 12:
            month = month.replace(year=month.year + 1, month=1)
        else:
            month = month.replace(month=month.month + 1)

        time.sleep(REQUEST_DELAY)


def backfill(start_date: str | None = None, end_date: str | None = None):
    """Backfill historical data from start_date to end_date."""
    init_db()

    # Add unique index to prevent duplicates on re-run
    with get_db() as conn:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_ts_unique ON readings(timestamp)"
        )

    start = datetime.strptime(start_date or PLANT_CREATED, "%Y-%m-%d")
    end = datetime.strptime(end_date or datetime.now().strftime("%Y-%m-%d"), "%Y-%m-%d")

    total_days = (end - start).days + 1
    logger.info(
        f"Backfilling {total_days} days: {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}"
    )

    session = SolArkSession()
    session.login()

    # Fetch monthly summaries first
    backfill_monthly(session)

    # Then fetch daily 5-min detail
    total_records = 0
    errors = 0

    for i in range(total_days):
        date = start + timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")

        try:
            data = session.get_day_power(date_str)
            if not data or not data.get("infos"):
                logger.warning(f"  {date_str}: no data")
                errors += 1
                time.sleep(REQUEST_DELAY)
                continue

            series = parse_series(data["infos"])
            count = store_historical_readings(date_str, series)
            total_records += count

            labels = ", ".join(series.keys())
            logger.info(
                f"  {date_str}: {count} records ({labels}) [{i + 1}/{total_days}]"
            )

        except Exception as e:
            logger.error(f"  {date_str}: ERROR {e}")
            errors += 1

        time.sleep(REQUEST_DELAY)

    # Compute daily summaries from the 5-min data
    compute_daily_summaries()

    logger.info(f"Backfill complete: {total_records} records, {errors} errors")

    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt, MIN(timestamp) as first, MAX(timestamp) as last FROM readings"
        ).fetchone()
        logger.info(
            f"Database: {row['cnt']} total readings, {row['first']} → {row['last']}"
        )


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Backfill historical solar data")
    parser.add_argument(
        "--start", default=PLANT_CREATED, help=f"Start date (default: {PLANT_CREATED})"
    )
    parser.add_argument("--end", default=None, help="End date (default: today)")
    args = parser.parse_args()
    backfill(args.start, args.end)


if __name__ == "__main__":
    main()
