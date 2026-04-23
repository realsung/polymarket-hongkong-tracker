from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import aiohttp

logger = logging.getLogger(__name__)

HKT = ZoneInfo("Asia/Hong_Kong")

_HEADERS = {
    "sec-ch-ua-platform": '"Android"',
    "Referer": "https://www.weather.gov.hk/en/cis/climat.htm",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 "
        "Mobile Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?1",
}


@dataclass
class Reading:
    bulletin_time: str
    observed_at_utc: datetime
    observed_at_hkt: datetime
    hkt_date: str
    temperature: float
    rh: Optional[int]
    forecast_max: Optional[float]
    forecast_min: Optional[float]
    raw: dict[str, Any]


def _parse_bulletin_time(s: str) -> datetime:
    return datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=HKT)


def _maybe_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _maybe_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_reading(data: dict[str, Any]) -> Reading:
    hko = data.get("hko") or {}
    bulletin = str(hko.get("BulletinTime") or "").strip()
    temp = _maybe_float(hko.get("Temperature"))
    if not bulletin or temp is None:
        raise ValueError("Missing hko.BulletinTime or hko.Temperature in HKO response")

    observed_hkt = _parse_bulletin_time(bulletin)

    fmax = _maybe_float(hko.get("HomeMaxTemperature"))
    fmin = _maybe_float(hko.get("HomeMinTemperature"))

    # Fallback: F9D today
    if fmax is None or fmin is None:
        f9d = (data.get("F9D") or {}).get("WeatherForecast") or []
        if f9d:
            today_fc = f9d[0] or {}
            if fmax is None:
                fmax = _maybe_float(today_fc.get("ForecastMaxtemp"))
            if fmin is None:
                fmin = _maybe_float(today_fc.get("ForecastMintemp"))

    return Reading(
        bulletin_time=bulletin,
        observed_at_utc=observed_hkt.astimezone(timezone.utc),
        observed_at_hkt=observed_hkt,
        hkt_date=observed_hkt.strftime("%Y-%m-%d"),
        temperature=temp,
        rh=_maybe_int(hko.get("RH")),
        forecast_max=fmax,
        forecast_min=fmin,
        raw=data,
    )


class HkoClient:
    def __init__(self, url: str):
        self.url = url
        self._session: Optional[aiohttp.ClientSession] = None
        self._etag: Optional[str] = None
        self._last_modified: Optional[str] = None
        self._cached_reading: Optional[Reading] = None
        self.stats_200 = 0
        self.stats_304 = 0

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers=_HEADERS,
        )

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch(self) -> Reading:
        """Fetch latest reading. Uses If-None-Match / If-Modified-Since to
        short-circuit with 304 when HKO has not published a new bulletin yet.
        On 304 returns the previously parsed Reading unchanged."""
        if self._session is None:
            raise RuntimeError("HkoClient.start() was not called")

        cond_headers: dict[str, str] = {}
        if self._etag:
            cond_headers["If-None-Match"] = self._etag
        if self._last_modified:
            cond_headers["If-Modified-Since"] = self._last_modified

        async with self._session.get(self.url, headers=cond_headers) as resp:
            if resp.status == 304 and self._cached_reading is not None:
                self.stats_304 += 1
                logger.debug("HKO 304 Not Modified (etag=%s)", self._etag)
                return self._cached_reading
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            etag = resp.headers.get("ETag")
            lm = resp.headers.get("Last-Modified")

        reading = parse_reading(data)
        self._etag = etag
        self._last_modified = lm
        self._cached_reading = reading
        self.stats_200 += 1
        return reading
