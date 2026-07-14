"""对比器测试。"""

from analyst.compute.plan import TradePlan
from analyst.training.compare import compare_plans


def _plan(direction="long", el=99, eh=101, sl=95, tp1=110, tp2=120, rr=3.0):
    return TradePlan(
        direction=direction,
        entry_low=el,
        entry_high=eh,
        stop_loss=sl,
        take_profit_1=tp1,
        take_profit_2=tp2,
        rr_ratio=rr,
        rationale="",
    )


def test_same_direction_same_levels():
    a = _plan()
    b = _plan()
    diff = compare_plans(a, b)
    assert diff.same_direction is True
    assert diff.entry_overlap is True
    assert diff.stop_diff_pct == 0
    assert diff.rr_diff == 0


def test_opposite_directions():
    a = _plan(direction="long")
    b = _plan(direction="short", sl=105, tp1=90)
    diff = compare_plans(a, b)
    assert diff.same_direction is False


def test_entry_no_overlap():
    a = _plan(el=99, eh=101)
    b = _plan(el=110, eh=112)   # 完全不重叠
    diff = compare_plans(a, b)
    assert diff.entry_overlap is False


def test_user_more_conservative_long():
    """用户止损更近(更高) + 目标更近(更低) → 保守。"""
    user = _plan(direction="long", sl=98, tp1=105)
    ai = _plan(direction="long", sl=95, tp1=115)
    diff = compare_plans(user, ai)
    assert diff.user_more_conservative is True
