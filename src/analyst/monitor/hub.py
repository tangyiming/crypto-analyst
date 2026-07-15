"""多客户端共享的实时监控中枢：Binance WS → 策略 → 浏览器 / Telegram。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from analyst.compute.strategies.double_line_reversal import (
    DoubleLineConfig,
    evaluate_double_line,
)
from analyst.compute.strategies.cycle_switch import (
    CycleSwitchConfig,
    build_cycle_regime,
    evaluate_cycle_switch,
)
from analyst.config import get_settings
from analyst.data.fetcher import Candle, CandleSeries, fetch_candles
from analyst.data.fetcher import fetch_candles_history
from analyst.data.ws_kline import stream_klines, stream_mark_price
from analyst.monitor.notifier import (
    build_default_notifier,
    format_rule_alert_text,
)
from analyst.monitor.rules import (
    RuleConfig,
    evaluate_closed_bar_rules,
    evaluate_premium_rules,
    rule_event_to_alert,
)
from analyst.monitor.serialize import candle_to_dict, signal_to_alert_dict

logger = logging.getLogger("uvicorn.error")

BINANCE_FAPI = "https://fapi.binance.com"


def _fetch_premium_index(symbol: str) -> dict[str, Any] | None:
    """REST 快照：标记价 / 指数价 / 资金费率。"""
    fsym = symbol.replace("/", "").upper()
    url = f"{BINANCE_FAPI}/fapi/v1/premiumIndex?symbol={fsym}"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            prem = json.loads(resp.read().decode())
        mark = float(prem["markPrice"])
        index = float(prem.get("indexPrice") or mark)
        funding = float(prem.get("lastFundingRate") or 0)
        premium_pct = (mark - index) / index * 100.0 if index else None
        return {
            "mark_price": mark,
            "index_price": index,
            "funding_rate": funding,
            "estimated_settle_price": float(prem["estimatedSettlePrice"])
            if prem.get("estimatedSettlePrice")
            else None,
            "premium_pct": premium_pct,
            "next_funding_time": int(prem["nextFundingTime"])
            if prem.get("nextFundingTime")
            else None,
        }
    except Exception as e:
        logger.warning("premiumIndex fetch failed %s: %s", symbol, e)
        return None


def _fetch_ticker_24h(symbol: str) -> dict[str, Any] | None:
    """U 本位 24h ticker：涨跌幅、高低点。"""
    fsym = symbol.replace("/", "").upper()
    url = f"{BINANCE_FAPI}/fapi/v1/ticker/24hr?symbol={fsym}"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        return {
            "change_pct_24h": float(data["priceChangePercent"]),
            "change_24h": float(data["priceChange"]),
            "high_24h": float(data["highPrice"]),
            "low_24h": float(data["lowPrice"]),
            "quote_volume_24h": float(data.get("quoteVolume") or 0),
        }
    except Exception as e:
        logger.warning("ticker/24hr fetch failed %s: %s", symbol, e)
        return None


def _fetch_contract_quote(symbol: str) -> dict[str, Any] | None:
    """合并 premiumIndex + 24h ticker，供监控顶栏展示。"""
    prem = _fetch_premium_index(symbol) or {}
    tick = _fetch_ticker_24h(symbol) or {}
    if not prem and not tick:
        return None
    return {**prem, **tick}


def _norm_symbol(symbol: str) -> str:
    s = symbol.upper().strip().replace("-", "/")
    if "/" not in s:
        # BTCUSDT → BTC/USDT；BTC → BTC/USDT
        if s.endswith("USDT") and len(s) > 4:
            s = f"{s[:-4]}/USDT"
        else:
            s = f"{s}/USDT"
    return s.split(":")[0]


@dataclass
class StreamKey:
    symbol: str
    timeframe: str
    market: str

    def __str__(self) -> str:
        return f"{self.symbol}|{self.timeframe}|{self.market}"


@dataclass
class StreamWorker:
    key: StreamKey
    series: CandleSeries
    clients: set[WebSocket] = field(default_factory=set)
    # 只收告警 / 状态，不收该品种 K 线（用于观察列表）
    alert_clients: set[WebSocket] = field(default_factory=set)
    task: asyncio.Task | None = None
    mark_task: asyncio.Task | None = None
    last_alert_key: str | None = None
    last_rule_keys: set[str] = field(default_factory=set)
    rule_state: dict[str, Any] = field(default_factory=dict)
    last_premium: dict[str, Any] | None = None
    strategy: DoubleLineConfig = field(default_factory=DoubleLineConfig)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    # 诊断：最近一根已处理收盘 K / 评估结果
    last_closed_at: datetime | None = None
    last_tick_at: datetime | None = None
    last_signal_dir: str = "wait"
    closed_bars: int = 0
    alerts_sent: int = 0
    last_cycle_position: float = 0.0


class MonitorHub:
    """进程内单例：按品种/周期复用一条 Binance 流。"""

    def __init__(self) -> None:
        self._workers: dict[str, StreamWorker] = {}
        self._alerts: deque[dict[str, Any]] = deque(maxlen=200)
        self._lock = asyncio.Lock()
        self._daemon_symbols: set[str] = set()
        self._daemon_timeframes: list[str] = ["15m"]
        self._startup_tg_sent: bool = False
        self._cycle_regime: dict | None = None
        self._last_outlook_key: str | None = None
        # AI 候选确认冷却：key = "SYMBOL|ai_tf" → unix ts
        self._ai_confirm_at: dict[str, float] = {}

    def _daemon_state_path(self) -> Any:
        from pathlib import Path

        return get_settings().cache_path / "monitor_daemon.json"

    @property
    def _primary_daemon_tf(self) -> str:
        return self._daemon_timeframes[0] if self._daemon_timeframes else "15m"

    def load_daemon_state(self) -> dict[str, Any]:
        """从磁盘/配置加载常驻盯盘列表。"""
        s = get_settings()
        # 多级别周期走 MONITOR_DAEMON_TIMEFRAMES，不受图表切换影响
        self._daemon_timeframes = list(s.daemon_timeframes_list)
        path = self._daemon_state_path()
        data: dict[str, Any] = {}
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("load daemon state failed: %s", e)
        syms = data.get("symbols") or s.daemon_symbols_list
        self._daemon_symbols = {_norm_symbol(x) for x in syms if x}
        return {
            "enabled": bool(s.monitor_always_on),
            "symbols": sorted(self._daemon_symbols),
            "timeframe": self._primary_daemon_tf,
            "timeframes": list(self._daemon_timeframes),
            "telegram_ready": self._telegram_ready(),
        }

    def save_daemon_state(self, symbols: list[str], timeframe: str | None = None) -> None:
        s = get_settings()
        self._daemon_symbols = {_norm_symbol(x) for x in symbols if x}
        self._daemon_timeframes = list(s.daemon_timeframes_list)
        path = self._daemon_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "symbols": sorted(self._daemon_symbols),
                        "timeframes": list(self._daemon_timeframes),
                        "timeframe": self._primary_daemon_tf,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("save daemon state failed: %s", e)

    def is_daemon_key(self, key: StreamKey) -> bool:
        s = get_settings()
        if not s.monitor_always_on:
            return False
        return (
            key.market == "futures"
            and key.timeframe in self._daemon_timeframes
            and key.symbol in self._daemon_symbols
        )

    def _symbol_has_mark_task(self, symbol: str) -> bool:
        for w in self._workers.values():
            if (
                w.key.symbol == symbol
                and w.mark_task is not None
                and not w.mark_task.done()
            ):
                return True
        return False

    async def start_always_on_workers(self) -> dict[str, Any]:
        """Web 启动或开关打开时：为常驻列表×多周期拉起 worker（不依赖浏览器）。"""
        info = self.load_daemon_state()
        if not get_settings().monitor_always_on:
            logger.info("monitor_always_on=false，跳过常驻盯盘")
            return info
        if not self._daemon_symbols:
            logger.warning("常驻盯盘品种为空")
            return info
        started, stopped = await self._reconcile_daemon_workers()
        ok = [
            str(w.key)
            for w in self._workers.values()
            if self.is_daemon_key(w.key) and w.task and not w.task.done()
        ]
        if stopped:
            logger.info("常驻盯盘 reconcile：停止 %d 个过期 worker", len(stopped))
        logger.info(
            "常驻盯盘已同步 %d workers · %d品种 × %s · TG=%s",
            len(ok),
            len(self._daemon_symbols),
            ",".join(list(self._daemon_timeframes) or ["15m"]),
            self._telegram_ready(),
        )
        info["running"] = ok
        info["started"] = started
        info["stopped"] = stopped
        info["timeframes"] = list(self._daemon_timeframes) or ["15m"]
        info["enabled"] = True
        # 进程内只推一次启动心跳，避免页面 daemon/sync 反复刷 TG
        if self._telegram_ready() and ok and not self._startup_tg_sent:
            self._startup_tg_sent = True
            tfs = list(self._daemon_timeframes) or ["15m"]
            await self._notify_telegram_text(
                "✅ Crypto Analyst 常驻盯盘已启动\n"
                f"workers={len(ok)} · 品种={len(self._daemon_symbols)} · "
                f"周期={','.join(tfs)}\n"
                "TG：各币策略点位照推；周期位置日更每天 1 条"
            )
        return info

    def worker_health(self) -> list[dict[str, Any]]:
        """供 /api/monitor/daemon 诊断：每个 worker 是否存活、最近收盘评估。"""
        rows: list[dict[str, Any]] = []
        # candle.timestamp 为 naive UTC，统一用 naive 比较
        now = datetime.utcnow()
        for w in sorted(self._workers.values(), key=lambda x: str(x.key)):
            task_ok = bool(w.task and not w.task.done())
            mark_ok = bool(w.mark_task and not w.mark_task.done())
            last_c = w.last_closed_at
            if last_c is not None and last_c.tzinfo is not None:
                last_c = last_c.replace(tzinfo=None)
            rows.append(
                {
                    "key": str(w.key),
                    "daemon": self.is_daemon_key(w.key),
                    "task_alive": task_ok,
                    "mark_alive": mark_ok,
                    "bars": len(w.series.candles),
                    "closed_bars": w.closed_bars,
                    "alerts_sent": w.alerts_sent,
                    "last_signal": w.last_signal_dir,
                    "last_closed_at": last_c.isoformat() if last_c else None,
                    "last_tick_at": w.last_tick_at.isoformat() if w.last_tick_at else None,
                    "sec_since_close": (
                        round((now - last_c).total_seconds()) if last_c else None
                    ),
                    "clients": len(w.clients),
                    "price": float(w.series.candles[-1].close) if w.series.candles else None,
                }
            )
        return rows

    def log_heartbeat(self) -> None:
        health = self.worker_health()
        alive = sum(1 for h in health if h["task_alive"])
        daemon_n = sum(1 for h in health if h["daemon"])
        closed_any = [h for h in health if h["last_closed_at"]]
        newest = max(
            (h["last_closed_at"] for h in closed_any),
            default=None,
        )
        logger.info(
            "盯盘心跳 workers=%d alive=%d daemon=%d alerts_mem=%d tg=%s "
            "newest_close=%s sample=%s",
            len(health),
            alive,
            daemon_n,
            len(self._alerts),
            self._telegram_ready(),
            newest,
            [
                f"{h['key']}@{h['last_signal']}/c{h['closed_bars']}"
                for h in health[:4]
            ],
        )
        dead = [h["key"] for h in health if h["daemon"] and not h["task_alive"]]
        if dead:
            logger.warning("daemon worker 已退出: %s", dead)

    async def _notify_telegram_text(self, text: str) -> None:
        settings = get_settings()
        if not self._telegram_ready():
            logger.info("跳过 TG 文本（未配置 token/chat_id）: %s", text[:80])
            return
        notifier = build_default_notifier(
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
        )
        await asyncio.to_thread(notifier.send_text, text)

    def recent_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        items = list(self._alerts)
        return items[-limit:][::-1]

    def history(
        self,
        symbol: str,
        timeframe: str,
        *,
        market: str = "spot",
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        symbol = _norm_symbol(symbol)
        key = str(StreamKey(symbol, timeframe, market))
        worker = self._workers.get(key)
        if worker and worker.series.candles:
            candles = worker.series.candles[-limit:]
            return [candle_to_dict(c) for c in candles]
        series = fetch_candles(
            symbol, timeframe=timeframe, limit=limit, use_cache=False, market=market
        )
        return [candle_to_dict(c) for c in series.candles]

    async def connect_client(
        self,
        websocket: WebSocket,
        symbol: str,
        timeframe: str,
        *,
        market: str = "spot",
        watch_symbols: list[str] | None = None,
    ) -> None:
        symbol = _norm_symbol(symbol)
        market = market if market in ("spot", "futures") else "spot"
        key = StreamKey(symbol, timeframe, market)

        # 先 accept，再拉主图；观察列表放到后台，避免排队卡死
        await websocket.accept()
        await websocket.send_json(
            {"type": "status", "message": f"正在加载 {symbol} {timeframe}…"}
        )

        try:
            worker = await self._ensure_worker(key)
        except Exception as e:
            logger.exception("ensure worker failed %s", key)
            await websocket.send_json(
                {"type": "status", "message": f"加载失败：{e}", "level": "error"}
            )
            await websocket.close(code=1011)
            return

        worker.clients.add(websocket)
        extras: list[StreamWorker] = []

        try:
            # 立刻推主图 snapshot + 后续 K 线；不要等 12 个观察对全拉完
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "market": market,
                    "candles": [candle_to_dict(c) for c in worker.series.candles],
                    "premium": worker.last_premium,
                    "telegram_ready": self._telegram_ready(),
                    "watching": [symbol],
                }
            )
            await websocket.send_json(
                {
                    "type": "status",
                    "message": f"已订阅 {symbol} {timeframe} ({market})",
                }
            )

            watch_syms = [
                _norm_symbol(raw)
                for raw in (watch_symbols or [])
                if _norm_symbol(raw) != symbol
            ]
            if watch_syms:
                asyncio.create_task(
                    self._attach_watchlist(websocket, timeframe, market, watch_syms, extras),
                    name=f"watchlist-{symbol}",
                )
                # 常驻模式：只合并品种；多级别周期由 MONITOR_DAEMON_TIMEFRAMES 决定
                if get_settings().monitor_always_on:
                    pinned = sorted({symbol, *watch_syms, *self._daemon_symbols})
                    self.save_daemon_state(pinned)
                    asyncio.create_task(
                        self.start_always_on_workers(),
                        name="daemon-refresh",
                    )

            while True:
                msg = await websocket.receive_json()
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.info("client closed: %s", e)
        finally:
            worker.clients.discard(websocket)
            for ew in list(extras):
                ew.alert_clients.discard(websocket)
            if not worker.clients and not worker.alert_clients:
                asyncio.create_task(self._maybe_stop_worker(key, delay=15.0))
            for ew in list(extras):
                if not ew.clients and not ew.alert_clients:
                    asyncio.create_task(self._maybe_stop_worker(ew.key, delay=15.0))

    async def _attach_watchlist(
        self,
        websocket: WebSocket,
        timeframe: str,
        market: str,
        watch_syms: list[str],
        extras: list[StreamWorker],
    ) -> None:
        attached: list[str] = []
        for wsym in watch_syms:
            try:
                if websocket.client_state != WebSocketState.CONNECTED:
                    return
                # 监控固定 U 本位合约
                wmarket = "futures"

                ew = await self._ensure_worker(StreamKey(wsym, timeframe, wmarket))
                ew.alert_clients.add(websocket)
                extras.append(ew)
                attached.append(wsym)
            except Exception as e:
                logger.warning("watchlist skip %s: %s", wsym, e)

        if attached and websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_json(
                    {
                        "type": "status",
                        "message": f"后台观察已接入 {len(attached)} 个交易对",
                    }
                )
            except Exception:
                pass

    def _telegram_ready(self) -> bool:
        s = get_settings()
        return bool(s.telegram_bot_token.strip() and s.telegram_chat_id.strip())

    def _tg_rule_allowed(self, rule: str | None) -> bool:
        """是否允许该规则推 Telegram。页面告警不受此限制。"""
        allowed = get_settings().tg_trade_rules_set
        if allowed is None:
            return True
        return (rule or "").strip().lower() in allowed

    def _outlook_tg_state_path(self) -> Path:
        s = get_settings()
        return Path(s.data_cache_dir) / "cycle_outlook_tg.json"

    def _ai_confirm_cooldown_path(self) -> Path:
        s = get_settings()
        return Path(s.data_cache_dir) / "ai_confirm_cooldown.json"

    def _load_outlook_tg_day(self) -> str | None:
        path = self._outlook_tg_state_path()
        try:
            if not path.is_file():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            day = str(data.get("day") or "").strip()
            return day or None
        except Exception:
            return None

    def _save_outlook_tg_day(self, day: str) -> None:
        path = self._outlook_tg_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"day": day, "sent_at": datetime.now(timezone.utc).isoformat()},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("写入 cycle_outlook TG 日戳失败: %s", e)

    def _load_ai_confirm_cooldown(self) -> None:
        if self._ai_confirm_at:
            return
        path = self._ai_confirm_cooldown_path()
        try:
            if not path.is_file():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = data.get("at") or {}
            if isinstance(raw, dict):
                self._ai_confirm_at = {
                    str(k): float(v) for k, v in raw.items() if v is not None
                }
        except Exception:
            pass

    def _save_ai_confirm_cooldown(self) -> None:
        path = self._ai_confirm_cooldown_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 只保留最近 80 条
            items = sorted(self._ai_confirm_at.items(), key=lambda kv: kv[1])[-80:]
            self._ai_confirm_at = dict(items)
            path.write_text(
                json.dumps({"at": self._ai_confirm_at}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("写入 AI 确认冷却失败: %s", e)

    def _strategy_from_settings(self) -> DoubleLineConfig:
        s = get_settings()
        return DoubleLineConfig(
            kelly_scale=s.monitor_kelly_scale,
            stop_buffer_pct=s.monitor_stop_buffer_pct,
            stop_buffer_atr_mult=s.monitor_stop_buffer_atr_mult,
            take_profit_r=s.monitor_take_profit_r,
            max_chase_atr=s.monitor_max_chase_atr,
            ema_trend_period=s.monitor_ema_trend_period,
            require_ema200=s.monitor_require_ema200,
            require_ema_slope=s.monitor_require_ema_slope,
            trail_to_8r=s.monitor_trail_to_8r,
            require_fib_zone=s.monitor_require_fib_zone,
            require_volume=s.monitor_require_volume,
        )

    async def _ensure_worker(self, key: StreamKey) -> StreamWorker:
        # 快路径：已有 worker 不占锁久等
        existing = self._workers.get(str(key))
        if existing and existing.task and not existing.task.done():
            return existing

        # 拉 K 线放在锁外，避免全站串行卡死
        series = await asyncio.wait_for(
            asyncio.to_thread(
                fetch_candles,
                key.symbol,
                key.timeframe,
                300,
                False,
                key.market,
            ),
            timeout=25.0,
        )

        async with self._lock:
            existing = self._workers.get(str(key))
            if existing and existing.task and not existing.task.done():
                return existing

            worker = StreamWorker(
                key=key,
                series=series,
                strategy=self._strategy_from_settings(),
            )
            if key.market == "futures":
                seeded = await asyncio.to_thread(_fetch_contract_quote, key.symbol)
                if seeded:
                    worker.last_premium = seeded
                # 同品种只挂一条 mark，避免多级别重复连 + funding 连推
                if not self._symbol_has_mark_task(key.symbol):
                    worker.mark_task = asyncio.create_task(
                        self._run_mark_loop(worker),
                        name=f"mark-{key.symbol}",
                    )
            worker.task = asyncio.create_task(
                self._run_binance_loop(worker),
                name=f"monitor-{key}",
            )
            self._workers[str(key)] = worker
            logger.info("started stream worker %s bars=%d", key, len(series.candles))
            return worker

    async def _stop_worker(self, worker: StreamWorker) -> None:
        """停止单个 worker；若该品种无其它 mark 流则迁到剩余 worker。"""
        sym = worker.key.symbol
        market = worker.key.market
        had_mark = bool(worker.mark_task and not worker.mark_task.done())
        worker.stop.set()
        pending = [
            t for t in (worker.task, worker.mark_task) if t and not t.done()
        ]
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except asyncio.CancelledError:
                pass
        async with self._lock:
            self._workers.pop(str(worker.key), None)
        logger.info("stopped stream worker %s", worker.key)
        if had_mark and market == "futures" and not self._symbol_has_mark_task(sym):
            for w in self._workers.values():
                if w.key.symbol == sym and w.key.market == "futures":
                    w.mark_task = asyncio.create_task(
                        self._run_mark_loop(w),
                        name=f"mark-{sym}",
                    )
                    logger.info("moved mark stream to %s", w.key)
                    break

    async def _reconcile_daemon_workers(self) -> tuple[list[str], list[str]]:
        """按常驻列表增删 worker，无需重启进程。返回 (新启动, 已停止)。"""
        tfs = list(self._daemon_timeframes) or ["15m"]
        desired: set[str] = set()
        started: list[str] = []
        for sym in sorted(self._daemon_symbols):
            for tf in tfs:
                key = StreamKey(sym, tf, "futures")
                sk = str(key)
                desired.add(sk)
                prev = self._workers.get(sk)
                if prev and prev.task and not prev.task.done():
                    continue
                try:
                    await self._ensure_worker(key)
                    started.append(sk)
                except Exception as e:
                    logger.warning("daemon worker failed %s: %s", sk, e)

        stopped: list[str] = []
        orphans: list[StreamWorker] = []
        async with self._lock:
            for wkey, worker in list(self._workers.items()):
                if wkey in desired:
                    continue
                if worker.clients or worker.alert_clients:
                    logger.info(
                        "daemon reconcile: keep %s (browser still subscribed)", wkey
                    )
                    continue
                orphans.append(worker)
        for worker in orphans:
            await self._stop_worker(worker)
            stopped.append(str(worker.key))
        return started, stopped

    async def _maybe_stop_worker(self, key: StreamKey, delay: float) -> None:
        await asyncio.sleep(delay)
        async with self._lock:
            worker = self._workers.get(str(key))
            if not worker or worker.clients or worker.alert_clients:
                return
            if self.is_daemon_key(key):
                logger.debug("keep daemon worker alive %s", key)
                return
        await self._stop_worker(worker)

    async def _run_mark_loop(self, worker: StreamWorker) -> None:
        key = worker.key
        last_tick_at = 0.0
        try:
            async for prem in stream_mark_price(
                key.symbol, speed="1s", stop_event=worker.stop
            ):
                # WS 只有 mark/index/funding；保留 REST 的 24h 涨跌等字段
                merged = {**(worker.last_premium or {}), **prem}
                now = time.monotonic()
                if now - last_tick_at >= 60.0:
                    tick = await asyncio.to_thread(_fetch_ticker_24h, key.symbol)
                    if tick:
                        merged.update(tick)
                    last_tick_at = now
                worker.last_premium = merged
                # 同品种其它周期 worker 同步报价（UI / 规则状态共用）
                for other in self._workers.values():
                    if other is not worker and other.key.symbol == key.symbol:
                        other.last_premium = merged
                await self._broadcast(
                    worker,
                    {
                        "type": "premium",
                        "symbol": key.symbol,
                        **merged,
                    },
                )
                for other in self._workers.values():
                    if other is not worker and other.key.symbol == key.symbol:
                        await self._broadcast(
                            other,
                            {
                                "type": "premium",
                                "symbol": key.symbol,
                                **merged,
                            },
                        )
                mark = merged.get("mark_price")
                owns_mark = (
                    worker.mark_task is not None and not worker.mark_task.done()
                )
                if owns_mark and mark is not None:
                    try:
                        await self._paper_on_mark(worker, float(mark))
                    except Exception:
                        logger.exception("paper mark check failed %s", key.symbol)
                settings = get_settings()
                if settings.monitor_rules_enabled:
                    # funding/溢价与 K 线周期无关：只由挂 mark 的那条 worker 告警
                    if not owns_mark:
                        continue
                    try:
                        events, st = evaluate_premium_rules(
                            merged, worker.rule_state, self._rule_config()
                        )
                        worker.rule_state = st
                        for ev in events:
                            ra = rule_event_to_alert(
                                key.symbol, self._primary_daemon_tf, ev
                            )
                            await self._emit_rule_alert(worker, ra)
                    except Exception:
                        logger.exception("premium rules failed %s", key.symbol)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mark price loop crashed %s", key.symbol)

    async def _broadcast(
        self,
        worker: StreamWorker,
        payload: dict[str, Any],
        *,
        alert_also: bool = False,
    ) -> None:
        targets = set(worker.clients)
        if alert_also:
            targets |= worker.alert_clients
        dead: list[WebSocket] = []
        for ws in list(targets):
            try:
                if ws.client_state != WebSocketState.CONNECTED:
                    dead.append(ws)
                    continue
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            worker.clients.discard(ws)
            worker.alert_clients.discard(ws)

    def _upsert(self, worker: StreamWorker, candle: Candle) -> None:
        candles = worker.series.candles
        if not candles:
            candles.append(candle)
            return
        last = candles[-1]
        if last.timestamp == candle.timestamp:
            candles[-1] = candle
        elif candle.timestamp > last.timestamp:
            candles.append(candle)
            if len(candles) > 500:
                del candles[: len(candles) - 500]

    async def _run_binance_loop(self, worker: StreamWorker) -> None:
        key = worker.key
        logger.info("Binance K 线流开始 %s", key)
        try:
            async for candle, is_closed in stream_klines(
                key.symbol,
                key.timeframe,
                market=key.market,
                stop_event=worker.stop,
            ):
                worker.last_tick_at = datetime.utcnow()
                self._upsert(worker, candle)
                await self._broadcast(
                    worker,
                    {
                        "type": "kline",
                        "symbol": key.symbol,
                        "timeframe": key.timeframe,
                        "market": key.market,
                        "closed": is_closed,
                        "candle": candle_to_dict(candle),
                    },
                )
                if not is_closed:
                    continue
                worker.last_closed_at = candle.timestamp
                worker.closed_bars += 1
                await self._evaluate_and_alert(worker)
                try:
                    close_px = float(candle.close)
                    await self._paper_on_mark(worker, close_px)
                except Exception:
                    logger.exception("paper close-bar check failed %s", worker.key)
        except asyncio.CancelledError:
            logger.info("Binance K 线流取消 %s", key)
            raise
        except Exception:
            logger.exception("binance loop crashed %s", key)
            await self._broadcast(
                worker,
                {"type": "status", "message": "Binance 流异常，正在重连…", "level": "error"},
                alert_also=True,
            )

    def _rule_config(self) -> RuleConfig:
        s = get_settings()
        return RuleConfig(
            enable_macd=s.monitor_rule_macd,
            enable_ema_stack=s.monitor_rule_ema_stack,
            enable_boll=s.monitor_rule_boll,
            enable_volume=s.monitor_rule_volume,
            enable_structure_touch=s.monitor_rule_structure_touch,
            enable_structure_flip=s.monitor_rule_structure_flip,
            enable_fib_zone=s.monitor_rule_fib_zone,
            enable_baseline=s.monitor_rule_baseline,
            enable_break_level=s.monitor_rule_break_level,
            enable_funding=s.monitor_rule_funding,
            enable_premium=s.monitor_rule_premium,
            funding_extreme_pct=s.monitor_funding_extreme_pct,
            premium_extreme_pct=s.monitor_premium_extreme_pct,
            volume_spike_ratio=s.monitor_volume_spike_ratio,
            touch_cooldown_bars=s.monitor_touch_cooldown_bars,
        )

    async def _btc_candles_for_regime(self, timeframe: str) -> list[Candle]:
        """构建牛熊判定用的 BTC K 线（优先复用已有 worker）。"""
        btc_key = str(StreamKey("BTC/USDT", timeframe, "futures"))
        w = self._workers.get(btc_key)
        if w and len(w.series.candles) >= 300:
            return w.series.candles
        series = await asyncio.to_thread(
            fetch_candles_history,
            "BTC/USDT",
            timeframe,
            days=1825,
            market="futures",
            use_cache=True,
        )
        return series.candles

    async def _evaluate_cycle_switch(self, worker: StreamWorker) -> bool:
        """配置周期收盘评估 cycle_switch（每个盯盘币对各自跑）。

        - 牛熊相位仍由 BTC 定调；各币用自身 K 线做唐奇安/反弹做空等执行
        - 相对上一根 K 仓位变化 → 页面告警 + AI 候选点评（不直推 TG）
        - 纸面直接跟目标仓位；AI 不开仓
        - 「每天提醒周期位置」见 cycle_outlook
        """
        from analyst.compute.cycle_theory import (
            calendar_countdown_dict,
            format_milestone_countdown,
            wolfy_calendar_phase,
        )

        settings = get_settings()
        if not settings.monitor_cycle_switch_enabled:
            return False
        tf = (settings.monitor_cycle_switch_timeframe or "4h").strip().lower()
        if worker.key.timeframe.lower() != tf:
            return False
        allow = settings.cycle_symbols_set
        sym_u = worker.key.symbol.upper().replace("-", "/").split(":")[0]
        if allow is not None and sym_u not in allow:
            # 白名单外：清掉已有纸面 cycle 仓，不再发仓位告警
            try:
                if len(worker.series.candles) >= 1:
                    px = float(worker.series.candles[-1].close)
                    await self._paper_sync_cycle(
                        worker,
                        target_position=0.0,
                        price=px,
                        regime="filtered",
                    )
            except Exception:
                logger.exception(
                    "paper cycle flat failed (filtered) %s", worker.key
                )
            worker.last_cycle_position = 0.0
            return False
        if len(worker.series.candles) < 50:
            return False

        btc_candles = await self._btc_candles_for_regime(tf)
        if len(btc_candles) < 300:
            return False
        self._cycle_regime = build_cycle_regime(btc_candles)
        signal = evaluate_cycle_switch(
            worker.series,
            self._cycle_regime,
            prev_position=worker.last_cycle_position,
        )
        worker.last_cycle_position = signal.target_position

        await self._broadcast(
            worker,
            {
                "type": "cycle_switch",
                "symbol": worker.key.symbol,
                "timeframe": worker.key.timeframe,
                "market_regime": signal.market_regime,
                "calendar_phase": signal.calendar_phase,
                "target_position": signal.target_position,
                "prev_position": signal.prev_position,
                "price": signal.price,
                "reasons": signal.reasons,
                "z_score": signal.z_score,
                "donchian_entry": signal.donchian_entry,
                "donchian_exit": signal.donchian_exit,
            },
        )

        if not signal.changed:
            return False

        last_ts = worker.series.candles[-1].timestamp
        if last_ts.tzinfo is None:
            marker = int(last_ts.replace(tzinfo=timezone.utc).timestamp())
        else:
            marker = int(last_ts.astimezone(timezone.utc).timestamp())
        wolfy_cal = wolfy_calendar_phase(btc_candles[-1].timestamp)
        countdown_line = format_milestone_countdown(wolfy_cal)
        sym = worker.key.symbol
        alert = {
            "type": "alert",
            "rule": "cycle_switch",
            "title": (
                f"周期策略 · 距转折点 {wolfy_cal.days_to_milestone} 天"
            ),
            "symbol": sym,
            "timeframe": worker.key.timeframe,
            "direction": (
                "long" if signal.target_position > 0
                else ("short" if signal.target_position < 0 else "wait")
            ),
            "strength": 0.85,
            "price": signal.price,
            "reasons": [countdown_line, *signal.reasons],
            "cycle_countdown": calendar_countdown_dict(wolfy_cal),
            "marker_time": marker,
        }
        dedupe = (
            f"cycle|{sym}|{worker.key.timeframe}|"
            f"{signal.target_position}|{marker}"
        )
        if dedupe == worker.last_alert_key:
            return True
        worker.last_alert_key = dedupe
        worker.alerts_sent += 1
        self._alerts.append(alert)
        await self._broadcast(worker, alert, alert_also=True)
        # 不直推 TG：等 AI 确认可交易后再走 ai_plan
        logger.info(
            "cycle_switch 仓位变化仅页面+AI候选 %s %s pos %.2f→%.2f regime=%s",
            sym,
            worker.key.timeframe,
            signal.prev_position,
            signal.target_position,
            signal.market_regime,
        )
        try:
            await self._paper_sync_cycle(
                worker,
                target_position=signal.target_position,
                price=float(signal.price),
                regime=signal.market_regime,
            )
        except Exception:
            logger.exception("paper cycle sync failed %s", worker.key)
        return True

    async def _evaluate_cycle_outlook(self, worker: StreamWorker) -> None:
        """BTC：每天提醒一次当前周期位置（UTC 日限 1 条；与各币 cycle_switch 交易点分离）。"""
        settings = get_settings()
        if not settings.monitor_cycle_outlook_enabled:
            return
        tf = (settings.monitor_cycle_switch_timeframe or "4h").strip().lower()
        if worker.key.timeframe.lower() != tf:
            return
        if worker.key.symbol.upper() != "BTC/USDT":
            return

        from analyst.compute.cycle_theory import (
            calendar_countdown_dict,
            evaluate_cycle_outlook,
            format_outlook_text,
        )

        series_1d = await asyncio.to_thread(
            fetch_candles_history,
            "BTC/USDT",
            "1d",
            days=800,
            market="futures",
            use_cache=True,
        )
        outlook = evaluate_cycle_outlook(series_1d)
        if not outlook.summary and not outlook.alerts:
            return

        # 按 UTC 日期限流：一天只推一次（内存 + 落盘）——这是「周期位置」日更
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        if day == self._last_outlook_key or day == self._load_outlook_tg_day():
            return
        self._last_outlook_key = day

        alert = {
            "type": "alert",
            "rule": "cycle_outlook",
            "title": (
                f"周期位置 · 距转折点 {outlook.calendar.days_to_milestone} 天"
            ),
            "symbol": worker.key.symbol,
            "timeframe": worker.key.timeframe,
            "direction": "info",
            "strength": 0.7,
            "price": outlook.price,
            "reasons": ([outlook.summary] if outlook.summary else [])
            + list(outlook.alerts[:5]),
            "cycle_countdown": calendar_countdown_dict(outlook.calendar),
            "marker_time": int(
                outlook.as_of.replace(tzinfo=timezone.utc).timestamp()
                if outlook.as_of.tzinfo is None
                else outlook.as_of.astimezone(timezone.utc).timestamp()
            ),
        }
        worker.alerts_sent += 1
        self._alerts.append(alert)
        await self._broadcast(worker, alert, alert_also=True)

        if not self._telegram_ready():
            self._save_outlook_tg_day(day)
            return

        notifier = build_default_notifier(
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
        )
        logger.info(
            "cycle_outlook 日更 → TG days_left=%d",
            outlook.calendar.days_to_milestone,
        )
        await asyncio.to_thread(
            notifier.send_text,
            format_outlook_text(outlook, worker.key.symbol),
        )
        self._save_outlook_tg_day(day)

    async def _emit_rule_alert(self, worker: StreamWorker, alert: dict[str, Any]) -> None:
        dedupe = (
            f"{alert.get('rule')}|{alert['symbol']}|{alert['timeframe']}|"
            f"{alert.get('direction')}|{alert.get('marker_time')}|"
            f"{(alert.get('title') or '')[:24]}"
        )
        if dedupe in worker.last_rule_keys:
            return
        worker.last_rule_keys.add(dedupe)
        if len(worker.last_rule_keys) > 120:
            # 简单裁剪：重建为最近风格集合
            worker.last_rule_keys = set(list(worker.last_rule_keys)[-80:])

        self._alerts.append(alert)
        worker.alerts_sent += 1
        await self._broadcast(worker, alert, alert_also=True)

        # 噪音规则只上页面，不刷 Telegram
        rule = str(alert.get("rule") or "")
        if not self._tg_rule_allowed(rule):
            logger.debug(
                "规则告警仅页面 rule=%s %s %s（未进 TG 白名单）",
                rule,
                worker.key.symbol,
                worker.key.timeframe,
            )
            return
        if not self._telegram_ready():
            return

        settings = get_settings()
        notifier = build_default_notifier(
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
        )
        text = format_rule_alert_text(
            worker.key.symbol, worker.key.timeframe, alert
        )
        logger.info(
            "规则告警 → TG rule=%s %s %s dir=%s",
            rule,
            worker.key.symbol,
            worker.key.timeframe,
            alert.get("direction"),
        )
        await asyncio.to_thread(notifier.send_text, text)

    async def _maybe_ai_confirm(
        self,
        worker: StreamWorker,
        *,
        had_candidate: bool,
        candidate_rules: list[str],
    ) -> None:
        """有双线/规则候选时调 AI；long/short 推盯盘点评（页面+TG），不开纸面仓。"""
        settings = get_settings()
        if not settings.monitor_ai_on_candidate or not had_candidate:
            return

        from analyst.training.session import map_monitor_tf_to_ai_tf, run_monitor_ai_confirm

        ai_tf = map_monitor_tf_to_ai_tf(worker.key.timeframe)
        cool_key = f"{worker.key.symbol.upper()}|{ai_tf}"
        self._load_ai_confirm_cooldown()
        cooldown_min = max(0, int(settings.monitor_ai_cooldown_minutes or 0))
        now = time.time()
        last = self._ai_confirm_at.get(cool_key)
        if last is not None and cooldown_min > 0 and (now - last) < cooldown_min * 60:
            logger.debug(
                "AI 确认冷却中 %s remain=%.0fs",
                cool_key,
                cooldown_min * 60 - (now - last),
            )
            return

        # 先占冷却，避免并发收盘重复打
        self._ai_confirm_at[cool_key] = now
        self._save_ai_confirm_cooldown()

        logger.info(
            "AI 候选确认开始 %s → ai_tf=%s rules=%s",
            worker.key,
            ai_tf,
            candidate_rules[:6],
        )
        try:
            result = await asyncio.to_thread(
                run_monitor_ai_confirm,
                worker.key.symbol,
                worker.key.timeframe,
                market="futures",
            )
        except Exception as e:
            logger.exception("AI 候选确认失败 %s", worker.key)
            await self._emit_ai_confirm_failure(
                worker,
                ai_tf=ai_tf,
                candidate_rules=candidate_rules,
                error=e,
            )
            return

        direction = str(result.get("direction") or "wait").lower()
        if direction not in ("long", "short"):
            logger.info(
                "AI 候选确认 wait %s dir=%s model=%s",
                worker.key,
                direction,
                result.get("model_id"),
            )
            return

        plan = result.get("plan") or {}
        rationale = str(result.get("rationale") or plan.get("rationale") or "")
        price = result.get("price")
        if price is None and worker.series.candles:
            price = worker.series.candles[-1].close
        last_ts = worker.series.candles[-1].timestamp if worker.series.candles else None
        if last_ts is None:
            marker = int(datetime.now(timezone.utc).timestamp())
        elif last_ts.tzinfo is None:
            marker = int(last_ts.replace(tzinfo=timezone.utc).timestamp())
        else:
            marker = int(last_ts.astimezone(timezone.utc).timestamp())

        reasons = [
            f"候选规则: {', '.join(candidate_rules[:4]) or 'double_line'}",
            f"AI 周期 {result.get('ai_timeframe') or ai_tf}",
        ]
        if rationale:
            reasons.append(rationale[:240])

        alert = {
            "type": "alert",
            "rule": "ai_plan",
            "title": "AI 盯盘点评",
            "symbol": worker.key.symbol,
            "timeframe": worker.key.timeframe,
            "direction": direction,
            "strength": 0.9,
            "price": price,
            "pattern": "ai_plan",
            "break_level": None,
            "reasons": reasons,
            "filters_passed": ["ai_plan", *candidate_rules[:4]],
            "marker_time": marker,
            "plan": plan,
            "kelly": None,
            "trail_note": None,
            "session_id": result.get("session_id"),
            "model_id": result.get("model_id"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "demo": False,
            "paper_trade": False,
        }
        logger.info(
            "AI 盯盘点评 %s dir=%s session=%s（仅提醒，不开纸面）",
            worker.key,
            direction,
            result.get("session_id"),
        )
        await self._emit_rule_alert(worker, alert)
        # ai_plan 不再开纸面仓：只作监控点评/通知；开仓仅 double_line / cycle_switch


    async def _paper_try_open(
        self,
        worker: StreamWorker,
        alert: dict[str, Any],
        *,
        strategy: str,
    ) -> None:
        settings = get_settings()
        if not settings.monitor_paper_enabled:
            return
        from analyst.trading.paper import get_paper_broker

        plan = alert.get("plan")
        if not isinstance(plan, dict):
            return
        price = alert.get("price")
        if price is None:
            return
        broker = get_paper_broker()
        event = await asyncio.to_thread(
            broker.try_open_from_plan,
            symbol=alert["symbol"],
            timeframe=alert.get("timeframe") or worker.key.timeframe,
            direction=alert.get("direction") or "",
            price=float(price),
            plan=plan,
            strategy=strategy,
            session_id=alert.get("session_id"),
            model_id=alert.get("model_id"),
        )
        if not event:
            return
        await self._broadcast(worker, event, alert_also=True)
        if settings.monitor_paper_tg and self._telegram_ready():
            pos = event.get("position") or {}
            dir_zh = "多" if pos.get("direction") == "long" else "空"
            text = (
                f"📄 纸面开{dir_zh} · [{pos.get('strategy') or strategy}] "
                f"{pos.get('symbol')} {pos.get('timeframe')}\n"
                f"entry={pos.get('entry')} SL={pos.get('stop_loss')} TP={pos.get('take_profit')}\n"
                f"qty={pos.get('qty')} · equity={event.get('equity')}\n"
                f"模拟盘 · 非真金"
            )
            await self._notify_telegram_text(text)

    async def _paper_sync_cycle(
        self,
        worker: StreamWorker,
        *,
        target_position: float,
        price: float,
        regime: str | None,
    ) -> None:
        settings = get_settings()
        if not settings.monitor_paper_enabled:
            return
        from analyst.trading.paper import get_paper_broker

        broker = get_paper_broker()
        events = await asyncio.to_thread(
            broker.sync_cycle_target,
            symbol=worker.key.symbol,
            timeframe=worker.key.timeframe,
            target_position=float(target_position),
            price=float(price),
            regime=regime,
        )
        for ev in events:
            await self._broadcast(worker, ev, alert_also=True)
            if settings.monitor_paper_tg and self._telegram_ready():
                if ev.get("type") == "paper_open":
                    pos = ev.get("position") or {}
                    dir_zh = "多" if pos.get("direction") == "long" else "空"
                    text = (
                        f"📄 纸面开{dir_zh} · [cycle_switch] {pos.get('symbol')}\n"
                        f"target={pos.get('target_weight')} entry={pos.get('entry')}\n"
                        f"equity={ev.get('equity')} · 模拟盘"
                    )
                else:
                    tr = ev.get("trade") or {}
                    text = (
                        f"📄 纸面平仓 · [cycle_switch] {tr.get('symbol')} SIGNAL\n"
                        f"pnl={tr.get('pnl_usd')} · equity={ev.get('equity')} · 模拟盘"
                    )
                await self._notify_telegram_text(text)

    async def _paper_on_mark(self, worker: StreamWorker, mark_price: float) -> None:
        settings = get_settings()
        if not settings.monitor_paper_enabled:
            return
        from analyst.trading.paper import get_paper_broker

        broker = get_paper_broker()
        events = await asyncio.to_thread(
            broker.on_mark, worker.key.symbol, float(mark_price)
        )
        for ev in events:
            await self._broadcast(worker, ev, alert_also=True)
            if settings.monitor_paper_tg and self._telegram_ready():
                tr = ev.get("trade") or {}
                outcome = tr.get("outcome") or "?"
                text = (
                    f"📄 纸面平仓 · [{tr.get('strategy') or '?'}] "
                    f"{tr.get('symbol')} {outcome.upper()}\n"
                    f"{tr.get('direction')} entry={tr.get('entry')} → exit={tr.get('exit')}\n"
                    f"pnl={tr.get('pnl_usd')} · equity={ev.get('equity')}\n"
                    f"模拟盘 · 非真金"
                )
                await self._notify_telegram_text(text)

    async def _emit_ai_confirm_failure(
        self,
        worker: StreamWorker,
        *,
        ai_tf: str,
        candidate_rules: list[str],
        error: Exception,
    ) -> None:
        """免费 AI 确认失败：上页面 + 推 TG（不回落付费，需人知晓）。"""
        err = str(error).strip() or error.__class__.__name__
        if len(err) > 280:
            err = err[:277] + "..."
        price = None
        if worker.series.candles:
            price = worker.series.candles[-1].close
        marker = int(datetime.now(timezone.utc).timestamp())
        alert = {
            "type": "alert",
            "rule": "ai_confirm_fail",
            "title": "免费 AI 确认失败",
            "symbol": worker.key.symbol,
            "timeframe": worker.key.timeframe,
            "direction": "info",
            "strength": 0.5,
            "price": price,
            "pattern": "ai_confirm_fail",
            "break_level": None,
            "reasons": [
                f"分析周期 {ai_tf}（仅 Groq，未回落付费）",
                f"候选: {', '.join(candidate_rules[:4]) or '-'}",
                err,
            ],
            "filters_passed": ["ai_confirm_fail"],
            "marker_time": marker,
            "plan": None,
            "kelly": None,
            "trail_note": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "demo": False,
        }
        # 页面：走去重广播；TG：直推（不受可交易白名单限制）
        dedupe = (
            f"ai_fail|{worker.key.symbol}|{worker.key.timeframe}|"
            f"{marker // 1800}"  # 同半小时内同币同周期只记一条页面
        )
        if dedupe not in worker.last_rule_keys:
            worker.last_rule_keys.add(dedupe)
            self._alerts.append(alert)
            worker.alerts_sent += 1
            await self._broadcast(worker, alert, alert_also=True)

        if self._telegram_ready():
            text = (
                f"⚠️ 免费 AI 确认失败 · {worker.key.symbol} {worker.key.timeframe}\n"
                f"分析周期 {ai_tf} · 仅 Groq，未用付费模型\n"
                f"候选: {', '.join(candidate_rules[:4]) or '-'}\n"
                f"{err}"
            )
            logger.warning("AI 确认失败 → TG %s", worker.key)
            await self._notify_telegram_text(text)

    async def _evaluate_and_alert(self, worker: StreamWorker) -> None:
        signal = evaluate_double_line(worker.series, worker.strategy)
        worker.last_signal_dir = signal.direction
        logger.info(
            "收盘评估 %s price=%.6g dir=%s pattern=%s closed_bars=%d reasons=%s",
            worker.key,
            signal.price,
            signal.direction,
            signal.pattern or "-",
            worker.closed_bars,
            (signal.reasons or [])[:2],
        )
        await self._broadcast(
            worker,
            {
                "type": "signal",
                "symbol": worker.key.symbol,
                "timeframe": worker.key.timeframe,
                "direction": signal.direction,
                "pattern": signal.pattern,
                "break_level": signal.break_level,
                "price": signal.price,
                "reasons": signal.reasons[:3],
            },
        )
        had_candidate = signal.direction != "wait"
        candidate_rules: list[str] = ["double_line"] if had_candidate else []

        if signal.direction != "wait":
            alert = signal_to_alert_dict(
                worker.key.symbol,
                worker.key.timeframe,
                signal,
            )
            alert["rule"] = "double_line"
            alert["title"] = "双线反转"
            dedupe = (
                f"{alert['symbol']}|{alert['timeframe']}|{alert['direction']}|"
                f"{alert['marker_time']}"
            )
            if dedupe != worker.last_alert_key:
                worker.last_alert_key = dedupe
                self._alerts.append(alert)
                worker.alerts_sent += 1
                await self._broadcast(worker, alert, alert_also=True)

                # 双线仅页面；TG 改由 AI 确认（ai_plan）
                if self._tg_rule_allowed("double_line") and self._telegram_ready():
                    settings = get_settings()
                    notifier = build_default_notifier(
                        telegram_bot_token=settings.telegram_bot_token,
                        telegram_chat_id=settings.telegram_chat_id,
                    )
                    logger.info(
                        "双线反转告警 → TG %s %s dir=%s strength=%.2f",
                        worker.key.symbol,
                        worker.key.timeframe,
                        signal.direction,
                        signal.strength,
                    )
                    await asyncio.to_thread(
                        notifier.notify,
                        worker.key.symbol,
                        worker.key.timeframe,
                        signal,
                    )
                # 纸面跟单双线（有 plan 时）
                try:
                    await self._paper_try_open(worker, alert, strategy="double_line")
                except Exception:
                    logger.exception("paper double_line open failed %s", worker.key)

        # 规则批次（页面全量；TG 受白名单限制，默认不含噪音规则）
        settings = get_settings()
        if settings.monitor_rules_enabled:
            try:
                events, new_state = evaluate_closed_bar_rules(
                    worker.series, worker.rule_state, self._rule_config()
                )
                worker.rule_state = new_state
                if events:
                    had_candidate = True
                    for e in events:
                        r = getattr(e, "rule", None) or str(e)
                        if r not in candidate_rules:
                            candidate_rules.append(r)
                    logger.info(
                        "规则命中 %s n=%d rules=%s",
                        worker.key,
                        len(events),
                        [getattr(e, "rule", str(e)) for e in events[:5]],
                    )
                for ev in events:
                    ra = rule_event_to_alert(
                        worker.key.symbol, worker.key.timeframe, ev
                    )
                    await self._emit_rule_alert(worker, ra)
            except Exception:
                logger.exception("rule evaluate failed %s", worker.key)

        try:
            cycle_candidate = await self._evaluate_cycle_switch(worker)
            if cycle_candidate:
                had_candidate = True
                if "cycle_switch" not in candidate_rules:
                    candidate_rules.append("cycle_switch")
        except Exception:
            logger.exception("cycle_switch evaluate failed %s", worker.key)

        try:
            await self._evaluate_cycle_outlook(worker)
        except Exception:
            logger.exception("cycle_outlook evaluate failed %s", worker.key)

        try:
            await self._maybe_ai_confirm(
                worker,
                had_candidate=had_candidate,
                candidate_rules=candidate_rules,
            )
        except Exception:
            logger.exception("ai confirm failed %s", worker.key)

    async def inject_demo_alert(
        self,
        *,
        symbol: str = "BTC/USDT",
        timeframe: str = "15m",
        direction: str = "long",
        market: str = "futures",
    ) -> dict[str, Any]:
        """注入一条模拟可交易告警：推页面 WS + Telegram（便于联调）。"""
        from analyst.compute.kelly import KellySize
        from analyst.compute.plan import TradePlan
        from analyst.compute.strategies.double_line_reversal import DoubleLineSignal

        symbol = _norm_symbol(symbol)
        direction = direction if direction in ("long", "short") else "long"
        market = market if market in ("spot", "futures") else "futures"
        key = StreamKey(symbol, timeframe, market)
        worker = self._workers.get(str(key))

        price = 0.0
        marker_ts = datetime.now(timezone.utc)
        if worker and worker.series.candles:
            last = worker.series.candles[-1]
            price = float(last.close)
            marker_ts = last.timestamp
        elif worker and worker.last_premium and worker.last_premium.get("mark_price"):
            price = float(worker.last_premium["mark_price"])
        else:
            # 无活跃流时用 REST 拉一根
            series = await asyncio.to_thread(
                fetch_candles, symbol, timeframe, 5, False, market
            )
            if series.candles:
                last = series.candles[-1]
                price = float(last.close)
                marker_ts = last.timestamp

        if not price:
            price = 65000.0 if symbol.startswith("BTC") else 1.0

        # 按方向造一份看得见的计划
        if direction == "long":
            entry_low, entry_high = price * 0.998, price * 1.002
            stop = price * 0.985
            tp1 = price * 1.03
        else:
            entry_low, entry_high = price * 0.998, price * 1.002
            stop = price * 1.015
            tp1 = price * 0.97
        risk = abs(price - stop) or price * 0.01
        rr = abs(tp1 - price) / risk

        plan = TradePlan(
            direction=direction,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop,
            take_profit_1=tp1,
            take_profit_2=None,
            rr_ratio=rr,
            rationale="【模拟告警】仅用于联调页面标记与 Telegram，非实盘信号",
        )
        signal = DoubleLineSignal(
            direction=direction,
            strength=0.86,
            price=price,
            pattern="demo_double_line",
            break_level=entry_high if direction == "long" else entry_low,
            reasons=[
                "【模拟】双线反转形态命中",
                "【模拟】放量确认",
                "【模拟】趋势过滤通过",
            ],
            filters_passed=["demo"],
            plan=plan,
            kelly=KellySize(
                win_rate=0.47,
                payoff_ratio=2.0,
                full_kelly=0.2,
                fraction=0.25,
                suggested_fraction=0.05,
                risk_budget_pct=1.0,
                note="模拟 Kelly",
            ),
            trail_note="【模拟】可按 2R 分批止盈",
            bar_ts=marker_ts,
        )

        alert = signal_to_alert_dict(symbol, timeframe, signal)
        alert["demo"] = True
        self._alerts.append(alert)

        # 广播到所有连着该品种的客户端；若无 worker 则广播给任意 BTC 流客户端
        targets_worker = worker
        if targets_worker is None:
            for w in self._workers.values():
                if w.key.symbol == symbol:
                    targets_worker = w
                    break
        if targets_worker is not None:
            await self._broadcast(targets_worker, alert, alert_also=True)
        else:
            # 推给任意已连接客户端（告警列表 / toast），哪怕品种不完全相同
            pushed = False
            for w in self._workers.values():
                if w.clients or w.alert_clients:
                    await self._broadcast(w, alert, alert_also=True)
                    pushed = True
                    break
            if not pushed:
                logger.info("demo alert stored (no live WS clients): %s", symbol)

        settings = get_settings()
        notifier = build_default_notifier(
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
        )
        await asyncio.to_thread(notifier.notify, symbol, timeframe, signal)
        return alert


_hub: MonitorHub | None = None


def get_monitor_hub() -> MonitorHub:
    global _hub
    if _hub is None:
        _hub = MonitorHub()
    return _hub
