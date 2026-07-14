"""市场快照 - 把多周期数据组装成训练会话用的快照。"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from analyst.data.derivatives import DerivativesSnapshot, fetch_derivatives
from analyst.data.fetcher import CandleSeries, fetch_multi_timeframe
from analyst.data.macro import MacroSnapshot, fetch_macro


@dataclass
class MarketSnapshot:
    """一次训练会话的完整市场快照。"""

    symbol: str
    captured_at: float                    # unix timestamp (UTC)
    current_price: float
    high_24h: float
    low_24h: float
    high_7d: float
    low_7d: float
    high_30d: float
    low_30d: float
    timeframes: dict[str, CandleSeries]

    # 新增：衍生品 + 宏观（可选，外部 API 失败时为 None）
    derivatives: DerivativesSnapshot | None = None
    macro: MacroSnapshot | None = None

    def to_dict(self) -> dict:
        """序列化用，存数据库（不含 candle 明细，只存元数据 + 最近 N 根）。"""
        return {
            "symbol": self.symbol,
            "captured_at": self.captured_at,
            "current_price": self.current_price,
            "high_24h": self.high_24h,
            "low_24h": self.low_24h,
            "high_7d": self.high_7d,
            "low_7d": self.low_7d,
            "high_30d": self.high_30d,
            "low_30d": self.low_30d,
            "timeframes": {
                tf: _series_to_dict(s, last_n=50)
                for tf, s in self.timeframes.items()
            },
            "derivatives": asdict(self.derivatives) if self.derivatives else None,
            "macro": asdict(self.macro) if self.macro else None,
        }


def _series_to_dict(series: CandleSeries, last_n: int = 50) -> dict:
    """只保留最近 N 根 K线（避免数据库膨胀）。"""
    candles = series.candles[-last_n:]
    return {
        "symbol": series.symbol,
        "timeframe": series.timeframe,
        "candles": [
            {
                "timestamp": c.timestamp.isoformat(),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ],
    }


def build_snapshot(symbol: str, market: str = "spot") -> MarketSnapshot:
    """构建一个完整的市场快照。

    Args:
        symbol: 交易对，如 BTC/USDT
        market: spot | futures（监控页/仅合约币用 futures）
    """
    market = market if market in ("spot", "futures") else "spot"
    timeframes_data = fetch_multi_timeframe(
        symbol,
        timeframes=["1d", "4h", "1h", "30m"],
        market=market,
    )

    daily = timeframes_data["1d"]
    h4 = timeframes_data["4h"]

    current_price = h4.candles[-1].close

    last_24h = h4.candles[-6:]                # 6 × 4h ≈ 24h
    high_24h = max(c.high for c in last_24h)
    low_24h = min(c.low for c in last_24h)

    last_7d = daily.candles[-7:]
    high_7d = max(c.high for c in last_7d)
    low_7d = min(c.low for c in last_7d)

    last_30d = daily.candles[-30:]
    high_30d = max(c.high for c in last_30d)
    low_30d = min(c.low for c in last_30d)

    # 衍生品和宏观数据失败不阻断（K 线分析依然能跑）
    derivatives = fetch_derivatives(symbol)
    macro = fetch_macro()

    return MarketSnapshot(
        symbol=symbol,
        captured_at=datetime.now(timezone.utc).timestamp(),
        current_price=current_price,
        high_24h=high_24h,
        low_24h=low_24h,
        high_7d=high_7d,
        low_7d=low_7d,
        high_30d=high_30d,
        low_30d=low_30d,
        timeframes=timeframes_data,
        derivatives=derivatives,
        macro=macro,
    )
