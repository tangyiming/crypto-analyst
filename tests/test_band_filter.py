"""波段过滤器（WaveTrend 近似）与 Eric 规则。"""

from datetime import datetime, timedelta

from analyst.compute.band_filter import compute_band_filter, wavetrend_series
from analyst.compute.eric_signals import evaluate_eric
from analyst.data.fetcher import Candle, CandleSeries


def _c(i: int, close: float, high: float | None = None, low: float | None = None) -> Candle:
    h = high if high is not None else close + 1
    l = low if low is not None else close - 1
    return Candle(
        timestamp=datetime(2026, 1, 1) + timedelta(hours=i),
        open=close,
        high=h,
        low=l,
        close=close,
        volume=1000,
    )


def test_wavetrend_scale_and_oversold_edge():
    # 先涨后急跌，迫使 WT 下穿 -40
    closes = [100.0 + i * 0.5 for i in range(80)]
    closes += [closes[-1] - i * 2.2 for i in range(1, 40)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    wt = wavetrend_series(highs, lows, closes, channel_len=10, average_len=21)
    assert min(wt[-20:]) < -20  # 急跌后应明显走弱
    series = CandleSeries(
        "BTC/USDT",
        "1h",
        [_c(i, closes[i], highs[i], lows[i]) for i in range(len(closes))],
    )
    # 找第一根进入超卖的前缀，验证 snapshot 边沿
    found = False
    for n in range(60, len(closes) + 1):
        snap = compute_band_filter(
            CandleSeries("BTC/USDT", "1d", series.candles[:n])
        )
        if snap and snap.entered_oversold:
            assert snap.value <= -40
            found = True
            break
    assert found, "应出现波段过滤器超卖边沿"


def test_eric_uses_band_filter_reason():
    closes = [100.0 + i * 0.5 for i in range(80)]
    closes += [closes[-1] - i * 2.2 for i in range(1, 45)]
    candles = [_c(i, closes[i], closes[i] + 1, closes[i] - 1) for i in range(len(closes))]
    # 截到首次过滤器超卖
    for n in range(60, len(candles) + 1):
        series = CandleSeries("BTC/USDT", "1d", candles[:n])
        bf = compute_band_filter(series)
        if not bf or not bf.entered_oversold:
            continue
        sigs = evaluate_eric(series, atr=3.0)
        os_sigs = [s for s in sigs if s.kind == "oversold"]
        assert os_sigs
        assert any("波段过滤器" in r or "WT" in r for r in os_sigs[0].reasons)
        assert os_sigs[0].filter_value is not None
        return
    raise AssertionError("未找到过滤器超卖边沿用于 Eric 测试")


def test_weekly_ob_gate_uses_channel_z():
    """周线启用 Z26 超买门：Stoch 贴顶但 Z26 未到 2.93 时不进入超买；日线同样序列则按 Stoch 进入。"""
    from analyst.compute.band_filter import (
        OB_GATE_ENTER_Z,
        channel_z_series,
        compute_band_filter,
    )

    # 长期缓涨：Stoch 贴顶，但相对 26 根均值/σ 的偏离不极端
    closes = [100.0 + i * 0.6 for i in range(80)]
    candles = [_c(i, closes[i], closes[i] + 0.5, closes[i] - 0.5) for i in range(len(closes))]
    weekly = CandleSeries("BTC/USDT", "1w", candles)
    daily = CandleSeries("BTC/USDT", "1d", candles)
    w = compute_band_filter(weekly)
    d = compute_band_filter(daily)
    assert w is not None and d is not None
    assert w.ob_gate and not d.ob_gate
    assert d.state == "overbought"  # 日线：Stoch 读数贴顶即超买
    z = channel_z_series(closes)[-1]
    assert abs(w.channel_z - z) < 1e-9
    assert (w.state == "overbought") == (z >= OB_GATE_ENTER_Z)
    # 超卖端不受门影响：急跌后周线也应进入 oversold
    drop = closes + [closes[-1] - 3.0 * k for k in range(1, 20)]
    cands = [_c(i, drop[i], drop[i] + 0.5, drop[i] - 0.5) for i in range(len(drop))]
    w2 = compute_band_filter(CandleSeries("BTC/USDT", "1w", cands))
    assert w2 is not None and w2.state == "oversold"
