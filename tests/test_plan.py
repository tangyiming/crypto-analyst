"""基线计划生成器测试。"""

import pytest

from analyst.compute.fibonacci import compute_fib
from analyst.compute.plan import calculate_rr, generate_baseline_plan
from analyst.compute.structure import Structure


# ─────────────────────────────────────
# R:R 计算
# ─────────────────────────────────────
def test_rr_long():
    # entry=100, stop=95, target=115 → risk=5, reward=15 → 3:1
    assert calculate_rr(100, 95, 115, "long") == pytest.approx(3.0)


def test_rr_short():
    # entry=100, stop=105, target=85 → risk=5, reward=15 → 3:1
    assert calculate_rr(100, 105, 85, "short") == pytest.approx(3.0)


def test_rr_zero_risk():
    assert calculate_rr(100, 100, 110, "long") == 0.0


def test_rr_wait():
    assert calculate_rr(100, 95, 110, "wait") == 0.0


# ─────────────────────────────────────
# Baseline 计划
# ─────────────────────────────────────
def test_baseline_uptrend_long():
    fib = compute_fib(high=100, low=80)
    structure = Structure(
        trend="up",
        supports=[85.0],
        resistances=[105.0, 110.0],
        key_pivot=88.0,
        recent_high=100,
        recent_low=80,
    )
    plan = generate_baseline_plan(95, fib, structure)
    assert plan.direction == "long"
    assert plan.entry_low == fib.retr_618
    assert plan.entry_high == fib.retr_500
    assert plan.rr_ratio >= 2.0


def test_baseline_downtrend_short():
    fib = compute_fib(high=100, low=80)
    structure = Structure(
        trend="down",
        supports=[75.0, 70.0],
        resistances=[95.0],
        key_pivot=92.0,
        recent_high=100,
        recent_low=80,
    )
    plan = generate_baseline_plan(85, fib, structure)
    assert plan.direction == "short"
    assert plan.rr_ratio >= 2.0


def test_baseline_range_returns_wait():
    fib = compute_fib(high=100, low=80)
    structure = Structure(
        trend="range",
        supports=[],
        resistances=[],
        key_pivot=90.0,
        recent_high=100,
        recent_low=80,
    )
    plan = generate_baseline_plan(90, fib, structure)
    assert plan.direction == "wait"
