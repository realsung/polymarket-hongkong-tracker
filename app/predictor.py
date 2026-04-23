from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .db import StoredReading


@dataclass
class PeakEstimate:
    status: str  # 'peaked' | 'rising' | 'falling' | 'flat' | 'unknown'
    temp: Optional[float]
    at_hkt: Optional[datetime]
    rate_c_per_hour: float
    confidence: str  # 'confirmed' | 'estimated' | 'forecast' | 'none'
    note: str


def _linear_rate_c_per_hour(points: list[StoredReading]) -> float:
    if len(points) < 2:
        return 0.0
    base = points[0].observed_at_utc
    xs = [(p.observed_at_utc - base).total_seconds() / 3600.0 for p in points]
    ys = [p.temperature for p in points]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den > 0 else 0.0


def estimate_peak(
    readings: list[StoredReading],
    forecast_max: Optional[float],
) -> PeakEstimate:
    if not readings:
        return PeakEstimate(
            status="unknown",
            temp=None,
            at_hkt=None,
            rate_c_per_hour=0.0,
            confidence="none",
            note="No readings yet for today.",
        )

    latest = readings[-1]
    running_max = max(readings, key=lambda r: r.temperature)
    now_hkt = latest.observed_at_hkt

    # Rate of change over the last 60 minutes
    recent = [
        r
        for r in readings
        if (latest.observed_at_utc - r.observed_at_utc).total_seconds() <= 3600
    ]
    rate = _linear_rate_c_per_hour(recent) if len(recent) >= 2 else 0.0

    minutes_since_max = (
        latest.observed_at_utc - running_max.observed_at_utc
    ).total_seconds() / 60.0

    # Past-peak heuristic: at least 90min since the max AND we're well below it
    # AND it's plausible the daily peak has already occurred (>=13:00 HKT).
    past_peak = (
        minutes_since_max >= 90
        and latest.temperature <= running_max.temperature - 0.3
        and now_hkt.hour >= 13
    )

    if past_peak:
        return PeakEstimate(
            status="peaked",
            temp=running_max.temperature,
            at_hkt=running_max.observed_at_hkt,
            rate_c_per_hour=rate,
            confidence="confirmed",
            note=(
                f"Today's high confirmed at {running_max.temperature:.1f}°C "
                f"({running_max.observed_at_hkt:%H:%M} HKT)."
            ),
        )

    if rate >= 0.1:
        target = (
            forecast_max
            if forecast_max is not None
            else max(running_max.temperature, latest.temperature) + 0.5
        )
        if target <= latest.temperature:
            target = latest.temperature + 0.3
        hours_to_peak = max(0.0, (target - latest.temperature) / rate)
        eta = latest.observed_at_hkt + timedelta(hours=hours_to_peak)

        # Clamp ETA into the typical HK peak window (13:00 – 16:30 HKT).
        day_floor = latest.observed_at_hkt.replace(
            hour=13, minute=0, second=0, microsecond=0
        )
        day_ceil = latest.observed_at_hkt.replace(
            hour=16, minute=30, second=0, microsecond=0
        )
        if eta > day_ceil:
            eta = day_ceil
        if now_hkt < day_floor and eta < day_floor:
            eta = day_floor

        confidence = "estimated" if forecast_max is not None else "forecast"
        return PeakEstimate(
            status="rising",
            temp=target,
            at_hkt=eta,
            rate_c_per_hour=rate,
            confidence=confidence,
            note=(
                f"Rising at {rate:+.2f}°C/hr. "
                f"Estimated peak ~{target:.1f}°C near {eta:%H:%M} HKT."
            ),
        )

    if rate <= -0.1:
        return PeakEstimate(
            status="falling",
            temp=running_max.temperature,
            at_hkt=running_max.observed_at_hkt,
            rate_c_per_hour=rate,
            confidence="estimated",
            note=(
                f"Trending down at {rate:+.2f}°C/hr. "
                f"Today's high so far {running_max.temperature:.1f}°C "
                f"at {running_max.observed_at_hkt:%H:%M} HKT."
            ),
        )

    return PeakEstimate(
        status="flat",
        temp=latest.temperature,
        at_hkt=latest.observed_at_hkt,
        rate_c_per_hour=rate,
        confidence="estimated",
        note=(
            f"Temperature is steady (≈{latest.temperature:.1f}°C). "
            f"Today's high so far {running_max.temperature:.1f}°C "
            f"at {running_max.observed_at_hkt:%H:%M} HKT."
        ),
    )
