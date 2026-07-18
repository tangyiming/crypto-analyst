"""条件胜率估计单测。"""

from datetime import datetime, timezone

from analyst.compute.conditional_edge import estimate_conditional_edge


def test_strong_us_session_boosts_and_scales():
    ts = datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)  # 美盘窗口
    edge = estimate_conditional_edge(
        base_win_rate=0.47,
        adx=32,
        sudden=3.0,
        overlap=0.75,
        bar_ts=ts,
        min_win_rate=0.42,
    )
    assert not edge.skip
    assert edge.session == "us"
    assert edge.win_rate > 0.47
    assert 0.35 <= edge.risk_scale <= 1.0


def test_weak_context_skips():
    ts = datetime(2026, 3, 10, 5, 0, tzinfo=timezone.utc)  # off + 近资金费 08? no
    edge = estimate_conditional_edge(
        base_win_rate=0.47,
        adx=10,
        sudden=1.5,
        overlap=0.4,
        bar_ts=ts,
        min_win_rate=0.42,
    )
    assert edge.skip
    assert edge.risk_scale == 0.0
    assert "条件胜率" in edge.skip_reason


def test_near_funding_penalty():
    # 同一 off 时段：15:50 近 16:00 结算 vs 12:00 远离结算
    near = estimate_conditional_edge(
        base_win_rate=0.47,
        adx=25,
        sudden=2.5,
        overlap=0.6,
        bar_ts=datetime(2026, 3, 10, 15, 50, tzinfo=timezone.utc),
    )
    far = estimate_conditional_edge(
        base_win_rate=0.47,
        adx=25,
        sudden=2.5,
        overlap=0.6,
        bar_ts=datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc),
    )
    assert near.near_funding
    assert not far.near_funding
    assert near.session == far.session == "off"
    assert near.win_rate < far.win_rate
