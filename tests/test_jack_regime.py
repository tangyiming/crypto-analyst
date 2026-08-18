"""零下二度三盘分类测试。"""

from datetime import datetime, timezone

import pytest

from analyst.compute.fibonacci import compute_fib
from analyst.compute.jack_levels import compute_jack_levels
from analyst.compute.jack_regime import compute_jack_regime
from analyst.compute.plan import generate_baseline_plan
from analyst.compute.structure import Structure
from analyst.data.fetcher import Candle, CandleSeries


def _eth_structure() -> Structure:
    return Structure(
        trend="up",
        supports=[1760.0, 1700.0],
        resistances=[1828.0, 2098.0],
        key_pivot=1800.0,
        recent_high=2463.0,
        recent_low=1510.0,
    )


def _hourly_with_spike() -> CandleSeries:
    base = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
    candles = []
    for i in range(24):
        # 最后一根：扎针到 1702 但收在 1775
        if i == 23:
            candles.append(
                Candle(
                    timestamp=base.replace(hour=i),
                    open=1788.0,
                    high=1790.0,
                    low=1702.0,
                    close=1775.0,
                    volume=100.0,
                )
            )
        else:
            candles.append(
                Candle(
                    timestamp=base.replace(hour=i),
                    open=1780.0 + i * 0.1,
                    high=1795.0,
                    low=1770.0,
                    close=1785.0,
                    volume=50.0,
                )
            )
    return CandleSeries(symbol="ETH/USDT", timeframe="1h", candles=candles)


def test_eth_rebound_618_matches_tweet():
    st = _eth_structure()
    fib = compute_fib(st.recent_high, st.recent_low)
    assert fib.rebound_618 == pytest.approx(1510 + (2463 - 1510) * 0.618, rel=1e-4)
    assert abs(fib.rebound_618 - 2098) < 2


def test_defense_break_switches_to_range_playbook():
    st = _eth_structure()
    fib = compute_fib(st.recent_high, st.recent_low)
    jack = compute_jack_levels(
        current_price=1755.0,
        structure=st,
        fib=fib,
        daily_indicators={
            "macd": {"histogram": 1, "above_zero": False},
            "ema": {"ema7": 1900, "ema30": 1850},
        },
        symbol="ETH/USDT",
    )
    reg = compute_jack_regime(
        current_price=1748.0,
        jack=jack,
        structure=st,
        primary_series=_hourly_with_spike(),
    )
    assert reg.defense_broken is True
    assert reg.regime_zh == "震荡盘"
    assert reg.seed_style == "pullback"
    assert reg.tp_style == "intraday_618"
    assert "1828" in reg.summary_line or "2098" in str(jack.rebound_618)


def test_strong_trend_when_continuation_intact():
    st = Structure(
        trend="up",
        supports=[62000.0],
        resistances=[67000.0],
        key_pivot=64000.0,
        recent_high=82828.0,
        recent_low=57758.0,
    )
    fib = compute_fib(st.recent_high, st.recent_low)
    jack = compute_jack_levels(
        current_price=72000.0,
        structure=st,
        fib=fib,
        daily_indicators={
            "macd": {"histogram": 10, "above_zero": True, "cross_signal": "golden"},
            "ema": {"ema7": 67000, "ema30": 62000},
        },
        symbol="BTC/USDT",
    )
    reg = compute_jack_regime(
        current_price=72000.0,
        jack=jack,
        structure=st,
    )
    assert reg.regime_zh == "强势盘"
    assert reg.seed_style == "market"
    assert reg.add_mode == "breakout"
    assert reg.continuation_intact is True


def test_weak_trend_daily_down():
    st = Structure(
        trend="down",
        supports=[60000.0],
        resistances=[65000.0],
        key_pivot=62000.0,
        recent_high=70000.0,
        recent_low=55000.0,
    )
    fib = compute_fib(st.recent_high, st.recent_low)
    jack = compute_jack_levels(
        current_price=61000.0,
        structure=st,
        fib=fib,
        daily_indicators={
            "macd": {"histogram": -5, "above_zero": False},
            "ema": {"ema7": 62000, "ema30": 65000},
        },
        symbol="BTC/USDT",
    )
    reg = compute_jack_regime(
        current_price=61000.0,
        jack=jack,
        structure=st,
    )
    assert reg.regime_zh == "弱势盘"
    assert reg.trade_side == "short"
    assert reg.add_mode == "breakdown"
    assert reg.waist_line == pytest.approx(70000.0 * 0.5)
    assert reg.below_waist is False


def _down_jack(price: float, high: float = 70000.0, low: float = 55000.0):
    st = Structure(
        trend="down",
        supports=[60000.0],
        resistances=[65000.0],
        key_pivot=62000.0,
        recent_high=high,
        recent_low=low,
    )
    fib = compute_fib(st.recent_high, st.recent_low)
    jack = compute_jack_levels(
        current_price=price,
        structure=st,
        fib=fib,
        daily_indicators={
            "macd": {"histogram": -5, "above_zero": False},
            "ema": {"ema7": 62000, "ema30": 65000},
        },
        symbol="BTC/USDT",
    )
    return st, fib, jack


def test_waist_line_is_half_swing_high():
    st, _fib, jack = _down_jack(61000.0, high=70000.0)
    reg = compute_jack_regime(current_price=61000.0, jack=jack, structure=st)
    assert reg.waist_line == pytest.approx(jack.swing_high * 0.5)
    assert jack.swing_high == pytest.approx(70000.0)


def test_below_waist_stops_chase_short():
    price = 34000.0
    st, fib, jack = _down_jack(price, high=70000.0)
    reg = compute_jack_regime(current_price=price, jack=jack, structure=st)
    assert reg.below_waist is True
    assert reg.add_mode == "none"
    assert reg.trade_side == "wait"
    assert "穷寇莫追" in reg.playbook_line
    plan = generate_baseline_plan(price, fib, st, jack=jack, jack_regime=reg)
    assert plan.direction == "wait"
    assert "腰斩" in plan.rationale


def test_wick_hold_allows_seed():
    st = _eth_structure()
    fib = compute_fib(st.recent_high, st.recent_low)
    jack = compute_jack_levels(
        current_price=1775.0,
        structure=st,
        fib=fib,
        daily_indicators={
            "macd": {"histogram": 1, "above_zero": False},
            "ema": {"ema7": 1900, "ema30": 1850},
        },
        symbol="ETH/USDT",
    )
    series = _hourly_with_spike()
    reg = compute_jack_regime(
        current_price=1775.0,
        jack=jack,
        structure=st,
        primary_series=series,
    )
    assert reg.wick_hold is True
    assert "虚破" in reg.playbook_line or "头仓" in reg.playbook_line


def test_hollow_daily_accel_flag():
    st = _eth_structure()
    fib = compute_fib(st.recent_high, st.recent_low)
    jack = compute_jack_levels(
        current_price=1800.0,
        structure=st,
        fib=fib,
        daily_indicators={
            "macd": {"histogram": 1, "above_zero": False},
            "ema": {"ema7": 1900, "ema30": 1850},
        },
        symbol="ETH/USDT",
    )
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(10):
        candles.append(
            Candle(
                timestamp=base.replace(day=min(1 + i, 28)),
                open=1700.0,
                high=1720.0,
                low=1680.0,
                close=1710.0,
                volume=10.0,
            )
        )
    candles[-1] = Candle(
        timestamp=base.replace(day=18),
        open=1710.0,
        high=1820.0,
        low=1705.0,
        close=1815.0,
        volume=80.0,
    )
    daily = CandleSeries(symbol="ETH/USDT", timeframe="1d", candles=candles)
    reg = compute_jack_regime(
        current_price=1815.0,
        jack=jack,
        structure=st,
        daily_series=daily,
    )
    assert reg.hollow_daily is True


def test_near_rebound_618_no_add():
    st = _eth_structure()
    fib = compute_fib(st.recent_high, st.recent_low)
    jack = compute_jack_levels(
        current_price=2100.0,
        structure=st,
        fib=fib,
        daily_indicators={
            "macd": {"histogram": 4, "above_zero": True, "cross_signal": "golden"},
            "ema": {"ema7": 2000, "ema30": 1800},
        },
        symbol="ETH/USDT",
    )
    # 0.618 ≈ 2098；现价贴着鱼尾
    reg = compute_jack_regime(current_price=2100.0, jack=jack, structure=st)
    assert jack.rebound_618 == pytest.approx(2098, abs=2)
    assert reg.near_rebound_618 is True
    assert reg.add_mode == "none"


def test_second_break_when_touch_count_ge_2():
    st = Structure(
        trend="up",
        supports=[62000.0],
        resistances=[67000.0],
        key_pivot=64000.0,
        recent_high=82828.0,
        recent_low=57758.0,
    )
    fib = compute_fib(st.recent_high, st.recent_low)
    jack = compute_jack_levels(
        current_price=72000.0,
        structure=st,
        fib=fib,
        daily_indicators={
            "macd": {"histogram": 10, "above_zero": True, "cross_signal": "golden"},
            "ema": {"ema7": 67000, "ema30": 62000},
        },
        symbol="BTC/USDT",
    )
    jack.touch_count = 2
    reg = compute_jack_regime(current_price=72000.0, jack=jack, structure=st)
    assert reg.second_break is True
    assert "假突破" in reg.playbook_line or "二次突破" in reg.summary_line
