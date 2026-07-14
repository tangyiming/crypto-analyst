"""验证算法测试 - 这是最关键的算法。"""

from datetime import datetime, timedelta

import pytest

from analyst.compute.plan import TradePlan
from analyst.data.fetcher import Candle
from analyst.training.verify import (
    TradeOutcome,
    find_optimal_trade,
    verify_plan,
)


def _candle(t: int, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1) + timedelta(hours=t),
        open=o, high=h, low=l, close=c, volume=0,
    )


def _long_plan(entry_low=99, entry_high=101, stop=95, tp1=110, tp2=None) -> TradePlan:
    return TradePlan(
        direction="long",
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2,
        rr_ratio=2.0,
        rationale="",
    )


def _short_plan(entry_low=99, entry_high=101, stop=105, tp1=90, tp2=None) -> TradePlan:
    return TradePlan(
        direction="short",
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2,
        rr_ratio=2.0,
        rationale="",
    )


# ─────────────────────────────────────
# Wait 计划
# ─────────────────────────────────────
def test_wait_plan_returns_no_trigger():
    plan = TradePlan(
        direction="wait",
        entry_low=0, entry_high=0, stop_loss=0,
        take_profit_1=0, take_profit_2=None,
        rr_ratio=0, rationale="",
    )
    candles = [_candle(0, 100, 110, 90, 105)]
    result = verify_plan(plan, candles)
    assert result.outcome == TradeOutcome.NO_TRIGGER
    assert result.pnl_r == 0.0


# ─────────────────────────────────────
# 多单
# ─────────────────────────────────────
def test_long_no_trigger():
    """价格远离入场区，从未触发。"""
    plan = _long_plan()
    candles = [
        _candle(0, 120, 121, 119, 120),
        _candle(1, 121, 122, 120, 121),
    ]
    result = verify_plan(plan, candles)
    assert result.triggered is False
    assert result.outcome == TradeOutcome.NO_TRIGGER


def test_long_hit_tp1():
    """触发入场后命中第一目标。"""
    plan = _long_plan(entry_low=99, entry_high=101, stop=95, tp1=110)
    candles = [
        _candle(0, 102, 102, 100, 100),  # 触发入场（100 在 99-101 之间）
        _candle(1, 100, 105, 99, 104),
        _candle(2, 104, 110, 103, 110),  # 命中 110
    ]
    result = verify_plan(plan, candles)
    assert result.triggered is True
    assert result.outcome == TradeOutcome.WIN_TP1
    # entry_mid=100, risk=5, reward=10 → 2R
    assert result.pnl_r == pytest.approx(2.0)


def test_long_hit_stop():
    """触发入场后被止损。"""
    plan = _long_plan(entry_low=99, entry_high=101, stop=95, tp1=110)
    candles = [
        _candle(0, 102, 102, 100, 100),  # 触发入场
        _candle(1, 100, 102, 94, 95),    # 跌破 95
    ]
    result = verify_plan(plan, candles)
    assert result.outcome == TradeOutcome.LOSS
    assert result.pnl_r == -1.0


def test_long_hit_tp2():
    """命中第二目标。"""
    plan = _long_plan(entry_low=99, entry_high=101, stop=95, tp1=105, tp2=120)
    candles = [
        _candle(0, 102, 102, 100, 100),
        _candle(1, 100, 121, 100, 120),  # 同根命中 105 和 120 -> 取 TP2
    ]
    result = verify_plan(plan, candles)
    assert result.outcome == TradeOutcome.WIN_TP2
    # entry_mid=100, risk=5, reward=20 → 4R
    assert result.pnl_r == pytest.approx(4.0)


def test_long_open_position():
    """触发但既没止损也没止盈。"""
    plan = _long_plan(entry_low=99, entry_high=101, stop=95, tp1=110)
    candles = [
        _candle(0, 102, 102, 100, 100),   # 触发
        _candle(1, 100, 103, 99, 102),    # 浮盈中
    ]
    result = verify_plan(plan, candles)
    assert result.outcome == TradeOutcome.OPEN
    assert result.pnl_r > 0   # 浮盈


# ─────────────────────────────────────
# 空单
# ─────────────────────────────────────
def test_short_hit_tp1():
    plan = _short_plan(entry_low=99, entry_high=101, stop=105, tp1=90)
    candles = [
        _candle(0, 100, 102, 100, 100),    # 入场（100 在 99-101 之间）
        _candle(1, 100, 100, 89, 90),      # 命中 90
    ]
    result = verify_plan(plan, candles)
    assert result.outcome == TradeOutcome.WIN_TP1
    # entry_mid=100, risk=5, reward=10 → 2R
    assert result.pnl_r == pytest.approx(2.0)


# ─────────────────────────────────────
# 同根 K 线触发止损 + 止盈：保守按止损
# ─────────────────────────────────────
def test_long_same_bar_stop_and_tp():
    plan = _long_plan(entry_low=99, entry_high=101, stop=95, tp1=110)
    candles = [
        _candle(0, 102, 102, 100, 100),
        _candle(1, 100, 110, 95, 100),   # 同根触及 95 和 110
    ]
    result = verify_plan(plan, candles)
    assert result.outcome == TradeOutcome.LOSS


# ─────────────────────────────────────
# 最优参考线
# ─────────────────────────────────────
def test_find_optimal_long():
    candles = [
        _candle(0, 100, 100, 90, 95),
        _candle(1, 95, 110, 93, 110),
    ]
    result = find_optimal_trade("long", candles)
    assert result.entry_price == 90
    assert result.exit_price == 110
    assert result.pnl_r > 0


def test_find_optimal_wait_returns_zero():
    result = find_optimal_trade("wait", [])
    assert result.pnl_r == 0.0
