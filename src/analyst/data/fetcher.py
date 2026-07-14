"""K线数据获取 - CCXT 封装。

职责：
- 从交易所拉取多周期 K线
- diskcache 避免重复请求
- 自动重试与限速保护

注意：所有 timestamp 都是 naive UTC datetime（不带 tzinfo）。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import ccxt
from diskcache import Cache

from analyst.config import get_settings


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class CandleSeries:
    symbol: str
    timeframe: str
    candles: list[Candle]

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.candles]

    @property
    def highs(self) -> list[float]:
        return [c.high for c in self.candles]

    @property
    def lows(self) -> list[float]:
        return [c.low for c in self.candles]

    @property
    def latest(self) -> Candle:
        return self.candles[-1]

    @property
    def latest_close(self) -> float:
        return self.candles[-1].close


_exchange = None
_futures_exchange = None
_cache: Cache | None = None


def get_exchange(market: str = "spot"):
    """获取 CCXT 交易所实例。

    market=spot → binance；market=futures → binanceusdm（U 本位）。
    """
    global _exchange, _futures_exchange
    settings = get_settings()
    if market == "futures":
        if _futures_exchange is None:
            _futures_exchange = ccxt.binanceusdm({"enableRateLimit": True})
        return _futures_exchange
    if _exchange is None:
        ex_class = getattr(ccxt, settings.exchange)
        _exchange = ex_class({"enableRateLimit": True})
    return _exchange


def get_cache() -> Cache:
    """获取磁盘缓存实例。"""
    global _cache
    if _cache is None:
        settings = get_settings()
        Path(settings.data_cache_dir).mkdir(parents=True, exist_ok=True)
        _cache = Cache(settings.data_cache_dir)
    return _cache


def _ccxt_symbol(symbol: str, market: str) -> str:
    """统一成 CCXT 符号：现货 BTC/USDT；U 本位 BTC/USDT:USDT。"""
    s = symbol.upper().strip()
    if "/" not in s:
        s = f"{s}/USDT"
    if market == "futures" and ":" not in s:
        base_quote = s.split(":")[0]
        s = f"{base_quote}:USDT"
    return s


def fetch_candles(
    symbol: str,
    timeframe: str = "4h",
    limit: int = 200,
    use_cache: bool = True,
    market: str = "spot",
) -> CandleSeries:
    """拉取指定品种和周期的 K线数据。

    Args:
        symbol: 形如 'BTC/USDT' 或 'HYPE/USDT'
        timeframe: '1m','5m','15m','30m','1h','2h','4h','1d'
        limit: 返回多少根
        use_cache: 是否使用缓存（在 TTL 内复用）
        market: spot | futures（HYPE 等仅合约上市时用 futures）
    """
    settings = get_settings()
    market = market if market in ("spot", "futures") else "spot"
    ccxt_sym = _ccxt_symbol(symbol, market)
    display_sym = ccxt_sym.split(":")[0]
    cache_key = f"ohlcv:{settings.exchange}:{market}:{ccxt_sym}:{timeframe}:{limit}"
    cache = get_cache()

    if use_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return _restore_series(cached)

    exchange = get_exchange(market)

    last_err: Exception | None = None
    ohlcv: list | None = None
    for attempt in range(3):
        try:
            ohlcv = exchange.fetch_ohlcv(ccxt_sym, timeframe=timeframe, limit=limit)
            break
        except Exception as e:
            last_err = e
            time.sleep(1 + attempt)

    if ohlcv is None:
        raise RuntimeError(f"Failed to fetch {ccxt_sym} {timeframe} ({market}): {last_err}")

    candles = [
        Candle(
            timestamp=datetime.utcfromtimestamp(row[0] / 1000),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in ohlcv
    ]
    series = CandleSeries(symbol=display_sym, timeframe=timeframe, candles=candles)

    if use_cache:
        cache.set(
            cache_key,
            _serialize_series(series),
            expire=settings.data_cache_ttl_minutes * 60,
        )

    return series


def list_usdt_perp_symbols(*, use_cache: bool = True) -> list[str]:
    """币安 U 本位永续 USDT 交易对列表（如 BTC/USDT），用于监控页过滤搜索。"""
    cache = get_cache()
    cache_key = "markets:binanceusdm:usdt_perp"
    if use_cache:
        cached = cache.get(cache_key)
        if cached:
            return list(cached)

    exchange = get_exchange("futures")
    markets = exchange.load_markets()
    seen: set[str] = set()
    symbols: list[str] = []
    for m in markets.values():
        if not m.get("active", True):
            continue
        if m.get("quote") != "USDT":
            continue
        settle = m.get("settle")
        if settle and settle != "USDT":
            continue
        # 永续 / 线性合约
        if not (m.get("swap") or m.get("linear")):
            continue
        base = m.get("base")
        if not base:
            continue
        sym = f"{base}/USDT"
        if sym not in seen:
            seen.add(sym)
            symbols.append(sym)

    symbols.sort(key=lambda s: (0 if s in ("BTC/USDT", "ETH/USDT") else 1, s))
    cache.set(cache_key, symbols, expire=6 * 3600)
    return symbols


def fetch_multi_timeframe(
    symbol: str,
    timeframes: list[str] | None = None,
    limit: int = 200,
    market: str = "spot",
) -> dict[str, CandleSeries]:
    """同时拉取多个周期。"""
    if timeframes is None:
        timeframes = ["1d", "4h", "1h", "30m"]
    return {
        tf: fetch_candles(symbol, timeframe=tf, limit=limit, market=market)
        for tf in timeframes
    }


def _serialize_series(series: CandleSeries) -> dict:
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
            for c in series.candles
        ],
    }


def _restore_series(data: dict) -> CandleSeries:
    candles = [
        Candle(
            timestamp=datetime.fromisoformat(c["timestamp"]),
            open=c["open"],
            high=c["high"],
            low=c["low"],
            close=c["close"],
            volume=c["volume"],
        )
        for c in data["candles"]
    ]
    return CandleSeries(
        symbol=data["symbol"],
        timeframe=data["timeframe"],
        candles=candles,
    )
