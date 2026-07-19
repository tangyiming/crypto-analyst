"""每日日报定时推送循环（UTC 每日一条，落盘去重防重启重发）。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from analyst.config import get_settings

logger = logging.getLogger(__name__)


def _state_path() -> Path:
    s = get_settings()
    return Path(s.data_cache_dir) / "daily_digest_state.json"


def _last_sent_day() -> str:
    try:
        p = _state_path()
        if p.is_file():
            return str(json.loads(p.read_text()).get("last_day") or "")
    except Exception:
        pass
    return ""


def _mark_sent(day: str) -> None:
    try:
        _state_path().write_text(json.dumps({"last_day": day}))
    except Exception:
        logger.exception("digest state save failed")


async def run_daily_digest_loop(
    notify: Callable[[str], Awaitable[None]],
    *,
    check_seconds: int = 300,
) -> None:
    """每 check_seconds 检查一次：到点（UTC 小时）且今天没发过 → 生成并推送。"""
    while True:
        try:
            settings = get_settings()
            if getattr(settings, "monitor_digest_enabled", True):
                hour = int(getattr(settings, "monitor_digest_utc_hour", 5) or 5)
                now = datetime.now(timezone.utc)
                today = now.strftime("%Y-%m-%d")
                if now.hour >= hour and _last_sent_day() != today:
                    _mark_sent(today)  # 先占位，防生成期间并发重发
                    from analyst.llm.digest import compose_daily_digest

                    out = await asyncio.to_thread(compose_daily_digest)
                    text = out.get("text") or ""
                    if text:
                        await notify(text)
                        logger.info(
                            "每日日报已推送 source=%s model=%s",
                            out.get("source"), out.get("model", "-"),
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("daily digest loop error")
        await asyncio.sleep(check_seconds)
