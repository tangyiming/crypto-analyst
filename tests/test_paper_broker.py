"""纸面交易引擎 + Jack 强势盘低多计划生成。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from analyst.compute.strategies.jack_pullback import JackPullbackConfig, OrderPlan, build_plan, position_size
from analyst.exec.paper import PaperBroker


def _plan(entry=100.0, stop=97.0, tp1=106.0, sym="SOL/USDT", now=None):
    now = now or datetime(2026, 8, 27, tzinfo=timezone.utc)
    return OrderPlan(symbol=sym, side="long", entry=entry, stop=stop, tp1=tp1, created_at=now, expires_at=now + timedelta(hours=24))


def test_position_size_by_risk_and_leverage_cap():
    cfg = JackPullbackConfig(risk_pct=1.0, max_leverage=3.0)
    p = _plan(100, 97, 106)
    qty = position_size(10_000, p, cfg)
    assert abs(qty - 100 / 3) < 1e-9  # 风险 100U / 每单位 3U
    tight = _plan(100, 99.9, 106)
    assert position_size(10_000, tight, cfg) == 300  # 名义 ≤ 3 倍权益


def test_paper_flow_fill_tp1_be_stop(tmp_path):
    b = PaperBroker(tmp_path / "s.json", equity0=10_000, cfg=JackPullbackConfig(risk_pct=1.0), daily_fuse_pct=0)
    t0 = datetime(2026, 8, 27, 4, tzinfo=timezone.utc)
    ev = b.submit(_plan(100, 97, 106, now=t0), t0)
    assert ev and ev.kind == "submit" and "SOL/USDT" in b.state["pending"]
    assert b.on_mark("SOL/USDT", 101.0, t0 + timedelta(hours=1)) == []  # 未触价
    evs = b.on_mark("SOL/USDT", 99.5, t0 + timedelta(hours=2))
    assert [e.kind for e in evs] == ["fill"] and "SOL/USDT" in b.state["positions"]
    pos = b.state["positions"]["SOL/USDT"]
    assert abs(pos["qty"] - 100 / 3) < 1e-6 and b.equity < 10_000  # 扣了 maker 费
    evs = b.on_mark("SOL/USDT", 106.5, t0 + timedelta(hours=5))
    assert [e.kind for e in evs] == ["tp1"] and pos["tp1_done"] and pos["stop"] == 100.0
    assert b.equity > 10_000
    eq_after_tp1 = b.equity
    evs = b.on_mark("SOL/USDT", 99.0, t0 + timedelta(hours=8))  # 打到保本损
    assert [e.kind for e in evs] == ["stop"] and "SOL/USDT" not in b.state["positions"]
    assert b.equity < eq_after_tp1 and b.state["closed"][-1]["reason"] == "be_stop"
    # 重新加载状态一致
    b2 = PaperBroker(tmp_path / "s.json", equity0=10_000)
    assert abs(b2.equity - b.equity) < 1e-9 and b2.state["closed"]


def test_paper_expire_reject_and_fuse(tmp_path):
    b = PaperBroker(tmp_path / "s.json", equity0=10_000, cfg=JackPullbackConfig(risk_pct=5.0, max_leverage=10), daily_fuse_pct=3.0, max_positions=1)
    t0 = datetime(2026, 8, 27, 4, tzinfo=timezone.utc)
    b.submit(_plan(100, 97, 106, now=t0), t0)
    evs = b.on_mark("SOL/USDT", 101.0, t0 + timedelta(hours=25))
    assert [e.kind for e in evs] == ["expire"] and not b.state["pending"]
    # 成交后大跌触发日内熔断（风险 5% ×… 止损前先熔断）
    b.submit(_plan(100, 90, 120, now=t0), t0)
    b.on_mark("SOL/USDT", 99.0, t0 + timedelta(hours=1))
    assert "SOL/USDT" in b.state["positions"]
    evs = b.on_mark("SOL/USDT", 93.0, t0 + timedelta(hours=2))  # 亏 ~3.5% 权益 → 熔断
    kinds = [e.kind for e in evs]
    assert "fuse" in kinds and not b.state["positions"]
    rej = b.submit(_plan(95, 92, 100, sym="ETH/USDT", now=t0 + timedelta(hours=3)), t0 + timedelta(hours=3))
    assert rej and rej.kind == "reject"


def test_build_plan_only_in_strong_long_regime():
    from analyst.compute.jack_regime import JackRegime
    from analyst.compute.jack_levels import JackLevels

    jack = JackLevels(
        swing_high=110, swing_low=90, rebound_382=97.6, rebound_500=100, rebound_618=102.4, retr_382=102.4, retr_618=97.6,
        boll_mid=None, confluence_382=False, confluence_618=False, daily_bias="up", defense_level=95, htf_ready=True,
        horizon="swing", touch_level=None, touch_count=0, rs_note="", summary_line="",
    )
    base = JackRegime(
        regime="strong_trend", regime_zh="强势盘", trade_side="long", seed_style="market", add_mode="breakout", tp_style="new_high",
        defense_broken=False, continuation_intact=True, nearest_support=96.0, nearest_resistance=105.0, prev_day_high=None, prev_day_low=None,
        intraday_high=None, intraday_low=None, tp_intraday_50=None, tp_intraday_618=None, ema12h_6=None, spike_stop_recent=False,
        pullback_618=98.0, pullback_50=99.0, pivot_supports=(96.5, 94.0), boll_4h_lower=95.0, boll_4h_upper=104.0, ext_150=108.0,
    )
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    p = build_plan("SOL/USDT", 100.0, jack, base, now)
    assert p is not None and p.entry == 98.0 and p.stop < 96.5 and p.tp1 == 105.0 and p.rr >= 1.2
    assert build_plan("SOL/USDT", 100.0, jack, replace(base, regime="range"), now) is None
    assert build_plan("SOL/USDT", 100.0, jack, replace(base, trade_side="wait"), now) is None
    # 回踩位太近（<0.4%）且近支撑太远（>4%）→ 不出计划
    assert build_plan("SOL/USDT", 100.0, jack, replace(base, pullback_618=99.8, nearest_support=90.0), now) is None


def test_report_view_fields(tmp_path):
    b = PaperBroker(tmp_path / "s.json", equity0=10_000, cfg=JackPullbackConfig(risk_pct=1.0), daily_fuse_pct=3.0)
    t0 = datetime(2026, 8, 27, 4, tzinfo=timezone.utc)
    b.submit(_plan(100, 97, 106, now=t0), t0)
    b.submit(_plan(2000, 1950, 2200, sym="ETH/USDT", now=t0), t0)
    b.on_mark("SOL/USDT", 99.5, t0 + timedelta(hours=2))  # 成交
    rep = b.report({"SOL/USDT": 102.0, "ETH/USDT": 2050.0}, now=t0 + timedelta(hours=6))
    assert rep["limits"] == {"max_positions": 2, "open": 1, "pending": 1}
    pos = rep["positions"][0]
    assert pos["symbol"] == "SOL/USDT" and pos["mark"] == 102.0
    assert abs(pos["unrealized"] - pos["qty"] * 2.0) < 1e-9 and abs(pos["unrealized_pct"] - 2.0) < 1e-9
    assert pos["held_hours"] == 4 and pos["stop_dist_pct"] > 0 and pos["tp1_dist_pct"] > 0
    pend = rep["pending"][0]
    assert pend["symbol"] == "ETH/USDT" and abs(pend["dist_pct"] - 2.5) < 1e-9 and abs(pend["rr"] - 4.0) < 1e-9
    assert rep["equity_mtm"] == rep["equity"] + rep["unrealized"]
    assert rep["stats"]["closed_n"] == 0 and rep["stats"]["win_rate"] is None
    assert rep["journal"][0]["kind"] == "fill"  # 倒序，最新在前
    # 平仓后统计
    b.on_mark("SOL/USDT", 96.0, t0 + timedelta(hours=8))
    rep2 = b.report({}, now=t0 + timedelta(hours=9))
    assert rep2["stats"]["closed_n"] == 1 and rep2["stats"]["losses"] == 1 and rep2["stats"]["by_reason"] == {"stop": 1}
    assert rep2["closed"][0]["reason"] == "stop" and rep2["closed"][0]["net"] < 0
