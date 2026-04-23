from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    poll_interval_seconds: int
    temp_change_threshold: float
    db_path: str
    log_level: str
    hko_url: str
    notify_daily_summary_hkt: str
    polymarket_enabled: bool
    polymarket_gamma_url: str
    polymarket_event_slug_override: str


def _require(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        raise SystemExit(f"Missing required env var: {key}")
    return v


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def load_config() -> Config:
    return Config(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_require("TELEGRAM_CHAT_ID"),
        poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "10")),
        temp_change_threshold=float(os.environ.get("TEMP_CHANGE_THRESHOLD", "0.0")),
        db_path=os.environ.get("DB_PATH", "/data/hko.db"),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        hko_url=os.environ.get(
            "HKO_URL",
            "https://www.weather.gov.hk/wxinfo/json/one_json.xml",
        ),
        notify_daily_summary_hkt=os.environ.get("NOTIFY_DAILY_SUMMARY_HKT", "21:00"),
        polymarket_enabled=_bool("POLYMARKET_ENABLED", True),
        polymarket_gamma_url=os.environ.get(
            "POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com"
        ),
        polymarket_event_slug_override=os.environ.get(
            "POLYMARKET_EVENT_SLUG", ""
        ).strip(),
    )
