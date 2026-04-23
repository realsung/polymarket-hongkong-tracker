from __future__ import annotations

import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self._base = f"https://api.telegram.org/bot{token}"
        self._session: Optional[aiohttp.ClientSession] = None
        self.offset: int = 0

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=70)
        )

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        """Register command autocomplete with Telegram (shows on '/' typed)."""
        if self._session is None:
            return
        payload = {
            "commands": [
                {"command": c.lstrip("/"), "description": d} for c, d in commands
            ]
        }
        try:
            async with self._session.post(
                f"{self._base}/setMyCommands", json=payload
            ) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning("setMyCommands not ok: %s", data)
        except Exception:
            logger.exception("setMyCommands failed")

    async def send(self, text: str, parse_mode: str = "HTML") -> Optional[dict]:
        if self._session is None:
            return None
        try:
            async with self._session.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            ) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning("Telegram sendMessage not ok: %s", data)
                return data
        except Exception:
            logger.exception("Telegram sendMessage failed")
            return None

    async def get_updates(self, timeout: int = 30) -> list[dict]:
        if self._session is None:
            return []
        try:
            async with self._session.get(
                f"{self._base}/getUpdates",
                params={
                    "offset": self.offset,
                    "timeout": timeout,
                    "allowed_updates": '["message"]',
                },
                timeout=aiohttp.ClientTimeout(total=timeout + 15),
            ) as resp:
                data = await resp.json()
                return data.get("result", []) if data.get("ok") else []
        except Exception:
            logger.exception("Telegram getUpdates failed")
            return []
