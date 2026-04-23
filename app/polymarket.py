from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

_KIND_ORDER = {"or_below": 0, "exact": 1, "or_higher": 2}


@dataclass
class Bucket:
    label: str          # '≤18°C' | '23°C' | '≥28°C'
    temp: int           # threshold integer (18, 23, 28)
    kind: str           # 'or_below' | 'exact' | 'or_higher'
    yes_price: float    # implied probability (0..1)
    no_price: float
    volume: float
    slug: str


@dataclass
class HkBuckets:
    event_slug: str
    event_title: str
    end_date_iso: str
    total_volume: float
    buckets: list[Bucket]

    def top(self) -> Optional[Bucket]:
        return max(self.buckets, key=lambda b: b.yes_price) if self.buckets else None

    def containing(self, value: float) -> Optional[Bucket]:
        if not self.buckets:
            return None
        rounded = int(round(value))
        for b in self.buckets:
            if b.kind == "exact" and b.temp == rounded:
                return b
        for b in self.buckets:
            if b.kind == "or_below" and rounded <= b.temp:
                return b
        for b in self.buckets:
            if b.kind == "or_higher" and rounded >= b.temp:
                return b
        return None


def event_slug_for(hkt_today: date) -> str:
    m = _MONTHS[hkt_today.month - 1]
    return f"highest-temperature-in-hong-kong-on-{m}-{hkt_today.day}-{hkt_today.year}"


def _parse_prices(v) -> list[float]:
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return []
    try:
        return [float(x) for x in v]
    except (TypeError, ValueError):
        return []


def _parse_outcomes(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return []
    return [str(x) for x in v]


def _parse_bucket(market: dict) -> Optional[Bucket]:
    slug = market.get("slug") or ""
    if not slug:
        return None
    tail = slug.rsplit("-", 1)[-1].lower()

    kind: str
    num_str: str
    if tail.endswith("corbelow"):
        kind = "or_below"
        num_str = tail[: -len("corbelow")]
    elif tail.endswith("corhigher"):
        kind = "or_higher"
        num_str = tail[: -len("corhigher")]
    elif tail.endswith("c"):
        kind = "exact"
        num_str = tail[:-1]
    else:
        return None

    try:
        temp = int(num_str)
    except ValueError:
        return None

    outcomes = _parse_outcomes(market.get("outcomes"))
    prices = _parse_prices(market.get("outcomePrices"))
    if len(outcomes) != len(prices) or not prices:
        return None

    yes = no = None
    for o, p in zip(outcomes, prices):
        if o.lower() == "yes":
            yes = p
        elif o.lower() == "no":
            no = p
    if yes is None:
        return None
    if no is None:
        no = max(0.0, 1.0 - yes)

    if kind == "or_below":
        label = f"≤{temp}°C"
    elif kind == "or_higher":
        label = f"≥{temp}°C"
    else:
        label = f"{temp}°C"

    try:
        vol = float(market.get("volume") or 0.0)
    except (TypeError, ValueError):
        vol = 0.0

    return Bucket(
        label=label, temp=temp, kind=kind,
        yes_price=yes, no_price=no, volume=vol, slug=slug,
    )


class PolymarketClient:
    def __init__(self, gamma_url: str, event_slug_override: Optional[str] = None):
        self.base = gamma_url.rstrip("/")
        self.override = (event_slug_override or "").strip() or None
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20)
        )

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def slug_for(self, hkt_today: date) -> str:
        return self.override or event_slug_for(hkt_today)

    async def fetch_hk_buckets(self, hkt_today: date) -> Optional[HkBuckets]:
        if self._session is None:
            raise RuntimeError("PolymarketClient.start() was not called")
        slug = self.slug_for(hkt_today)
        try:
            async with self._session.get(
                f"{self.base}/events", params={"slug": slug}
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception:
            logger.exception("Polymarket fetch failed for %s", slug)
            return None

        ev = None
        if isinstance(data, list) and data:
            ev = data[0]
        elif isinstance(data, dict):
            ev = data
        if not ev:
            return None

        markets = ev.get("markets") or []
        buckets: list[Bucket] = []
        for m in markets:
            b = _parse_bucket(m)
            if b is not None:
                buckets.append(b)
        buckets.sort(key=lambda b: (_KIND_ORDER[b.kind], b.temp))

        try:
            vol = float(ev.get("volume") or 0.0)
        except (TypeError, ValueError):
            vol = 0.0

        return HkBuckets(
            event_slug=ev.get("slug") or slug,
            event_title=ev.get("title") or "",
            end_date_iso=ev.get("endDate") or "",
            total_volume=vol,
            buckets=buckets,
        )
