"""波段锁点 + 头仓仓位测试。"""

import pytest

from analyst.compute.fibonacci import compute_fib
from analyst.compute.swing_levels import compute_swing_levels
from analyst.compute.plan import generate_baseline_plan
from analyst.compute.position_sizing import plan_seed_position
from analyst.compute.structure import Structure


def _up_structure() -> Structure:
    return Structure(
        trend="up",
        supports=[62000.0, 60000.0],
        resistances=[67000.0, 70000.0],
        key_pivot=64000.0,
        recent_high=82828.0,
        recent_low=57758.0,
    )


def test_swing_rebound_matches_formula():
    """BTC 帖：57758+(82828-57758)*0.382 ≈ 67334。"""
    st = _up_structure()
    fib = compute_fib(st.recent_high, st.recent_low)
    swing = compute_swing_levels(
        current_price=64625,
        structure=st,
        fib=fib,
        daily_indicators={
            "macd": {"histogram": 10, "above_zero": True, "cross_signal": "golden"},
            "ema": {"ema7": 65000, "ema30": 62000},
            "boll": {"middle": 65778},
        },
        symbol="BTC/USDT",
    )
    assert swing.rebound_382 == pytest.approx(57758 + (82828 - 57758) * 0.382, rel=1e-6)
    assert swing.rebound_618 == pytest.approx(57758 + (82828 - 57758) * 0.618, rel=1e-6)
    assert swing.daily_bias == "up"
    assert swing.htf_ready is True
    assert swing.defense_level == pytest.approx(62000.0)


def test_baseline_uses_swing_bounce_targets():
    """下半区反弹：已过 0.382 则 TP1=0.618；防守用近支撑以保 R:R。"""
    st = Structure(
        trend="up",
        supports=[1780.0],  # 近防守，贴近实战失效位
        resistances=[2100.0],
        key_pivot=1800.0,
        recent_high=2463.0,
        recent_low=1510.0,
    )
    fib = compute_fib(st.recent_high, st.recent_low)
    swing = compute_swing_levels(
        current_price=1877,
        structure=st,
        fib=fib,
        daily_indicators={
            "macd": {"histogram": 1, "above_zero": False, "cross_signal": ""},
            "ema": {"ema7": 1900, "ema30": 1850},
            "boll": {"middle": 2008.0},
        },
        symbol="ETH/USDT",
    )
    plan = generate_baseline_plan(1877, fib, st, swing=swing)
    assert plan.direction == "long"
    assert plan.take_profit_1 == pytest.approx(swing.rebound_618)
    assert plan.take_profit_2 == pytest.approx(2100.0)
    assert plan.rr_ratio >= 2.0


def test_swing_eth_618_target():
    """ETH 帖：1510+(2463-1510)*0.618 ≈ 2098。"""
    st = Structure(
        trend="up",
        supports=[1600.0],
        resistances=[2100.0],
        key_pivot=1800.0,
        recent_high=2463.0,
        recent_low=1510.0,
    )
    swing = compute_swing_levels(
        current_price=1877,
        structure=st,
        daily_indicators={"boll": {"middle": 2008.0}},
        symbol="ETH/USDT",
    )
    assert swing.rebound_618 == pytest.approx(1510 + (2463 - 1510) * 0.618, rel=1e-4)
    assert abs(swing.rebound_618 - 2098) < 2


def test_swing_prompt_block_compact():
    st = _up_structure()
    swing = compute_swing_levels(current_price=65000, structure=st, symbol="BTC/USDT")
    block = swing.prompt_block(compact=True)
    assert "0.382" in block or "反抽" in block
    assert "锁点" in block or "H/L" in block


def test_seed_position_split():
    plan = plan_seed_position(10000, leverage=25, seed_pct=0.04, max_total_pct=0.18)
    assert plan.seed_margin == pytest.approx(400)
    assert plan.add_margin == pytest.approx(1400)
    assert plan.seed_notional == pytest.approx(10000)
    assert plan.add_mode == "pullback"
    assert "不可" in plan.note or "禁止" in plan.note


def test_seed_position_none_add():
    plan = plan_seed_position(10000, add_mode="none")
    assert plan.add_margin == 0
    assert plan.total_margin == plan.seed_margin


def test_seed_position_leverage_caps_total():
    """25x 基准≤25%；50x 减半到 12.5%。"""
    hi = plan_seed_position(10000, leverage=25, seed_pct=0.04, max_total_pct=0.40)
    assert hi.max_total_pct == pytest.approx(0.25)
    mid = plan_seed_position(10000, leverage=50, seed_pct=0.04, max_total_pct=0.18)
    assert mid.max_total_pct == pytest.approx(0.125)
    assert "25x" in mid.note
