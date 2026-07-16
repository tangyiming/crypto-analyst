"""指标计算测试。"""

from datetime import datetime

import pytest

from analyst.compute.indicators import (
    compute_adx,
    compute_boll,
    compute_ema,
    compute_macd,
    ema,
    sma,
    stddev,
)
from analyst.data.fetcher import Candle, CandleSeries


def _make_series(closes: list[float], timeframe: str = "4h") -> CandleSeries:
    """构造仅供指标测试用的 K线序列（OHLC=close）。"""
    candles = [
        Candle(
            timestamp=datetime(2026, 1, 1),
            open=c, high=c, low=c, close=c, volume=0,
        )
        for c in closes
    ]
    return CandleSeries(symbol="TEST/USDT", timeframe=timeframe, candles=candles)


# ─────────────────────────────────────
# 基础数学
# ─────────────────────────────────────
def test_sma():
    assert sma([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)


def test_stddev():
    # 5 个等差数 1..5, mean=3, var=2, std=√2
    result = stddev([1, 2, 3, 4, 5], 5)
    assert result == pytest.approx(2**0.5)


def test_ema_first_value_equals_input():
    result = ema([10, 20, 30], 3)
    assert result[0] == 10.0
    assert len(result) == 3


def test_ema_converges():
    """EMA 应当趋近于稳定值。"""
    values = [100.0] * 50
    result = ema(values, 10)
    assert result[-1] == pytest.approx(100.0)


# ─────────────────────────────────────
# MACD
# ─────────────────────────────────────
def test_macd_insufficient_data():
    """数据不足返回零值。"""
    series = _make_series([100.0] * 10)
    result = compute_macd(series)
    assert result.dif == 0.0
    assert result.dea == 0.0


def test_macd_flat_market():
    """完全平直的市场 MACD 应接近 0。"""
    series = _make_series([100.0] * 100)
    result = compute_macd(series)
    assert abs(result.dif) < 0.01
    assert abs(result.dea) < 0.01


def test_macd_uptrend():
    """单调上涨，DIF 应该大于 0（短期均线在长期均线上方）。"""
    closes = [100 + i for i in range(100)]
    series = _make_series(closes)
    result = compute_macd(series)
    assert result.above_zero is True


# ─────────────────────────────────────
# EMA
# ─────────────────────────────────────
def test_ema_result_uses_last_close_when_short():
    series = _make_series([100.0, 200.0, 300.0])
    result = compute_ema(series)
    # 数据不足 7/30/52 时，回退到最后收盘
    assert result.ema7 == 300.0


# ─────────────────────────────────────
# BOLL
# ─────────────────────────────────────
def test_boll_flat_market():
    """平直市场 BOLL 上下轨等于中轨。"""
    series = _make_series([100.0] * 30)
    result = compute_boll(series)
    assert result.upper == pytest.approx(100.0)
    assert result.middle == pytest.approx(100.0)
    assert result.lower == pytest.approx(100.0)
    assert result.width == pytest.approx(0.0)


def test_boll_volatile_market():
    """波动市场上下轨应当分开。"""
    closes = [100, 110, 90, 105, 95, 108, 92, 102, 98, 103,
              100, 110, 90, 105, 95, 108, 92, 102, 98, 103]
    series = _make_series(closes)
    result = compute_boll(series)
    assert result.upper > result.middle > result.lower
    assert result.width > 0


def test_adx_insufficient_and_trend():
    assert compute_adx([1] * 10, [1] * 10, [1] * 10, 14) == 0.0
    # 单调上涨：ADX 应明显 > 0
    highs = [100 + i * 1.5 for i in range(80)]
    lows = [100 + i * 1.5 - 0.5 for i in range(80)]
    closes = [100 + i * 1.5 - 0.1 for i in range(80)]
    adx_v = compute_adx(highs, lows, closes, 14)
    assert adx_v > 20.0
