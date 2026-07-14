"""Kelly 仓位测试。"""

import pytest

from analyst.compute.kelly import kelly_fraction, suggest_position


def test_kelly_zero_on_negative_edge():
    # p=0.4, b=1 → f* = (0.4-0.6)/1 = -0.2 → 0
    assert kelly_fraction(0.4, 1.0) == 0.0


def test_kelly_positive():
    # p=0.55, b=2 → f* = (1.1 - 0.45)/2 = 0.325
    assert kelly_fraction(0.55, 2.0) == pytest.approx(0.325)


def test_quarter_kelly_capped():
    size = suggest_position(0.55, 2.0, kelly_scale=0.25, max_fraction=0.05)
    assert size.full_kelly == pytest.approx(0.325)
    assert size.suggested_fraction == 0.05  # 0.325*0.25=0.08125 → capped
    assert "上限" in size.note
