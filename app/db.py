from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiosqlite

from .hko import Reading
from .polymarket import HkBuckets

HKT = ZoneInfo("Asia/Hong_Kong")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    bulletin_time     TEXT NOT NULL UNIQUE,
    observed_at_utc   TEXT NOT NULL,
    hkt_date          TEXT NOT NULL,
    temperature       REAL NOT NULL,
    rh                INTEGER,
    forecast_max      REAL,
    forecast_min      REAL,
    raw_json          TEXT,
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_readings_hkt_date   ON readings(hkt_date);
CREATE INDEX IF NOT EXISTS idx_readings_observed   ON readings(observed_at_utc);

CREATE TABLE IF NOT EXISTS notifications (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at_utc          TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    reading_id           INTEGER,
    date_key             TEXT,
    temperature          REAL,
    previous_temperature REAL,
    message              TEXT,
    FOREIGN KEY(reading_id) REFERENCES readings(id)
);
CREATE INDEX IF NOT EXISTS idx_notifications_kind_sent
    ON notifications(kind, sent_at_utc);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_daily_summary_unique
    ON notifications(date_key) WHERE kind = 'daily_summary' AND date_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS market_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at_utc  TEXT NOT NULL,
    hkt_date        TEXT NOT NULL,
    event_slug      TEXT NOT NULL,
    bucket_slug     TEXT NOT NULL,
    temp            INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    yes_price       REAL,
    no_price        REAL,
    volume          REAL
);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_date
    ON market_snapshots(hkt_date, fetched_at_utc);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_bucket
    ON market_snapshots(hkt_date, bucket_slug, fetched_at_utc);
"""


_READING_COLS = (
    "id, bulletin_time, observed_at_utc, hkt_date, "
    "temperature, rh, forecast_max, forecast_min"
)


@dataclass
class StoredReading:
    id: int
    bulletin_time: str
    observed_at_utc: datetime
    observed_at_hkt: datetime
    hkt_date: str
    temperature: float
    rh: Optional[int]
    forecast_max: Optional[float]
    forecast_min: Optional[float]


@dataclass
class TempChangeEvent:
    sent_at_utc: datetime
    sent_at_hkt: datetime
    bulletin_time: Optional[str]
    observed_at_hkt: Optional[datetime]
    temperature: float
    previous_temperature: Optional[float]


@dataclass
class DailySummary:
    hkt_date: str
    max_temp: float
    max_temp_at_hkt: datetime
    min_temp: float
    min_temp_at_hkt: datetime
    latest_temp: float
    latest_at_hkt: datetime
    forecast_max: Optional[float]
    forecast_min: Optional[float]
    count: int


def _to_stored(row: tuple) -> StoredReading:
    rid, bt, oau, hd, t, rh, fmax, fmin = row
    # aiosqlite returns ISO strings with "+00:00" — fromisoformat handles it in 3.11+
    dt_utc = datetime.fromisoformat(oau)
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return StoredReading(
        id=rid,
        bulletin_time=bt,
        observed_at_utc=dt_utc,
        observed_at_hkt=dt_utc.astimezone(HKT),
        hkt_date=hd,
        temperature=float(t),
        rh=int(rh) if rh is not None else None,
        forecast_max=float(fmax) if fmax is not None else None,
        forecast_min=float(fmin) if fmin is not None else None,
    )


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.execute("PRAGMA journal_mode=WAL;")
        await self.conn.execute("PRAGMA foreign_keys=ON;")
        await self.conn.executescript(_SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def upsert_reading(self, r: Reading) -> tuple[Optional[int], bool]:
        assert self.conn is not None
        cur = await self.conn.execute(
            """INSERT OR IGNORE INTO readings
               (bulletin_time, observed_at_utc, hkt_date, temperature,
                rh, forecast_max, forecast_min, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r.bulletin_time,
                r.observed_at_utc.isoformat(),
                r.hkt_date,
                r.temperature,
                r.rh,
                r.forecast_max,
                r.forecast_min,
                json.dumps(r.raw, ensure_ascii=False),
            ),
        )
        inserted = cur.rowcount == 1
        await cur.close()
        await self.conn.commit()

        cur = await self.conn.execute(
            "SELECT id FROM readings WHERE bulletin_time=?", (r.bulletin_time,)
        )
        row = await cur.fetchone()
        await cur.close()
        return (row[0] if row else None, inserted)

    async def latest_reading(self) -> Optional[StoredReading]:
        assert self.conn is not None
        cur = await self.conn.execute(
            f"SELECT {_READING_COLS} FROM readings "
            "ORDER BY bulletin_time DESC LIMIT 1"
        )
        row = await cur.fetchone()
        await cur.close()
        return _to_stored(row) if row else None

    async def previous_reading(
        self, before_bulletin_time: str
    ) -> Optional[StoredReading]:
        assert self.conn is not None
        cur = await self.conn.execute(
            f"SELECT {_READING_COLS} FROM readings "
            "WHERE bulletin_time < ? ORDER BY bulletin_time DESC LIMIT 1",
            (before_bulletin_time,),
        )
        row = await cur.fetchone()
        await cur.close()
        return _to_stored(row) if row else None

    async def readings_for_date(self, hkt_date: str) -> list[StoredReading]:
        assert self.conn is not None
        cur = await self.conn.execute(
            f"SELECT {_READING_COLS} FROM readings "
            "WHERE hkt_date = ? ORDER BY bulletin_time ASC",
            (hkt_date,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_to_stored(r) for r in rows]

    async def daily_summary(self, hkt_date: str) -> Optional[DailySummary]:
        readings = await self.readings_for_date(hkt_date)
        if not readings:
            return None
        max_r = max(readings, key=lambda r: r.temperature)
        min_r = min(readings, key=lambda r: r.temperature)
        latest = readings[-1]
        return DailySummary(
            hkt_date=hkt_date,
            max_temp=max_r.temperature,
            max_temp_at_hkt=max_r.observed_at_hkt,
            min_temp=min_r.temperature,
            min_temp_at_hkt=min_r.observed_at_hkt,
            latest_temp=latest.temperature,
            latest_at_hkt=latest.observed_at_hkt,
            forecast_max=latest.forecast_max,
            forecast_min=latest.forecast_min,
            count=len(readings),
        )

    async def total_readings(self) -> int:
        assert self.conn is not None
        cur = await self.conn.execute("SELECT COUNT(*) FROM readings")
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row else 0

    async def days_tracked(self) -> int:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT COUNT(DISTINCT hkt_date) FROM readings"
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row else 0

    async def first_hkt_date(self) -> Optional[str]:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT hkt_date FROM readings ORDER BY bulletin_time ASC LIMIT 1"
        )
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None

    async def total_notifications(self) -> int:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE kind='temp_change'"
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row else 0

    async def record_notification(
        self,
        *,
        kind: str,
        reading_id: Optional[int],
        temperature: Optional[float],
        prev_temperature: Optional[float],
        message: str,
        date_key: Optional[str],
    ) -> None:
        assert self.conn is not None
        await self.conn.execute(
            """INSERT OR IGNORE INTO notifications
               (sent_at_utc, kind, reading_id, date_key,
                temperature, previous_temperature, message)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                kind,
                reading_id,
                date_key,
                temperature,
                prev_temperature,
                message,
            ),
        )
        await self.conn.commit()

    async def daily_summary_sent(self, hkt_date: str) -> bool:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT 1 FROM notifications "
            "WHERE kind='daily_summary' AND date_key=? LIMIT 1",
            (hkt_date,),
        )
        row = await cur.fetchone()
        await cur.close()
        return row is not None

    async def record_market_snapshot(
        self, hkt_date: str, buckets: HkBuckets
    ) -> None:
        assert self.conn is not None
        if not buckets.buckets:
            return
        now_utc = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                now_utc, hkt_date, buckets.event_slug, b.slug,
                b.temp, b.kind, b.yes_price, b.no_price, b.volume,
            )
            for b in buckets.buckets
        ]
        await self.conn.executemany(
            """INSERT INTO market_snapshots
               (fetched_at_utc, hkt_date, event_slug, bucket_slug,
                temp, kind, yes_price, no_price, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        await self.conn.commit()

    async def recent_temp_changes(self, limit: int = 15) -> list[TempChangeEvent]:
        assert self.conn is not None
        cur = await self.conn.execute(
            """SELECT n.sent_at_utc, n.temperature, n.previous_temperature,
                      r.bulletin_time, r.observed_at_utc
               FROM notifications n
               LEFT JOIN readings r ON r.id = n.reading_id
               WHERE n.kind = 'temp_change'
               ORDER BY n.sent_at_utc DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cur.fetchall()
        await cur.close()
        events: list[TempChangeEvent] = []
        for sent_at, temp, prev_temp, bt, oau in rows:
            sent_dt = datetime.fromisoformat(sent_at)
            if sent_dt.tzinfo is None:
                sent_dt = sent_dt.replace(tzinfo=timezone.utc)
            observed_hkt: Optional[datetime] = None
            if oau:
                o = datetime.fromisoformat(oau)
                if o.tzinfo is None:
                    o = o.replace(tzinfo=timezone.utc)
                observed_hkt = o.astimezone(HKT)
            events.append(
                TempChangeEvent(
                    sent_at_utc=sent_dt,
                    sent_at_hkt=sent_dt.astimezone(HKT),
                    bulletin_time=bt,
                    observed_at_hkt=observed_hkt,
                    temperature=float(temp) if temp is not None else 0.0,
                    previous_temperature=(
                        float(prev_temp) if prev_temp is not None else None
                    ),
                )
            )
        return events

    async def total_market_snapshots(self) -> int:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM market_snapshots"
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row else 0
