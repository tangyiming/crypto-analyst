"""市场日程 Telegram 提醒轮询。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

from analyst.compute.market_schedule import (
    funding_lead_candidates,
    macro_lead_candidates,
    session_lead_candidates,
)
from analyst.config import get_settings

logger = logging.getLogger("uvicorn.error")

NotifyFn = Callable[[str], Awaitable[None]]
PremiumFn = Callable[[], dict[str, Any] | None]


def _parse_int_list(raw: str, default: list[int]) -> list[int]:
    if not (raw or "").strip():
        return list(default)
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = int(part)
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return out or list(default)


def _parse_set(raw: str, default: set[str]) -> set[str]:
    if not (raw or "").strip():
        return set(default)
    return {x.strip().upper() for x in raw.split(",") if x.strip()} or set(default)


class ScheduleReminderLoop:
    def __init__(
        self,
        *,
        notify: NotifyFn,
        get_premium: PremiumFn,
        funding_symbol: str = "BTC/USDT",
    ) -> None:
        self._notify = notify
        self._get_premium = get_premium
        self._funding_symbol = funding_symbol
        self._fired: set[str] = set()
        self._load()

    def _path(self) -> Path:
        s = get_settings()
        return Path(s.data_cache_dir) / "schedule_reminders.json"

    def _load(self) -> None:
        path = self._path()
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                keys = data.get("fired") or []
                if isinstance(keys, list):
                    self._fired = {str(k) for k in keys[-500:]}
        except Exception as e:
            logger.warning("load schedule reminders failed: %s", e)

    def _save(self) -> None:
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "fired": sorted(self._fired)[-500:],
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("save schedule reminders failed: %s", e)

    async def tick(self) -> int:
        settings = get_settings()
        if not getattr(settings, "monitor_schedule_enabled", True):
            return 0
        if not getattr(settings, "monitor_schedule_tg", True):
            return 0

        session_leads = _parse_int_list(
            getattr(settings, "monitor_schedule_session_leads", "30,15") or "",
            [30, 15],
        )
        funding_leads = _parse_int_list(
            getattr(settings, "monitor_schedule_funding_leads", "30") or "",
            [30],
        )
        macro_leads = _parse_int_list(
            getattr(settings, "monitor_schedule_macro_leads", "60,30,15") or "",
            [60, 30, 15],
        )
        currencies = _parse_set(
            getattr(settings, "monitor_schedule_macro_currencies", "USD") or "",
            {"USD"},
        )
        impacts = {
            x.capitalize()
            for x in _parse_set(
                getattr(settings, "monitor_schedule_macro_impacts", "High") or "",
                {"HIGH"},
            )
        }

        candidates: list[dict[str, Any]] = []
        candidates.extend(session_lead_candidates(session_leads))
        candidates.extend(
            funding_lead_candidates(
                self._get_premium(),
                funding_leads,
                symbol=self._funding_symbol,
            )
        )
        candidates.extend(
            macro_lead_candidates(
                macro_leads, currencies=currencies, impacts=impacts
            )
        )

        sent = 0
        for c in candidates:
            key = str(c.get("key") or "")
            if not key or key in self._fired:
                continue
            text = str(c.get("text") or "").strip()
            if not text:
                continue
            try:
                await self._notify(text)
            except Exception:
                logger.exception("schedule TG notify failed %s", key)
                continue
            self._fired.add(key)
            sent += 1
            logger.info("日程提醒已推送 %s", key)
        if sent:
            self._save()
        return sent


async def run_schedule_reminder_loop(
    *,
    notify: NotifyFn,
    get_premium: PremiumFn,
    interval_sec: float = 30.0,
) -> None:
    loop = ScheduleReminderLoop(notify=notify, get_premium=get_premium)
    while True:
        try:
            await loop.tick()
        except Exception:
            logger.exception("schedule reminder tick failed")
        await asyncio.sleep(interval_sec)
