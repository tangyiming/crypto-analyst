"""DTO 转换 - 把数据库实体转成前端友好的 dict。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from analyst.config import get_settings
from analyst.storage import repo
from analyst.storage.models import AIPlan, Session, Verification


def _iso_utc(dt: datetime | None) -> str | None:
    """将 naive UTC datetime 输出为带 Z 后缀的 ISO8601 字符串。

    数据库里 created_at 等字段用 datetime.utcnow() 存的是 naive UTC，
    直接 isoformat() 没有时区后缀，前端 new Date() 会按本地时区解析，
    导致显示偏差 +N 小时（N = 本地时区偏移）。加 Z 后明确告知是 UTC。
    """
    if dt is None:
        return None
    return dt.isoformat(timespec="seconds") + "Z"


def session_to_dto(s: Session, market_extras: dict[str, Any] | None = None) -> dict:
    """Web DTO：AI 计划 + 验证摘要（不含用户主观计划）。"""
    ai_plan = repo.get_ai_plan(s.id) if s.id else None
    verification = repo.get_verification(s.id) if s.id else None

    market = s.market_snapshot or {}
    settings = get_settings()
    # 优先用会话自带字段，回退全局默认（兼容老数据）
    verify_hours = s.verify_after_hours or settings.verification_delay_hours
    can_verify_at = (
        s.created_at + timedelta(hours=verify_hours) if s.created_at else None
    )

    return {
        "id": s.id,
        "symbol": s.symbol,
        "timeframe": s.timeframe,
        "status": s.status,
        "ai_error": s.ai_error,
        "created_at": _iso_utc(s.created_at),
        "expire_at": _iso_utc(s.expire_at),
        "can_verify_at": _iso_utc(can_verify_at),
        "verify_after_hours": verify_hours,
        "derivatives": market.get("derivatives"),
        "macro": market.get("macro"),
        "current_price": (market_extras or {}).get(
            "current_price", market.get("current_price")
        ),
        "high_24h": (market_extras or {}).get("high_24h", market.get("high_24h")),
        "low_24h": (market_extras or {}).get("low_24h", market.get("low_24h")),
        "captured_at": market.get("captured_at"),
        "structure": (market_extras or {}).get("structure"),
        "fib": (market_extras or {}).get("fib"),
        "indicators": (market_extras or {}).get("indicators"),
        "baseline_plan": (market_extras or {}).get("baseline_plan"),
        "latency_ms": (market_extras or {}).get("latency_ms"),
        "chat_log": list(s.chat_log or []),
        "ai_plan": _ai_plan_dto(ai_plan) if ai_plan else None,
        "verification": _verification_dto(verification) if verification else None,
    }


def _ai_plan_dto(p: AIPlan) -> dict:
    return {
        "direction": p.direction,
        "entry_low": p.entry_low,
        "entry_high": p.entry_high,
        "stop_loss": p.stop_loss,
        "take_profit_1": p.take_profit_1,
        "take_profit_2": p.take_profit_2,
        "rr_ratio": p.rr_ratio,
        "rationale": p.rationale,
        "model_id": p.model_id,
        "prompt_version": p.prompt_version,
        "cost_usd": p.cost_usd,
    }


def _verification_dto(v: Verification) -> dict:
    return {
        "verified_at": _iso_utc(v.verified_at),
        "actual_high": v.actual_high,
        "actual_low": v.actual_low,
        "actual_close": v.actual_close,
        "ai_outcome": v.ai_outcome,
        "ai_pnl_r": v.ai_pnl_r,
        "optimal_pnl_r": v.optimal_pnl_r,
    }
