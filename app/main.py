from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta
from html import escape
from typing import Optional
from zoneinfo import ZoneInfo

from .config import Config, load_config
from .db import Database, DailySummary, StoredReading, TempChangeEvent
from .hko import HkoClient, Reading
from .polymarket import HkBuckets, PolymarketClient
from .predictor import PeakEstimate, estimate_peak
from .telegram_bot import TelegramBot

HKT = ZoneInfo("Asia/Hong_Kong")
logger = logging.getLogger(__name__)

BOT_COMMANDS: list[tuple[str, str]] = [
    ("status",   "Current HKO temp + today's summary + Polymarket buckets"),
    ("today",    "Today's high / low / peak estimate"),
    ("history",  "Recent temperature changes from DB (/history [N])"),
    ("markets",  "Polymarket buckets for today"),
    ("forecast", "HKO 9-day forecast"),
    ("stats",    "Storage & notification stats"),
    ("help",     "Show command help"),
]

HELP_TEXT = (
    "<b>Polymarket HK Weather Tracker</b>\n"
    "Tracks Hong Kong Observatory temperature and alerts on every change.\n\n"
    "Commands:\n"
    "/status      — current temperature + today's summary\n"
    "/today       — today's high / low / peak estimate\n"
    "/history [N] — last N (default 15, max 50) temperature changes\n"
    "/markets     — Polymarket buckets for today\n"
    "/forecast    — HKO 9-day forecast\n"
    "/stats       — storage &amp; notification stats\n"
    "/help        — this message"
)


def _fmt_delta(current: float, prev: Optional[float]) -> str:
    if prev is None:
        return ""
    d = current - prev
    if d > 0:
        return f" (↑ +{d:.1f}°C)"
    if d < 0:
        return f" (↓ {d:.1f}°C)"
    return ""


def _fmt_forecast_range(fmin: Optional[float], fmax: Optional[float]) -> str:
    lo = f"{fmin:.0f}" if fmin is not None else "–"
    hi = f"{fmax:.0f}" if fmax is not None else "–"
    return f"{lo}–{hi}°C"


def format_polymarket_block(
    buckets: Optional[HkBuckets],
    running_max: Optional[float],
) -> list[str]:
    """Return a list of lines representing Polymarket buckets, or [] if unavailable."""
    if buckets is None or not buckets.buckets:
        return []
    marker_bucket = (
        buckets.containing(running_max) if running_max is not None else None
    )
    top = buckets.top()
    label_width = max(len(b.label) for b in buckets.buckets)
    rows = []
    for b in buckets.buckets:
        pct = b.yes_price * 100.0
        marker = ""
        if marker_bucket is not None and b.slug == marker_bucket.slug:
            marker = "  ← now"
        elif top is not None and b.slug == top.slug:
            marker = "  ★"
        rows.append(f"{b.label.rjust(label_width)}  {pct:5.1f}%{marker}")

    vol = buckets.total_volume
    vol_str = (
        f"${vol / 1_000_000:.2f}M" if vol >= 1_000_000
        else f"${vol / 1_000:.1f}k" if vol >= 1_000
        else f"${vol:.0f}"
    )
    header = f"🎯 Polymarket buckets · vol {vol_str}"
    return ["", header, "<pre>" + "\n".join(rows) + "</pre>"]


def format_change_message(
    reading: Reading,
    prev_temp: Optional[float],
    summary: DailySummary,
    pred: PeakEstimate,
    buckets: Optional[HkBuckets] = None,
) -> str:
    delta = _fmt_delta(reading.temperature, prev_temp)
    lines = [
        f"<b>🌡 HK Temp: {reading.temperature:.1f}°C</b>{delta}",
        f"<i>{reading.observed_at_hkt:%Y-%m-%d %H:%M} HKT · bulletin {reading.bulletin_time}</i>",
        "",
        "<b>Today so far</b>",
        f"  ▲ High: {summary.max_temp:.1f}°C @ {summary.max_temp_at_hkt:%H:%M}",
        f"  ▼ Low:  {summary.min_temp:.1f}°C @ {summary.min_temp_at_hkt:%H:%M}",
        f"  Forecast: {_fmt_forecast_range(summary.forecast_min, summary.forecast_max)} (HKO)",
        "",
        f"<b>Peak estimate</b> [{pred.confidence}]",
        f"  {escape(pred.note)}",
    ]
    if reading.rh is not None:
        lines.append(f"\nHumidity: {reading.rh}%")
    lines += format_polymarket_block(buckets, running_max=summary.max_temp)
    return "\n".join(lines)


def format_today_message(
    summary: DailySummary,
    pred: PeakEstimate,
    buckets: Optional[HkBuckets] = None,
) -> str:
    lines = [
        f"<b>📅 {summary.hkt_date} (HKT)</b>",
        "",
        f"Latest: {summary.latest_temp:.1f}°C @ {summary.latest_at_hkt:%H:%M}",
        f"High:   {summary.max_temp:.1f}°C @ {summary.max_temp_at_hkt:%H:%M}",
        f"Low:    {summary.min_temp:.1f}°C @ {summary.min_temp_at_hkt:%H:%M}",
        f"Forecast: {_fmt_forecast_range(summary.forecast_min, summary.forecast_max)}",
        f"Readings stored: {summary.count}",
        "",
        f"<b>Peak estimate</b> [{pred.confidence}]",
        f"  {escape(pred.note)}",
    ]
    lines += format_polymarket_block(buckets, running_max=summary.max_temp)
    return "\n".join(lines)


def format_history_message(events: list[TempChangeEvent]) -> str:
    if not events:
        return "No temperature changes recorded yet."
    # Decide if date prefix is needed (mixed dates → show date)
    dates = {(e.observed_at_hkt or e.sent_at_hkt).date() for e in events}
    show_date = len(dates) > 1

    # Compute column widths for neat monospace alignment
    prev_strs: list[str] = []
    cur_strs: list[str] = []
    delta_strs: list[str] = []
    time_strs: list[str] = []
    for e in events:
        t = e.observed_at_hkt or e.sent_at_hkt
        time_strs.append(
            t.strftime("%m-%d %H:%M") if show_date else t.strftime("%H:%M")
        )
        cur_strs.append(f"{e.temperature:4.1f}°C")
        if e.previous_temperature is not None:
            prev_strs.append(f"{e.previous_temperature:4.1f}°C")
            d = e.temperature - e.previous_temperature
            arrow = "↑" if d > 0 else "↓" if d < 0 else "·"
            delta_strs.append(f"{arrow} {d:+.1f}")
        else:
            prev_strs.append("     —")
            delta_strs.append("init")

    tw = max(len(s) for s in time_strs)
    pw = max(len(s) for s in prev_strs)
    cw = max(len(s) for s in cur_strs)
    dw = max(len(s) for s in delta_strs)

    rows = []
    for tstr, pstr, cstr, dstr in zip(time_strs, prev_strs, cur_strs, delta_strs):
        rows.append(
            f"{tstr.ljust(tw)}  {pstr.rjust(pw)} → {cstr.rjust(cw)}  {dstr.rjust(dw)}"
        )

    header = f"<b>🕓 Recent temperature changes ({len(events)})</b>"
    return header + "\n<pre>" + "\n".join(rows) + "</pre>"


def format_markets_message(buckets: Optional[HkBuckets], running_max: Optional[float]) -> str:
    if buckets is None:
        return "No Polymarket HK event found for today."
    if not buckets.buckets:
        return f"Event <b>{escape(buckets.event_slug)}</b> has no buckets."
    block = format_polymarket_block(buckets, running_max=running_max)
    header = (
        f"<b>🎯 {escape(buckets.event_title or buckets.event_slug)}</b>\n"
        f"<i>{escape(buckets.event_slug)}</i>"
    )
    return header + "\n" + "\n".join(block)


def format_forecast_message(reading: Reading) -> str:
    f9d = (reading.raw.get("F9D") or {}).get("WeatherForecast") or []
    if not f9d:
        return "No 9-day forecast available."
    lines = ["<b>🗓 HKO 9-day Forecast</b>", ""]
    for item in f9d:
        d = str(item.get("ForecastDate", ""))
        pretty = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
        tmax = item.get("ForecastMaxtemp", "–")
        tmin = item.get("ForecastMintemp", "–")
        icon = escape(str(item.get("IconDesc", "") or ""))
        lines.append(f"<b>{pretty}</b>  {tmin}–{tmax}°C  <i>{icon}</i>")
    return "\n".join(lines)


def format_daily_summary_message(summary: DailySummary) -> str:
    lines = [
        f"<b>🌙 Daily summary — {summary.hkt_date} (HKT)</b>",
        "",
        f"High: {summary.max_temp:.1f}°C @ {summary.max_temp_at_hkt:%H:%M}",
        f"Low:  {summary.min_temp:.1f}°C @ {summary.min_temp_at_hkt:%H:%M}",
        f"Close: {summary.latest_temp:.1f}°C @ {summary.latest_at_hkt:%H:%M}",
        f"Readings: {summary.count}",
        f"HKO forecast was: {_fmt_forecast_range(summary.forecast_min, summary.forecast_max)}",
    ]
    return "\n".join(lines)


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.hko = HkoClient(cfg.hko_url)
        self.bot = TelegramBot(cfg.telegram_bot_token, cfg.telegram_chat_id)
        self.poly: Optional[PolymarketClient] = (
            PolymarketClient(
                cfg.polymarket_gamma_url,
                cfg.polymarket_event_slug_override or None,
            )
            if cfg.polymarket_enabled
            else None
        )
        self.stopping = asyncio.Event()

    async def start(self) -> None:
        await self.db.open()
        await self.hko.start()
        await self.bot.start()
        await self.bot.set_commands(BOT_COMMANDS)
        if self.poly is not None:
            await self.poly.start()
        logger.info(
            "started; poll=%ss threshold=%.2f db=%s polymarket=%s",
            self.cfg.poll_interval_seconds,
            self.cfg.temp_change_threshold,
            self.cfg.db_path,
            "on" if self.poly is not None else "off",
        )

    async def stop(self) -> None:
        self.stopping.set()
        await self.hko.stop()
        await self.bot.stop()
        if self.poly is not None:
            await self.poly.stop()
        await self.db.close()

    async def _fetch_buckets(self, hkt_date: str) -> Optional[HkBuckets]:
        if self.poly is None:
            return None
        try:
            d = datetime.strptime(hkt_date, "%Y-%m-%d").date()
            return await self.poly.fetch_hk_buckets(d)
        except Exception:
            logger.exception("polymarket fetch failed")
            return None

    async def run(self) -> None:
        await self.bot.send("🟢 <b>HK weather tracker online.</b>\n\n" + HELP_TEXT)
        await asyncio.gather(
            self._poll_loop(),
            self._command_loop(),
            self._daily_summary_loop(),
        )

    async def _sleep_interruptible(self, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self.stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _poll_loop(self) -> None:
        latest = await self.db.latest_reading()
        last_notified_temp: Optional[float] = latest.temperature if latest else None
        last_seen_bulletin: Optional[str] = latest.bulletin_time if latest else None

        while not self.stopping.is_set():
            try:
                reading = await self.hko.fetch()
                row_id, inserted = await self.db.upsert_reading(reading)

                if reading.bulletin_time != last_seen_bulletin:
                    if inserted:
                        logger.info(
                            "new bulletin %s temp=%.1f",
                            reading.bulletin_time,
                            reading.temperature,
                        )

                    if last_notified_temp is not None:
                        delta = abs(reading.temperature - last_notified_temp)
                        threshold = self.cfg.temp_change_threshold
                        # HKO resolution is 0.1°C; any change of >=0.05 is real.
                        meaningful = (
                            delta > threshold if threshold > 0 else delta >= 0.05
                        )
                        if meaningful:
                            await self._notify_change(
                                reading, last_notified_temp, row_id
                            )
                    last_notified_temp = reading.temperature
                    last_seen_bulletin = reading.bulletin_time

            except Exception:
                logger.exception("poll iteration failed")

            await self._sleep_interruptible(self.cfg.poll_interval_seconds)

    async def _notify_change(
        self,
        reading: Reading,
        prev_temp: float,
        row_id: Optional[int],
    ) -> None:
        readings_today = await self.db.readings_for_date(reading.hkt_date)
        summary = await self.db.daily_summary(reading.hkt_date)
        if summary is None:
            summary = self._summary_from_reading(reading)
        pred = estimate_peak(readings_today, reading.forecast_max)
        buckets = await self._fetch_buckets(reading.hkt_date)
        if buckets is not None:
            try:
                await self.db.record_market_snapshot(reading.hkt_date, buckets)
            except Exception:
                logger.exception("record_market_snapshot failed")
        msg = format_change_message(reading, prev_temp, summary, pred, buckets)
        await self.bot.send(msg)
        await self.db.record_notification(
            kind="temp_change",
            reading_id=row_id,
            temperature=reading.temperature,
            prev_temperature=prev_temp,
            message=msg,
            date_key=reading.hkt_date,
        )
        logger.info(
            "notified change %.1f → %.1f (Δ %+.1f)",
            prev_temp,
            reading.temperature,
            reading.temperature - prev_temp,
        )

    def _summary_from_reading(self, reading: Reading) -> DailySummary:
        return DailySummary(
            hkt_date=reading.hkt_date,
            max_temp=reading.temperature,
            max_temp_at_hkt=reading.observed_at_hkt,
            min_temp=reading.temperature,
            min_temp_at_hkt=reading.observed_at_hkt,
            latest_temp=reading.temperature,
            latest_at_hkt=reading.observed_at_hkt,
            forecast_max=reading.forecast_max,
            forecast_min=reading.forecast_min,
            count=0,
        )

    # ---- Commands --------------------------------------------------------

    async def _command_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                updates = await self.bot.get_updates(timeout=30)
                for u in updates:
                    self.bot.offset = u["update_id"] + 1
                    message = u.get("message") or {}
                    chat = message.get("chat") or {}
                    chat_id = str(chat.get("id", ""))
                    text = (message.get("text") or "").strip()
                    if not text.startswith("/"):
                        continue
                    if chat_id != self.cfg.telegram_chat_id:
                        logger.info("ignoring command from chat_id=%s", chat_id)
                        continue
                    try:
                        await self._handle_command(text)
                    except Exception:
                        logger.exception("command handler failed: %s", text)
                        await self.bot.send(f"⚠️ Error handling {escape(text)}")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("command loop error")
                await asyncio.sleep(5)

    async def _handle_command(self, text: str) -> None:
        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/start", "/help"):
            await self.bot.send(HELP_TEXT)
        elif cmd == "/status":
            await self._cmd_status()
        elif cmd == "/today":
            await self._cmd_today()
        elif cmd == "/history":
            parts = text.split()
            limit = 15
            if len(parts) > 1:
                try:
                    limit = int(parts[1])
                except ValueError:
                    pass
            limit = max(1, min(limit, 50))
            await self._cmd_history(limit)
        elif cmd == "/markets" or cmd == "/market":
            await self._cmd_markets()
        elif cmd == "/forecast":
            await self._cmd_forecast()
        elif cmd == "/stats":
            await self._cmd_stats()
        else:
            await self.bot.send(f"Unknown command: <code>{escape(cmd)}</code>\n\n" + HELP_TEXT)

    async def _cmd_status(self) -> None:
        try:
            reading = await self.hko.fetch()
        except Exception as e:
            await self.bot.send(f"❌ HKO fetch failed: {escape(str(e))}")
            return
        prev = await self.db.previous_reading(reading.bulletin_time)
        readings_today = await self.db.readings_for_date(reading.hkt_date)
        summary = await self.db.daily_summary(reading.hkt_date)
        if summary is None:
            summary = self._summary_from_reading(reading)
        pred = estimate_peak(readings_today, reading.forecast_max)
        buckets = await self._fetch_buckets(reading.hkt_date)
        await self.bot.send(
            format_change_message(
                reading,
                prev.temperature if prev else None,
                summary,
                pred,
                buckets,
            )
        )

    async def _cmd_today(self) -> None:
        hkt_date = datetime.now(HKT).strftime("%Y-%m-%d")
        summary = await self.db.daily_summary(hkt_date)
        if summary is None:
            await self.bot.send(f"No readings stored yet for {hkt_date}.")
            return
        readings = await self.db.readings_for_date(hkt_date)
        pred = estimate_peak(readings, summary.forecast_max)
        buckets = await self._fetch_buckets(hkt_date)
        await self.bot.send(format_today_message(summary, pred, buckets))

    async def _cmd_history(self, limit: int) -> None:
        events = await self.db.recent_temp_changes(limit)
        await self.bot.send(format_history_message(events))

    async def _cmd_markets(self) -> None:
        if self.poly is None:
            await self.bot.send("Polymarket integration is disabled.")
            return
        hkt_date = datetime.now(HKT).strftime("%Y-%m-%d")
        buckets = await self._fetch_buckets(hkt_date)
        summary = await self.db.daily_summary(hkt_date)
        running_max = summary.max_temp if summary else None
        await self.bot.send(format_markets_message(buckets, running_max))

    async def _cmd_forecast(self) -> None:
        try:
            reading = await self.hko.fetch()
        except Exception as e:
            await self.bot.send(f"❌ HKO fetch failed: {escape(str(e))}")
            return
        await self.bot.send(format_forecast_message(reading))

    async def _cmd_stats(self) -> None:
        total = await self.db.total_readings()
        days = await self.db.days_tracked()
        first = await self.db.first_hkt_date()
        notif = await self.db.total_notifications()
        snaps = await self.db.total_market_snapshots()
        lines = [
            "<b>📊 Storage stats</b>",
            f"Readings stored:   {total}",
            f"Days tracked:      {days}",
            f"Change alerts:     {notif}",
            f"Market snapshots:  {snaps}",
        ]
        if first:
            lines.append(f"Tracking since:    {first}")
        lines.append(f"DB path:           <code>{escape(self.cfg.db_path)}</code>")
        await self.bot.send("\n".join(lines))

    # ---- Daily summary ---------------------------------------------------

    async def _daily_summary_loop(self) -> None:
        try:
            hh, mm = (int(x) for x in self.cfg.notify_daily_summary_hkt.split(":"))
        except Exception:
            logger.error(
                "invalid NOTIFY_DAILY_SUMMARY_HKT=%s; disabling daily summary",
                self.cfg.notify_daily_summary_hkt,
            )
            return

        while not self.stopping.is_set():
            now = datetime.now(HKT)
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            logger.debug("daily summary fires in %.0fs at %s", wait_seconds, target)
            await self._sleep_interruptible(wait_seconds)
            if self.stopping.is_set():
                return

            try:
                hkt_date = datetime.now(HKT).strftime("%Y-%m-%d")
                if await self.db.daily_summary_sent(hkt_date):
                    logger.info("daily summary for %s already sent", hkt_date)
                    continue
                summary = await self.db.daily_summary(hkt_date)
                if summary is None:
                    logger.info("no readings to summarize for %s", hkt_date)
                    continue
                msg = format_daily_summary_message(summary)
                await self.bot.send(msg)
                await self.db.record_notification(
                    kind="daily_summary",
                    reading_id=None,
                    temperature=summary.max_temp,
                    prev_temperature=summary.min_temp,
                    message=msg,
                    date_key=hkt_date,
                )
            except Exception:
                logger.exception("daily summary error")


async def amain() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = App(cfg)
    await app.start()

    loop = asyncio.get_running_loop()

    def _stop() -> None:
        logger.info("shutdown signal received")
        app.stopping.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    try:
        await app.run()
    finally:
        await app.stop()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
