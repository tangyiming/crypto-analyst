"""Binance K 线 / 标记价格 WebSocket（现货 / U 本位）。

产出与 REST 相同的 Candle，供 monitor 增量更新 CandleSeries。
U 本位另提供 markPrice 流（标记价 / 指数价 / 资金费率）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from websockets.asyncio.client import connect

from analyst.data.fetcher import Candle

logger = logging.getLogger(__name__)

SPOT_WS = "wss://stream.binance.com:9443/ws"
FUTURES_WS = "wss://fstream.binance.com/ws"

# 币安 U 本位 K 线周期（官方全部）
BINANCE_FUTURES_INTERVALS: list[str] = [
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
]

OnKline = Callable[[Candle, bool], Awaitable[None] | None]


def symbol_to_stream(symbol: str) -> str:
    """BTC/USDT 或 HYPE/USDT:USDT → btcusdt / hypeusdt。"""
    base = symbol.split(":")[0]
    return base.replace("/", "").lower()


def kline_url(symbol: str, timeframe: str, *, market: str = "spot") -> str:
    stream = f"{symbol_to_stream(symbol)}@kline_{timeframe}"
    base = FUTURES_WS if market == "futures" else SPOT_WS
    return f"{base}/{stream}"


def mark_price_url(symbol: str, *, speed: str = "1s") -> str:
    """U 本位标记价流（含指数价、资金费率）。speed: 1s | ''（约 3s）。"""
    s = symbol_to_stream(symbol)
    stream = f"{s}@markPrice@{speed}" if speed else f"{s}@markPrice"
    return f"{FUTURES_WS}/{stream}"


def _parse_kline_msg(payload: dict) -> tuple[Candle, bool] | None:
    """解析单流 kline 消息 → (Candle, is_closed)。"""
    data = payload.get("k") if "k" in payload else payload.get("data", {}).get("k")
    if not data:
        return None
    ts_ms = int(data["t"])
    candle = Candle(
        timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).replace(tzinfo=None),
        open=float(data["o"]),
        high=float(data["h"]),
        low=float(data["l"]),
        close=float(data["c"]),
        volume=float(data["v"]),
    )
    is_closed = bool(data.get("x"))
    return candle, is_closed


def _parse_mark_price_msg(payload: dict) -> dict[str, Any] | None:
    """解析 markPriceUpdate → 标记价 / 指数价 / 资金费率 / 溢价。"""
    data = payload
    if "data" in payload and isinstance(payload["data"], dict):
        data = payload["data"]
    if "p" not in data:
        return None
    mark = float(data["p"])
    index = float(data["i"]) if data.get("i") not in (None, "") else None
    funding = float(data["r"]) if data.get("r") not in (None, "") else None
    est_settle = float(data["P"]) if data.get("P") not in (None, "") else None
    premium_pct = None
    if index and index != 0:
        premium_pct = (mark - index) / index * 100.0
    return {
        "mark_price": mark,
        "index_price": index,
        "funding_rate": funding,
        "estimated_settle_price": est_settle,
        "premium_pct": premium_pct,
        "next_funding_time": int(data["T"]) if data.get("T") else None,
        "event_time": int(data["E"]) if data.get("E") else None,
    }


async def stream_klines(
    symbol: str,
    timeframe: str,
    *,
    market: str = "spot",
    on_kline: OnKline | None = None,
    stop_event: asyncio.Event | None = None,
) -> AsyncIterator[tuple[Candle, bool]]:
    """持续订阅 K 线；断线自动重连。

    Yields:
        (candle, is_closed) —— is_closed=True 表示该根已收盘，可做策略评估。
    """
    url = kline_url(symbol, timeframe, market=market)
    stop_event = stop_event or asyncio.Event()
    backoff = 1.0

    while not stop_event.is_set():
        try:
            logger.info("WS connect %s", url)
            async with connect(url, ping_interval=20, ping_timeout=60) as ws:
                backoff = 1.0
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        continue
                    payload = json.loads(raw)
                    parsed = _parse_kline_msg(payload)
                    if parsed is None:
                        continue
                    candle, is_closed = parsed
                    if on_kline is not None:
                        maybe = on_kline(candle, is_closed)
                        if asyncio.iscoroutine(maybe):
                            await maybe
                    yield candle, is_closed
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if stop_event.is_set():
                break
            logger.warning("WS error: %s — retry in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def stream_mark_price(
    symbol: str,
    *,
    speed: str = "1s",
    stop_event: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """订阅 U 本位标记价格流。"""
    url = mark_price_url(symbol, speed=speed)
    stop_event = stop_event or asyncio.Event()
    backoff = 1.0

    while not stop_event.is_set():
        try:
            logger.info("WS connect %s", url)
            async with connect(url, ping_interval=20, ping_timeout=60) as ws:
                backoff = 1.0
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        continue
                    payload = json.loads(raw)
                    parsed = _parse_mark_price_msg(payload)
                    if parsed is None:
                        continue
                    yield parsed
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if stop_event.is_set():
                break
            logger.warning("markPrice WS error: %s — retry in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
