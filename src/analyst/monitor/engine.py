"""实时监控引擎：REST 预热 + Binance WS + 双线反转评估 + 告警。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from analyst.compute.strategies.double_line_reversal import (
    DoubleLineConfig,
    DoubleLineSignal,
    evaluate_double_line,
)
from analyst.data.fetcher import Candle, CandleSeries, fetch_candles
from analyst.data.ws_kline import stream_klines
from analyst.monitor.notifier import Notifier, build_default_notifier

logger = logging.getLogger(__name__)


@dataclass
class MonitorConfig:
    symbol: str = "BTC/USDT"
    timeframe: str = "15m"
    market: str = "spot"                 # spot | futures
    history_limit: int = 200
    strategy: DoubleLineConfig = field(default_factory=DoubleLineConfig)
    alert_on_closed_only: bool = True
    dedupe: bool = True


class MonitorEngine:
    """订阅单品种单周期；仅在收盘 K 上发可交易提醒。"""

    def __init__(
        self,
        cfg: MonitorConfig,
        notifier: Notifier | None = None,
    ) -> None:
        self.cfg = cfg
        self.notifier = notifier or build_default_notifier()
        self.series: CandleSeries | None = None
        self._last_alert_key: str | None = None
        self._stop = asyncio.Event()

    def warm_up(self) -> CandleSeries:
        series = fetch_candles(
            self.cfg.symbol,
            timeframe=self.cfg.timeframe,
            limit=self.cfg.history_limit,
            use_cache=False,
        )
        self.series = series
        logger.info(
            "warm-up %s %s bars=%d last=%s",
            series.symbol,
            series.timeframe,
            len(series.candles),
            series.latest.close if series.candles else None,
        )
        return series

    def evaluate_once(self) -> DoubleLineSignal:
        if self.series is None:
            self.warm_up()
        assert self.series is not None
        return evaluate_double_line(self.series, self.cfg.strategy)

    def _upsert_candle(self, candle: Candle, is_closed: bool) -> None:
        assert self.series is not None
        candles = self.series.candles
        if not candles:
            candles.append(candle)
            return
        last = candles[-1]
        if last.timestamp == candle.timestamp:
            candles[-1] = candle
        elif candle.timestamp > last.timestamp:
            candles.append(candle)
            max_keep = max(self.cfg.history_limit, 300)
            if len(candles) > max_keep:
                del candles[: len(candles) - max_keep]

    def _maybe_alert(self, signal: DoubleLineSignal) -> None:
        if signal.direction == "wait":
            return
        key = f"{self.cfg.symbol}|{self.cfg.timeframe}|{signal.direction}|{signal.bar_ts}"
        if self.cfg.dedupe and key == self._last_alert_key:
            return
        self._last_alert_key = key
        self.notifier.notify(self.cfg.symbol, self.cfg.timeframe, signal)

    async def run(self) -> None:
        self.warm_up()
        boot = self.evaluate_once()
        logger.info(
            "boot signal dir=%s reasons=%s",
            boot.direction,
            boot.reasons[:2],
        )
        self._maybe_alert(boot)

        async for candle, is_closed in stream_klines(
            self.cfg.symbol,
            self.cfg.timeframe,
            market=self.cfg.market,
            stop_event=self._stop,
        ):
            self._upsert_candle(candle, is_closed)
            if self.cfg.alert_on_closed_only and not is_closed:
                continue
            signal = self.evaluate_once()
            logger.debug(
                "bar closed=%s price=%.4f dir=%s",
                is_closed,
                signal.price,
                signal.direction,
            )
            self._maybe_alert(signal)

    def stop(self) -> None:
        self._stop.set()


def run_monitor_blocking(cfg: MonitorConfig, notifier: Notifier | None = None) -> None:
    engine = MonitorEngine(cfg, notifier=notifier)
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        engine.stop()
