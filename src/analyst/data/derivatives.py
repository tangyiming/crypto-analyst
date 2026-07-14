"""永续合约衍生品数据：funding rate、open interest、多空持仓比。

数据源：Binance Futures Public API（fapi.binance.com，无需 API key）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import diskcache
import requests

from analyst.config import get_settings

log = logging.getLogger(__name__)

BINANCE_FAPI = "https://fapi.binance.com"


@dataclass
class DerivativesSnapshot:
    """衍生品快照。"""

    symbol: str

    # ─── Funding Rate ───
    funding_rate: float           # 当前 8h funding rate（小数，如 0.0001 = 0.01%）
    funding_rate_pct: float       # 百分比形式
    next_funding_time_ms: int     # 下次结算时间（ms）

    # ─── Open Interest ───
    open_interest: float          # 当前 OI（合约数）
    oi_change_pct_4h: float       # 4h 变化百分比
    oi_change_pct_24h: float      # 24h 变化百分比

    # ─── 多空比 ───
    long_short_ratio: float       # 大户持仓多空比（>1 多>空）

    # ─── Mark / Index Price ───
    mark_price: float
    index_price: float
    basis_pct: float              # 期现基差 (mark-index)/index ×100

    # ─── 综合解读 ───
    funding_sentiment: str        # "🔥 多头狂热" / "❄️ 空头狂热" / ...
    oi_signal: str                # "新增多头/空头/回补"


# ─────────────────────────────────────
# 缓存（避免短时间重复调用）
# ─────────────────────────────────────
def _cache():
    settings = get_settings()
    return diskcache.Cache(str(settings.cache_path / "derivatives"))


def _get_json(url: str, params: dict, timeout: int = 5):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_derivatives(symbol: str, ttl_seconds: int = 60) -> DerivativesSnapshot | None:
    """获取衍生品快照。

    Args:
        symbol: 现货格式（如 "BTC/USDT"），自动转换为 Binance 永续格式。
        ttl_seconds: 缓存秒数（默认 1 分钟，funding/OI 变化慢）

    Returns:
        快照；该币种没有 USDT 永续 / 接口失败时返回 None。
    """
    fsym = symbol.replace("/", "").upper()  # BTC/USDT → BTCUSDT
    cache = _cache()
    cache_key = f"deriv:{fsym}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        # ─── 1. premium index：funding rate + mark/index ───
        prem = _get_json(
            f"{BINANCE_FAPI}/fapi/v1/premiumIndex",
            params={"symbol": fsym},
        )
        funding_rate = float(prem["lastFundingRate"])
        mark = float(prem["markPrice"])
        index = float(prem.get("indexPrice", mark) or mark)
        next_funding_ms = int(prem["nextFundingTime"])
        basis = (mark - index) / index * 100 if index else 0.0

        # ─── 2. open interest 当前值 ───
        oi_now_data = _get_json(
            f"{BINANCE_FAPI}/fapi/v1/openInterest",
            params={"symbol": fsym},
        )
        oi_now = float(oi_now_data["openInterest"])

        # ─── 3. open interest 历史（24h）───
        oi_hist = _get_json(
            f"{BINANCE_FAPI}/futures/data/openInterestHist",
            params={"symbol": fsym, "period": "1h", "limit": 25},
        )
        if oi_hist:
            oi_24h = float(oi_hist[0]["sumOpenInterest"])
            oi_4h = (
                float(oi_hist[-5]["sumOpenInterest"])
                if len(oi_hist) >= 5
                else oi_now
            )
        else:
            oi_24h = oi_now
            oi_4h = oi_now

        oi_24h_pct = (oi_now - oi_24h) / oi_24h * 100 if oi_24h else 0.0
        oi_4h_pct = (oi_now - oi_4h) / oi_4h * 100 if oi_4h else 0.0

        # ─── 4. 大户持仓多空比 ───
        try:
            lsr = _get_json(
                f"{BINANCE_FAPI}/futures/data/topLongShortPositionRatio",
                params={"symbol": fsym, "period": "1h", "limit": 1},
            )
            long_short = float(lsr[0]["longShortRatio"]) if lsr else 1.0
        except Exception:
            long_short = 1.0

        # ─── 解读 ───
        funding_pct = funding_rate * 100
        if funding_pct > 0.05:
            funding_sentiment = "🔥 多头狂热（小心反向）"
        elif funding_pct > 0.01:
            funding_sentiment = "📈 偏多"
        elif funding_pct < -0.05:
            funding_sentiment = "❄️ 空头狂热（反弹动能）"
        elif funding_pct < -0.01:
            funding_sentiment = "📉 偏空"
        else:
            funding_sentiment = "⚖️ 中性"

        # OI 信号：结合 24h 价格方向，但这里没有，先按 OI 变化幅度
        if oi_24h_pct > 10:
            oi_signal = f"📈 OI 24h +{oi_24h_pct:.1f}%（资金大量涌入）"
        elif oi_24h_pct < -10:
            oi_signal = f"📉 OI 24h {oi_24h_pct:.1f}%（资金撤离/平仓）"
        elif abs(oi_24h_pct) > 3:
            oi_signal = f"OI 24h {oi_24h_pct:+.1f}%"
        else:
            oi_signal = f"OI 24h {oi_24h_pct:+.1f}%（平稳）"

        snap = DerivativesSnapshot(
            symbol=symbol,
            funding_rate=funding_rate,
            funding_rate_pct=funding_pct,
            next_funding_time_ms=next_funding_ms,
            open_interest=oi_now,
            oi_change_pct_4h=oi_4h_pct,
            oi_change_pct_24h=oi_24h_pct,
            long_short_ratio=long_short,
            mark_price=mark,
            index_price=index,
            basis_pct=basis,
            funding_sentiment=funding_sentiment,
            oi_signal=oi_signal,
        )

        cache.set(cache_key, snap, expire=ttl_seconds)
        return snap

    except Exception as e:
        log.warning(f"fetch_derivatives({fsym}) 失败：{e}")
        return None
