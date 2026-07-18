"""双线反转（K 线形态）测试。"""

from datetime import datetime, timedelta

from analyst.compute.strategies.double_line_reversal import (
    DoubleLineConfig,
    detect_pattern,
    evaluate_double_line,
)
from analyst.data.fetcher import Candle, CandleSeries


def _c(i: int, o: float, h: float, l: float, c: float, v: float = 1000) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1) + timedelta(minutes=15 * i),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
    )


def _pad_then(extras: list[Candle], n: int = 40, base: float = 100.0) -> CandleSeries:
    # 前置横盘，方便 ATR / EMA
    pad = [_c(i, base, base + 0.2, base - 0.2, base) for i in range(n)]
    candles = pad + [
        Candle(
            timestamp=datetime(2026, 1, 1) + timedelta(minutes=15 * (n + i)),
            open=e.open,
            high=e.high,
            low=e.low,
            close=e.close,
            volume=e.volume,
        )
        for i, e in enumerate(extras)
    ]
    # 修正 extras 的时间已在上面重写
    return CandleSeries(symbol="BTC/USDT", timeframe="15m", candles=candles)


def test_detect_bullish_double_line():
    # 强阴 + 强阳，重合大、振幅大
    extras = [
        _c(0, 100, 100.5, 96, 96.2),   # 大阴
        _c(1, 96.3, 100.4, 96.0, 100.2),  # 大阳，重合
    ]
    s = _pad_then(extras, n=30, base=100)
    # 手工拼两根到末尾覆盖 pad 逻辑：直接用 series 末两根
    cfg = DoubleLineConfig(
        min_body_ratio=0.5,
        min_overlap_ratio=0.4,
        min_sudden_atr_mult=0.5,
        require_ema200=False,
    )
    # 替换最后两根
    s.candles[-2] = Candle(
        timestamp=s.candles[-2].timestamp,
        open=100, high=100.5, low=96, close=96.2, volume=2000,
    )
    s.candles[-1] = Candle(
        timestamp=s.candles[-1].timestamp,
        open=96.3, high=100.4, low=96.0, close=100.2, volume=2500,
    )
    p = detect_pattern(s.candles, cfg)
    assert p is not None
    assert p.direction == "long"
    assert p.break_level == 100.5


def test_evaluate_waits_without_breakout():
    s = _pad_then([], n=50, base=100)
    s.candles[-2] = Candle(
        timestamp=s.candles[-2].timestamp,
        open=100, high=100.5, low=96, close=96.2, volume=2000,
    )
    s.candles[-1] = Candle(
        timestamp=s.candles[-1].timestamp,
        open=96.3, high=100.4, low=96.0, close=99.0, volume=2500,  # 未破 100.5
    )
    # 再追加一根未突破的收盘
    last = s.candles[-1]
    s.candles.append(
        Candle(
            timestamp=last.timestamp + timedelta(minutes=15),
            open=99.0, high=100.0, low=98.5, close=99.5, volume=1200,
        )
    )
    sig = evaluate_double_line(
        s,
        DoubleLineConfig(
            min_body_ratio=0.5,
            min_overlap_ratio=0.4,
            min_sudden_atr_mult=0.5,
            require_ema200=False,
            require_volume=False,
            require_adx=False,
            use_conditional_edge=False,
        ),
    )
    assert sig.direction == "wait"
    assert sig.pattern is not None or "未识别" in "".join(sig.reasons) or "等待" in "".join(sig.reasons)


def test_flat_no_pattern():
    s = _pad_then([], n=80, base=100)
    sig = evaluate_double_line(
        s,
        DoubleLineConfig(
            require_ema200=False,
            require_volume=False,
            require_adx=False,
            use_conditional_edge=False,
        ),
    )
    assert sig.direction == "wait"


def test_stop_buffer_capped_by_atr():
    from analyst.compute.strategies.double_line_reversal import _stop_buffer_abs

    cfg = DoubleLineConfig(stop_buffer_pct=2.0, stop_buffer_atr_mult=1.0)
    # 小周期场景：2% = 2.0，远大于 ATR 0.5 → 取 ATR
    assert _stop_buffer_abs(100.0, atr=0.5, cfg=cfg) == 0.5
    # 大周期场景：ATR 5.0 > 2% → 退回 2%
    assert _stop_buffer_abs(100.0, atr=5.0, cfg=cfg) == 2.0
    # 禁用封顶
    cfg_off = DoubleLineConfig(stop_buffer_pct=2.0, stop_buffer_atr_mult=0.0)
    assert _stop_buffer_abs(100.0, atr=0.5, cfg=cfg_off) == 2.0


def test_no_chase_when_far_above_break():
    # 形态成立后价格已远超突破位 → 不追价，观望
    s = _pad_then([], n=50, base=100)
    s.candles[-3] = Candle(
        timestamp=s.candles[-3].timestamp,
        open=100, high=100.5, low=96, close=96.2, volume=2000,
    )
    s.candles[-2] = Candle(
        timestamp=s.candles[-2].timestamp,
        open=96.3, high=100.4, low=96.0, close=100.2, volume=2500,
    )
    # 最新根暴涨远离突破位 100.5
    s.candles[-1] = Candle(
        timestamp=s.candles[-1].timestamp,
        open=100.3, high=108.0, low=100.2, close=107.5, volume=3000,
    )
    sig = evaluate_double_line(
        s,
        DoubleLineConfig(
            min_body_ratio=0.5,
            min_overlap_ratio=0.4,
            min_sudden_atr_mult=0.5,
            require_ema200=False,
            require_volume=False,
            require_adx=False,
            use_conditional_edge=False,
            max_chase_atr=1.5,
        ),
    )
    assert sig.direction == "wait"
    assert "chase" in sig.filters_failed
