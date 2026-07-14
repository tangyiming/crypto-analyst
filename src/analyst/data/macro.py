"""宏观市场数据：BTC 主导率、Fear & Greed Index。

数据源：
- CoinGecko Global API（免费、无需 key）
- Alternative.me Fear & Greed（免费、无需 key）

对训练系统的价值：
- 分析 alt 时考虑大盘情绪（BTC.D 上升 → alt 难涨）
- F&G 极端值作为反向指标（贪婪 > 80 警惕，恐惧 < 25 关注抄底）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import diskcache
import requests

from analyst.config import get_settings

log = logging.getLogger(__name__)


@dataclass
class MacroSnapshot:
    """全市场情绪 + 资金分布快照。"""

    btc_dominance: float          # BTC 市值占比（%）
    eth_dominance: float          # ETH 市值占比（%）
    total_market_cap_usd: float   # 加密总市值（USD）
    market_cap_change_24h: float  # 24h 总市值变化（%）

    fear_greed_index: int         # 0-100
    fear_greed_label: str         # extreme_fear / fear / neutral / greed / extreme_greed
    fear_greed_emoji: str         # 视觉标识

    summary: str                  # 一句话整体氛围


def _cache():
    settings = get_settings()
    return diskcache.Cache(str(settings.cache_path / "macro"))


def fetch_macro(ttl_seconds: int = 600) -> MacroSnapshot | None:
    """获取全市场宏观快照。

    缓存 10 分钟（这些数据变化非常慢）。
    """
    cache = _cache()
    cached = cache.get("macro")
    if cached is not None:
        return cached

    try:
        # ─── CoinGecko Global ───
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=8)
        g = r.json()["data"]
        btc_dom = g["market_cap_percentage"]["btc"]
        eth_dom = g["market_cap_percentage"]["eth"]
        total_cap = g["total_market_cap"]["usd"]
        cap_change = g["market_cap_change_percentage_24h_usd"]

        # ─── Alternative.me Fear & Greed ───
        r2 = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        fng = r2.json()["data"][0]
        fng_value = int(fng["value"])
        fng_label = fng["value_classification"].lower().replace(" ", "_")

        if fng_value <= 25:
            fng_emoji = "😱 极度恐惧"
        elif fng_value <= 45:
            fng_emoji = "😟 恐惧"
        elif fng_value <= 55:
            fng_emoji = "😐 中性"
        elif fng_value <= 75:
            fng_emoji = "🤑 贪婪"
        else:
            fng_emoji = "🚨 极度贪婪"

        # ─── 整体氛围 ───
        parts = [f"BTC.D {btc_dom:.1f}%"]
        if cap_change > 2:
            parts.append(f"24h 总市值 +{cap_change:.1f}% 🟢")
        elif cap_change < -2:
            parts.append(f"24h 总市值 {cap_change:.1f}% 🔴")
        else:
            parts.append(f"24h 总市值 {cap_change:+.1f}%")
        parts.append(fng_emoji)
        summary = " · ".join(parts)

        snap = MacroSnapshot(
            btc_dominance=btc_dom,
            eth_dominance=eth_dom,
            total_market_cap_usd=total_cap,
            market_cap_change_24h=cap_change,
            fear_greed_index=fng_value,
            fear_greed_label=fng_label,
            fear_greed_emoji=fng_emoji,
            summary=summary,
        )

        cache.set("macro", snap, expire=ttl_seconds)
        return snap

    except Exception as e:
        log.warning(f"fetch_macro 失败：{e}")
        return None
