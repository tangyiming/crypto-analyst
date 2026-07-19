"""K 线 → 前端 JSON 序列化。"""

from __future__ import annotations

from datetime import timezone

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
