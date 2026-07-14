"""斐波模块测试 - 这是 Sprint 1 唯一已实现的模块。"""

import pytest

from analyst.compute.fibonacci import compute_fib, find_long_zone, find_short_zone


def test_compute_fib_basic():
    """80000 → 100000 上涨波段。"""
    fib = compute_fib(high=100000, low=80000)
    assert fib.range == 20000
    # 0.5 回撤 = 90000
    assert fib.retr_500 == pytest.approx(90000)
    # 0.618 回撤
    assert fib.retr_618 == pytest.approx(100000 - 20000 * 0.618)
    # 1.272 扩展
    assert fib.ext_1272 == pytest.approx(100000 + 20000 * 0.272)


def test_compute_fib_rebound():
    """下跌波段反弹位。"""
    fib = compute_fib(high=100000, low=80000)
    assert fib.rebound_500 == pytest.approx(90000)
    assert fib.rebound_618 == pytest.approx(80000 + 20000 * 0.618)


def test_long_zone():
    fib = compute_fib(high=100000, low=80000)
    low, high = find_long_zone(fib)
    # 应当返回 0.618-0.5 区间（low 是 0.618，更深的回撤）
    assert low < high
    assert low == pytest.approx(fib.retr_618)
    assert high == pytest.approx(fib.retr_500)


def test_short_zone():
    fib = compute_fib(high=100000, low=80000)
    low, high = find_short_zone(fib)
    assert low < high
    assert low == pytest.approx(fib.rebound_500)
    assert high == pytest.approx(fib.rebound_618)
