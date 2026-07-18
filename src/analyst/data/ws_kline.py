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

import httpx
from websockets.asyncio.client import connect

from analyst.data.fetcher import Candle

logger = logging.getLogger("uvicorn.error")

SPOT_WS = "wss://stream.binance.com:9443/ws"
FUTURES_WS = "wss://fstream.binance.com/ws"
SPOT_REST_KLINES = "https://api.binance.com/api/v3/klines"
FUTURES_REST_KLINES = "https://fapi.binance.com/fapi/v1/klines"
FUTURES_REST_PREMIUM = "https://fapi.binance.com/fapi/v1/premiumIndex"

# WS 连上但静默超过该秒数 → 判定该端点不出数据，切 REST 轮询兜底
WS_NO_DATA_TIMEOUT = 45.0
# REST 兜底持续时间，到点后回头再试 WS
REST_FALLBACK_SECONDS = 300.0
REST_POLL_INTERVAL = 2.5

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
        taker_buy_volume=float(data.get("V", 0.0)),
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


def _rest_fetch_latest_klines(
    symbol: str, timeframe: str, *, market: str, limit: int = 2
) -> list[tuple[Candle, bool]]:
    """REST 拉最近 K 线。倒数第二根视为已收盘，最后一根为进行中。"""
    base = FUTURES_REST_KLINES if market == "futures" else SPOT_REST_KLINES
    fsym = symbol.split(":")[0].replace("/", "").upper()
    r = httpx.get(
        base,
        params={"symbol": fsym, "interval": timeframe, "limit": limit},
        timeout=10.0,
    )
    r.raise_for_status()
    rows = r.json()
    out: list[tuple[Candle, bool]] = []
    for i, k in enumerate(rows):
        candle = Candle(
            timestamp=datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc).replace(
                tzinfo=None
            ),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5]),
            taker_buy_volume=float(k[9]) if len(k) > 9 else 0.0,
        )
        out.append((candle, i < len(rows) - 1))
    return out


async def _rest_poll_klines(
    symbol: str,
    timeframe: str,
    *,
    market: str,
    stop_event: asyncio.Event,
    duration: float,
) -> AsyncIterator[tuple[Candle, bool]]:
    """WS 不出数据时的 REST 轮询兜底：进行中 K 实时刷新，收盘只推一次。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration
    last_closed_ts: datetime | None = None
    while not stop_event.is_set() and loop.time() < deadline:
        try:
            rows = await asyncio.to_thread(
                _rest_fetch_latest_klines, symbol, timeframe, market=market, limit=3
            )
            for candle, is_closed in rows:
                if is_closed:
                    if last_closed_ts is not None and candle.timestamp <= last_closed_ts:
                        continue
                    last_closed_ts = candle.timestamp
                yield candle, is_closed
        except Exception as e:
            logger.warning("REST kline poll failed %s %s: %s", symbol, timeframe, e)
        await asyncio.sleep(REST_POLL_INTERVAL)


async def stream_klines(
    symbol: str,
    timeframe: str,
    *,
    market: str = "spot",
    on_kline: OnKline | None = None,
    stop_event: asyncio.Event | None = None,
) -> AsyncIterator[tuple[Candle, bool]]:
    """持续订阅 K 线；断线自动重连。

    WS 连上但长时间（WS_NO_DATA_TIMEOUT）收不到 K 线时（网络层静默丢流），
    自动降级为 REST 轮询一段时间，再回头重试 WS。

    Yields:
        (candle, is_closed) —— is_closed=True 表示该根已收盘，可做策略评估。
    """
    url = kline_url(symbol, timeframe, market=market)
    stop_event = stop_event or asyncio.Event()
    backoff = 1.0

    while not stop_event.is_set():
        got_data = False
        try:
            logger.info("WS connect %s", url)
            async with connect(url, ping_interval=20, ping_timeout=60) as ws:
                backoff = 1.0
                silent_for = 0.0
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        silent_for += 1.0
                        if not got_data and silent_for >= WS_NO_DATA_TIMEOUT:
                            raise _WsSilent(
                                f"WS 静默 {silent_for:.0f}s 无 K 线：{url}"
                            )
                        continue
                    payload = json.loads(raw)
                    parsed = _parse_kline_msg(payload)
                    if parsed is None:
                        continue
                    silent_for = 0.0
                    got_data = True
                    candle, is_closed = parsed
                    if on_kline is not None:
                        maybe = on_kline(candle, is_closed)
                        if asyncio.iscoroutine(maybe):
                            await maybe
                    yield candle, is_closed
        except asyncio.CancelledError:
            raise
        except _WsSilent as e:
            logger.warning("%s — 切 REST 轮询 %.0fs", e, REST_FALLBACK_SECONDS)
            async for item in _rest_poll_klines(
                symbol,
                timeframe,
                market=market,
                stop_event=stop_event,
                duration=REST_FALLBACK_SECONDS,
            ):
                candle, is_closed = item
                if on_kline is not None:
                    maybe = on_kline(candle, is_closed)
                    if asyncio.iscoroutine(maybe):
                        await maybe
                yield candle, is_closed
            logger.info("REST 兜底结束，重试 WS %s", url)
        except Exception as e:
            if stop_event.is_set():
                break
            logger.warning("WS error: %s — retry in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


class _WsSilent(Exception):
    """WS 已连接但迟迟无业务数据。"""


def _rest_fetch_premium(symbol: str) -> dict[str, Any] | None:
    fsym = symbol.split(":")[0].replace("/", "").upper()
    r = httpx.get(FUTURES_REST_PREMIUM, params={"symbol": fsym}, timeout=10.0)
    r.raise_for_status()
    data = r.json()
    mark = float(data["markPrice"])
    index = float(data.get("indexPrice") or 0) or None
    premium_pct = (mark - index) / index * 100.0 if index else None
    return {
        "mark_price": mark,
        "index_price": index,
        "funding_rate": float(data.get("lastFundingRate") or 0),
        "estimated_settle_price": (
            float(data["estimatedSettlePrice"]) if data.get("estimatedSettlePrice") else None
        ),
        "premium_pct": premium_pct,
        "next_funding_time": int(data["nextFundingTime"]) if data.get("nextFundingTime") else None,
        "event_time": int(data["time"]) if data.get("time") else None,
    }


async def stream_mark_price(
    symbol: str,
    *,
    speed: str = "1s",
    stop_event: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """订阅 U 本位标记价格流；WS 静默时降级 REST premiumIndex 轮询。"""
    url = mark_price_url(symbol, speed=speed)
    stop_event = stop_event or asyncio.Event()
    backoff = 1.0

    while not stop_event.is_set():
        got_data = False
        try:
            logger.info("WS connect %s", url)
            async with connect(url, ping_interval=20, ping_timeout=60) as ws:
                backoff = 1.0
                silent_for = 0.0
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        silent_for += 1.0
                        if not got_data and silent_for >= WS_NO_DATA_TIMEOUT:
                            raise _WsSilent(
                                f"markPrice WS 静默 {silent_for:.0f}s：{url}"
                            )
                        continue
                    payload = json.loads(raw)
                    parsed = _parse_mark_price_msg(payload)
                    if parsed is None:
                        continue
                    silent_for = 0.0
                    got_data = True
                    yield parsed
        except asyncio.CancelledError:
            raise
        except _WsSilent as e:
            logger.warning("%s — 切 REST 轮询 %.0fs", e, REST_FALLBACK_SECONDS)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + REST_FALLBACK_SECONDS
            while not stop_event.is_set() and loop.time() < deadline:
                try:
                    prem = await asyncio.to_thread(_rest_fetch_premium, symbol)
                    if prem:
                        yield prem
                except Exception as pe:
                    logger.warning("REST premium poll failed %s: %s", symbol, pe)
                await asyncio.sleep(3.0)
            logger.info("REST 兜底结束，重试 markPrice WS %s", url)
        except Exception as e:
            if stop_event.is_set():
                break
            logger.warning("markPrice WS error: %s — retry in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
