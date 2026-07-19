"""规则告警降噪逻辑测试（volume 门槛 / 触及冷却参数）。"""

from datetime import datetime, timedelta

from analyst.data.fetcher import Candle, CandleSeries
from analyst.monitor.rules import RuleConfig, evaluate_closed_bar_rules


def _c(i: int, o: float, h: float, l: float, c: float, v: float = 1000) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1) + timedelta(minutes=15 * i),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
    )


def _series(candles: list[Candle]) -> CandleSeries:
    return CandleSeries(symbol="BTC/USDT", timeframe="15m", candles=candles)


def _flat(n: int, base: float = 100.0) -> list[Candle]:
    # 略带抖动，避免 ATR/结构全为零
    return [
        _c(i, base, base + 0.3, base - 0.3, base + (0.1 if i % 2 else -0.1))
        for i in range(n)
    ]


def _only_volume_cfg() -> RuleConfig:
    return RuleConfig(
        enable_macd=False,
        enable_ema_stack=False,
        enable_boll=False,
        enable_structure_touch=False,
        enable_structure_flip=False,
        enable_fib_zone=False,
        enable_baseline=False,
    )


def test_volume_spike_with_body_fires():
    candles = _flat(60)
    # 放量 3× + 大实体阳线
    candles.append(_c(60, 100, 102.2, 99.9, 102, v=3000))
    events, _ = evaluate_closed_bar_rules(_series(candles), {}, _only_volume_cfg())
    vol_events = [e for e in events if e.rule == "volume"]
    assert vol_events, "放量+大实体应触发"
    assert vol_events[0].direction == "long"


def test_volume_moderate_spike_filtered():
    candles = _flat(60)
    # 旧阈值 1.5× 会报；新默认 2.0× 应过滤
    candles.append(_c(60, 100, 102.2, 99.9, 102, v=1600))
    events, _ = evaluate_closed_bar_rules(_series(candles), {}, _only_volume_cfg())
    assert not [e for e in events if e.rule == "volume"]


def test_volume_spike_without_body_filtered():
    candles = _flat(60)
    # 放量 3× 但十字星（无实体、无背离）→ 无方向意义，不报
    candles.append(_c(60, 100, 100.4, 99.6, 100.02, v=3000))
    events, _ = evaluate_closed_bar_rules(_series(candles), {}, _only_volume_cfg())
    assert not [e for e in events if e.rule == "volume"]
