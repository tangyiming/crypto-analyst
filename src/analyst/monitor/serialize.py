"""信号 → 前端 / 存储用的 JSON 序列化。"""

from __future__ import annotations

from datetime import datetime, timezone

from analyst.compute.strategies.double_line_reversal import DoubleLineSignal
from analyst.data.fetcher import Candle


def candle_to_dict(c: Candle) -> dict:
    # lightweight-charts 用 UTC 秒
    ts = c.timestamp.replace(tzinfo=timezone.utc).timestamp()
    return {
        "time": int(ts),
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "volume": c.volume,
    }


def signal_to_alert_dict(
    symbol: str,
    timeframe: str,
    signal: DoubleLineSignal,
) -> dict:
    plan = signal.plan
    kelly = signal.kelly
    bar_ts = signal.bar_ts
    if isinstance(bar_ts, datetime):
        if bar_ts.tzinfo is None:
            marker_time = int(bar_ts.replace(tzinfo=timezone.utc).timestamp())
        else:
            marker_time = int(bar_ts.astimezone(timezone.utc).timestamp())
    else:
        marker_time = int(datetime.now(timezone.utc).timestamp())

    return {
        "type": "alert",
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": signal.direction,
        "strength": signal.strength,
        "price": signal.price,
        "pattern": signal.pattern,
        "break_level": signal.break_level,
        "reasons": list(signal.reasons),
        "filters_passed": list(signal.filters_passed),
        "marker_time": marker_time,
        "plan": None
        if plan is None
        else {
            "direction": plan.direction,
            "entry_low": plan.entry_low,
            "entry_high": plan.entry_high,
            "stop_loss": plan.stop_loss,
            "take_profit_1": plan.take_profit_1,
            "take_profit_2": plan.take_profit_2,
            "rr_ratio": plan.rr_ratio,
            "rationale": plan.rationale,
        },
        "kelly": None
        if kelly is None
        else {
            "suggested_fraction": kelly.suggested_fraction,
            "risk_budget_pct": kelly.risk_budget_pct,
            "note": kelly.note,
        },
        "trail_note": signal.trail_note,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
