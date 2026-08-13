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


def test_passes_trend_filters_adx_and_htf():
    from analyst.monitor.rules import RuleConfig, passes_trend_filters

    chop = RuleConfig(adx_min_trend=18, htf_bias="mixed")
    assert passes_trend_filters("long", adx=12, cfg=chop, adx_ready=True) is False
    assert passes_trend_filters("long", adx=25, cfg=chop, adx_ready=True) is True
    assert passes_trend_filters("long", adx=0, cfg=chop, adx_ready=False) is True
    assert passes_trend_filters("long", adx=0, cfg=chop, adx_ready=True) is False

    vs_bear = RuleConfig(adx_min_trend=0, htf_bias="bear")
    assert passes_trend_filters("long", adx=40, cfg=vs_bear) is False
    assert passes_trend_filters("short", adx=40, cfg=vs_bear) is True


def test_htf_bear_filters_bull_boll_break():
    """更高周期空头时，布林上轨突破不当做多信号。"""
    candles = _flat(50)
    # 抬升一段再收盘突破：构造带宽内 → 带宽外
    for i in range(10):
        px = 100 + i * 0.4
        candles.append(_c(50 + i, px, px + 0.5, px - 0.2, px + 0.3, v=2000))
    last = 108
    candles.append(_c(60, last, last + 8, last - 0.2, last + 7.5, v=4000))
    cfg = RuleConfig(
        enable_macd=False,
        enable_ema_stack=False,
        enable_volume=False,
        enable_structure_touch=False,
        enable_structure_flip=False,
        enable_fib_zone=False,
        enable_baseline=False,
        enable_cvd=False,
        adx_min_trend=0,
        htf_bias="bear",
        boll_min_vol_ratio=0.5,
        boll_atr_margin=0.0,
    )
    events, _ = evaluate_closed_bar_rules(_series(candles), {}, cfg)
    assert not [e for e in events if e.rule == "boll_break" and e.direction == "long"]


def test_is_ai_candidate_quality_gate():
    from analyst.monitor.rules import is_ai_candidate

    assert is_ai_candidate(["volume"]) is False
    assert is_ai_candidate(["structure_touch"]) is False
    assert is_ai_candidate(["macd_cross"]) is True
    assert is_ai_candidate(["cycle_switch"]) is True
    assert is_ai_candidate(["volume", "structure_touch"]) is True


def test_cycle_chop_blocked_on_flat_open():
    """横盘假突破开多：ADX 低 → chop_blocked。"""
    from analyst.compute.strategies.cycle_switch import (
        CycleSwitchConfig,
        evaluate_cycle_switch,
    )

    candles = []
    for i in range(80):
        w = 0.5 if i % 2 == 0 else -0.5
        candles.append(_c(i, 100, 100.7, 99.3, 100 + w))
    candles[-1] = _c(79, 100.2, 112, 100.0, 111.5)
    series = CandleSeries("BTC/USDT", "4h", candles)
    regime = {c.timestamp: "bull" for c in candles}
    sig = evaluate_cycle_switch(
        series,
        regime,
        cfg=CycleSwitchConfig(entry_n=20, exit_n=10, min_adx=20),
    )
    assert sig.target_position > 0
    assert sig.adx < 20
    assert sig.chop_blocked is True


def test_cycle_vol_target_suggested_size():
    """持仓时给出波动率目标化建议仓位：0.15 ≤ scale ≤ 1，方向一致。"""
    from analyst.compute.strategies.cycle_switch import (
        CycleSwitchConfig,
        evaluate_cycle_switch,
    )

    candles = [
        _c(i, 100 + i * 2, 103 + i * 2, 99 + i * 2, 102 + i * 2)
        for i in range(80)
    ]
    series = CandleSeries("BTC/USDT", "4h", candles)
    regime = {c.timestamp: "bull" for c in candles}
    sig = evaluate_cycle_switch(
        series, regime, cfg=CycleSwitchConfig(entry_n=20, exit_n=10, min_adx=0)
    )
    assert sig.target_position == 1.0
    assert 0.15 <= sig.vol_scale <= 1.0
    assert sig.suggested_size == sig.target_position * sig.vol_scale
    assert any("波动率目标化" in r for r in sig.reasons)

