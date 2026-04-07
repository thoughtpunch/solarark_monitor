#!/usr/bin/env python3
"""Analyze historical data: find outages, back-predict, and identify risk patterns."""

import logging
from datetime import datetime, timedelta

from solar_monitor.database import init_db, get_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

SAFETY_CUTOFF = 20.0


def find_outages() -> list[dict]:
    """Find all times the battery hit or went below the safety cutoff.

    An 'outage' is a period where SOC dropped to/below 20%.
    Returns start time, end time (when SOC recovered), duration, and
    what the SOC was at 6pm and 10pm the night before.
    """
    with get_db() as conn:
        # Find all readings where SOC <= cutoff, grouped into contiguous periods
        rows = conn.execute(
            """
            SELECT timestamp, soc, pv_power, load_power
            FROM readings
            WHERE soc IS NOT NULL AND soc <= ?
            ORDER BY timestamp
        """,
            (SAFETY_CUTOFF,),
        ).fetchall()

    if not rows:
        return []

    # Group contiguous low-SOC readings into outage periods
    outages = []
    current_outage = None

    for row in rows:
        ts = datetime.fromisoformat(row["timestamp"])

        if current_outage is None:
            current_outage = {
                "start": ts,
                "end": ts,
                "min_soc": row["soc"],
                "readings": 1,
            }
        elif (ts - current_outage["end"]).total_seconds() <= 600:  # Within 10 min
            current_outage["end"] = ts
            current_outage["min_soc"] = min(current_outage["min_soc"], row["soc"])
            current_outage["readings"] += 1
        else:
            # Gap > 10 min — this is a new outage
            outages.append(current_outage)
            current_outage = {
                "start": ts,
                "end": ts,
                "min_soc": row["soc"],
                "readings": 1,
            }

    if current_outage:
        outages.append(current_outage)

    return outages


def back_predict_outage(outage: dict) -> dict:
    """For a given outage, look back at what happened the evening before.

    Answers: what was the SOC at 6pm? 10pm? What was the avg load overnight?
    Could we have predicted this?
    """
    outage_start = outage["start"]
    # The evening before = same day if outage is before noon, otherwise day before
    if outage_start.hour < 12:
        evening_date = outage_start - timedelta(days=1)
    else:
        evening_date = outage_start

    with get_db() as conn:
        # SOC at 6pm the evening before
        soc_6pm = conn.execute(
            """
            SELECT soc FROM readings
            WHERE timestamp BETWEEN ? AND ?
            AND soc IS NOT NULL
            ORDER BY ABS(CAST(strftime('%H', timestamp) AS INTEGER) - 18)
            LIMIT 1
        """,
            (
                evening_date.strftime("%Y-%m-%d 17:30"),
                evening_date.strftime("%Y-%m-%d 18:30"),
            ),
        ).fetchone()

        # SOC at 10pm
        soc_10pm = conn.execute(
            """
            SELECT soc FROM readings
            WHERE timestamp BETWEEN ? AND ?
            AND soc IS NOT NULL
            ORDER BY ABS(CAST(strftime('%H', timestamp) AS INTEGER) - 22)
            LIMIT 1
        """,
            (
                evening_date.strftime("%Y-%m-%d 21:30"),
                evening_date.strftime("%Y-%m-%d 22:30"),
            ),
        ).fetchone()

        # Average overnight load (6pm to outage start)
        avg_load = conn.execute(
            """
            SELECT AVG(load_power) as avg_load, MAX(load_power) as peak_load
            FROM readings
            WHERE timestamp BETWEEN ? AND ?
            AND load_power > 0
        """,
            (
                evening_date.strftime("%Y-%m-%d 18:00"),
                outage_start.isoformat(),
            ),
        ).fetchone()

        # When did SOC start dropping fast? (biggest 1-hour drop)
        hourly_drops = conn.execute(
            """
            SELECT
                timestamp,
                soc,
                soc - LEAD(soc, 12) OVER (ORDER BY timestamp) as drop_1h
            FROM readings
            WHERE timestamp BETWEEN ? AND ?
            AND soc IS NOT NULL
            ORDER BY timestamp
        """,
            (
                evening_date.strftime("%Y-%m-%d 16:00"),
                outage_start.isoformat(),
            ),
        ).fetchall()

        max_drop = None
        for r in hourly_drops:
            if r["drop_1h"] and (
                max_drop is None or r["drop_1h"] > max_drop["drop_1h"]
            ):
                max_drop = dict(r)

    duration_hours = (outage["end"] - outage["start"]).total_seconds() / 3600

    # Recovery: when did SOC get back above cutoff?
    with get_db() as conn:
        recovery = conn.execute(
            """
            SELECT timestamp, soc FROM readings
            WHERE timestamp > ?
            AND soc IS NOT NULL AND soc > ?
            ORDER BY timestamp
            LIMIT 1
        """,
            (outage["end"].isoformat(), SAFETY_CUTOFF),
        ).fetchone()

    recovery_time = datetime.fromisoformat(recovery["timestamp"]) if recovery else None
    total_downtime = (
        (recovery_time - outage["start"]).total_seconds() / 3600
        if recovery_time
        else None
    )

    return {
        "outage_start": outage["start"].strftime("%Y-%m-%d %H:%M"),
        "outage_end": outage["end"].strftime("%Y-%m-%d %H:%M"),
        "min_soc": outage["min_soc"],
        "duration_hours": round(duration_hours, 1),
        "recovery_time": recovery_time.strftime("%H:%M")
        if recovery_time
        else "unknown",
        "total_downtime_hours": round(total_downtime, 1) if total_downtime else None,
        "soc_at_6pm": soc_6pm["soc"] if soc_6pm else None,
        "soc_at_10pm": soc_10pm["soc"] if soc_10pm else None,
        "avg_overnight_load_w": round(avg_load["avg_load"], 0)
        if avg_load and avg_load["avg_load"]
        else None,
        "peak_overnight_load_w": round(avg_load["peak_load"], 0)
        if avg_load and avg_load["peak_load"]
        else None,
        "fastest_drop": max_drop,
    }


def analyze_risk_patterns():
    """Analyze what conditions lead to outages vs safe nights."""
    with get_db() as conn:
        # Get all days with SOC data at 6pm
        evenings = conn.execute("""
            SELECT
                date(timestamp) as date,
                soc,
                load_power
            FROM readings
            WHERE CAST(strftime('%H', timestamp) AS INTEGER) = 18
            AND soc IS NOT NULL
            ORDER BY timestamp
        """).fetchall()

        # For each evening, check if the next morning had an outage
        results = []
        for row in evenings:
            date = row["date"]
            next_morning = conn.execute(
                """
                SELECT MIN(soc) as min_soc
                FROM readings
                WHERE timestamp BETWEEN ? AND ?
                AND soc IS NOT NULL
            """,
                (
                    f"{date} 22:00",
                    # Next day 12:00
                    (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime(
                        "%Y-%m-%d 12:00"
                    ),
                ),
            ).fetchone()

            had_outage = (
                next_morning
                and next_morning["min_soc"] is not None
                and next_morning["min_soc"] <= SAFETY_CUTOFF
            )

            results.append(
                {
                    "date": date,
                    "soc_6pm": row["soc"],
                    "load_6pm": row["load_power"],
                    "had_outage": had_outage,
                    "next_morning_min_soc": next_morning["min_soc"]
                    if next_morning
                    else None,
                }
            )

    return results


def print_report():
    """Print a full analysis report."""
    init_db()

    print("\n" + "=" * 70)
    print("  SOLAR MONITOR — HISTORICAL OUTAGE ANALYSIS")
    print("=" * 70)

    # Find outages
    outages = find_outages()
    print(f"\n{'=' * 70}")
    print(f"  OUTAGES FOUND: {len(outages)}")
    print(f"{'=' * 70}")

    if not outages:
        print("  No outages detected (SOC never hit 20%)!")
    else:
        for i, outage in enumerate(outages, 1):
            bp = back_predict_outage(outage)
            print(f"\n  --- Outage #{i} ---")
            print(f"  When:       {bp['outage_start']} → {bp['outage_end']}")
            print(f"  Min SOC:    {bp['min_soc']}%")
            print(
                f"  Duration:   {bp['duration_hours']}h (recovered at {bp['recovery_time']})"
            )
            if bp["total_downtime_hours"]:
                print(f"  Total down: {bp['total_downtime_hours']}h")
            print("  ---- Evening before ----")
            print(f"  SOC at 6pm:   {bp['soc_at_6pm']}%")
            print(f"  SOC at 10pm:  {bp['soc_at_10pm']}%")
            print(f"  Avg load:     {bp['avg_overnight_load_w']}W")
            print(f"  Peak load:    {bp['peak_overnight_load_w']}W")
            if bp["fastest_drop"]:
                print(
                    f"  Fastest drop: {bp['fastest_drop']['drop_1h']:.0f}% in 1h at {bp['fastest_drop']['timestamp'][:16]}"
                )
            print("  ---- Could we have predicted? ----")
            if bp["soc_at_6pm"] is not None:
                if bp["soc_at_6pm"] < 70:
                    print(
                        f"  YES — SOC was {bp['soc_at_6pm']}% at 6pm (below 70% threshold)"
                    )
                elif bp["soc_at_10pm"] is not None and bp["soc_at_10pm"] < 50:
                    print(
                        f"  YES — SOC was {bp['soc_at_10pm']}% at 10pm (below 50% threshold)"
                    )
                else:
                    print(
                        f"  MAYBE — SOC was {bp['soc_at_6pm']}% at 6pm, {bp['soc_at_10pm']}% at 10pm"
                    )
                    print(
                        f"         Load was higher than expected ({bp['avg_overnight_load_w']}W avg)"
                    )

    # Risk pattern analysis
    print(f"\n{'=' * 70}")
    print("  RISK PATTERN ANALYSIS")
    print(f"{'=' * 70}")

    patterns = analyze_risk_patterns()
    if patterns:
        outage_nights = [p for p in patterns if p["had_outage"]]
        safe_nights = [p for p in patterns if not p["had_outage"]]

        if outage_nights:
            avg_soc_outage = sum(p["soc_6pm"] for p in outage_nights) / len(
                outage_nights
            )
            print(f"\n  Outage nights ({len(outage_nights)}):")
            print(f"    Avg SOC at 6pm: {avg_soc_outage:.0f}%")
            for p in outage_nights:
                print(
                    f"    {p['date']}: {p['soc_6pm']:.0f}% at 6pm → min {p['next_morning_min_soc']:.0f}% overnight"
                )

        if safe_nights:
            avg_soc_safe = sum(p["soc_6pm"] for p in safe_nights) / len(safe_nights)
            print(f"\n  Safe nights ({len(safe_nights)}):")
            print(f"    Avg SOC at 6pm: {avg_soc_safe:.0f}%")

        # Find the danger threshold
        if outage_nights and safe_nights:
            min_safe_soc = min(p["soc_6pm"] for p in safe_nights)
            max_outage_soc = max(p["soc_6pm"] for p in outage_nights)
            print("\n  KEY FINDING:")
            print(f"    Lowest safe 6pm SOC:    {min_safe_soc:.0f}%")
            print(f"    Highest outage 6pm SOC: {max_outage_soc:.0f}%")
            if max_outage_soc < min_safe_soc:
                threshold = (min_safe_soc + max_outage_soc) / 2
                print(f"    → Alert threshold:      {threshold:.0f}% at 6pm")
            else:
                print(
                    f"    → Overlap zone: {min(min_safe_soc, max_outage_soc):.0f}-{max(min_safe_soc, max_outage_soc):.0f}%"
                )
                print("      (load variation matters more than SOC alone)")

    print(f"\n{'=' * 70}\n")


def main():
    print_report()


if __name__ == "__main__":
    main()
