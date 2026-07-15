"""纸面交易账本单元测试。"""

from __future__ import annotations

import analyst.trading.paper as paper_mod
from analyst.trading.paper import PaperBroker


def _reset_broker(tmp_path, monkeypatch, **extra_env):
    monkeypatch.setenv("DATA_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("MONITOR_PAPER_ENABLED", "true")
    monkeypatch.setenv("MONITOR_PAPER_EQUITY", "10")
    monkeypatch.setenv("MONITOR_PAPER_RISK_PCT", "0.01")
    monkeypatch.setenv("MONITOR_PAPER_FEE_BPS", "0")
    monkeypatch.setenv("MONITOR_PAPER_MAX_POSITIONS", "12")
    monkeypatch.setenv(
        "MONITOR_PAPER_SOURCES", "ai_plan,double_line,cycle_switch"
    )
    for k, v in extra_env.items():
        monkeypatch.setenv(k, str(v))
    import analyst.config as cfg

    cfg._settings = None
    paper_mod._broker = None
    broker = PaperBroker()
    broker.reset(10.0)
    return broker


def test_paper_open_tp_and_sl(tmp_path, monkeypatch):
    broker = _reset_broker(tmp_path, monkeypatch, MONITOR_PAPER_MAX_POSITIONS=1)

    plan = {
        "direction": "long",
        "entry_low": 100,
        "entry_high": 100,
        "stop_loss": 99,
        "take_profit_1": 102,
        "rr_ratio": 2,
        "rationale": "test",
    }
    opened = broker.try_open_from_plan(
        symbol="BTC/USDT",
        timeframe="15m",
        direction="long",
        price=100.0,
        plan=plan,
        strategy="ai_plan",
    )
    assert opened is not None
    assert len(broker.state.positions) == 1
    assert broker.state.positions[0].qty == 0.1  # risk 0.1 / dist 1
    assert broker.state.positions[0].strategy == "ai_plan"
    assert broker.state.positions[0].rr_ratio == 2.0
    assert broker.state.positions[0].leverage == 5.0
    assert abs(broker.state.positions[0].notional - 10.0) < 1e-6  # 0.1 * 100
    assert abs(broker.state.positions[0].margin - 2.0) < 1e-6  # 10 / 5x
    st = broker.status()
    assert st["positions"][0]["rr_ratio"] == 2.0
    assert st["positions"][0]["unrealized_r"] is not None
    assert st["positions"][0]["leverage"] == 5.0
    assert abs(st["positions"][0]["margin"] - 2.0) < 1e-6
    assert st["used_margin"] > 0

    # 未触及
    assert broker.on_mark("BTC/USDT", 100.5) == []
    assert len(broker.state.positions) == 1
    st2 = broker.status()
    assert abs(st2["positions"][0]["unrealized_r"] - 0.5) < 1e-6  # +0.5 / 1R
    # 浮盈 0.05 / 保证金 2 = 2.5%
    assert abs(st2["positions"][0]["margin_roi_pct"] - 2.5) < 1e-6

    # 止盈
    closed = broker.on_mark("BTC/USDT", 102.0)
    assert len(closed) == 1
    assert closed[0]["trade"]["outcome"] == "tp"
    assert closed[0]["trade"]["pnl_usd"] > 0
    assert closed[0]["trade"].get("rr_ratio") == 2.0
    assert closed[0]["trade"].get("margin_roi_pct") is not None
    assert len(broker.state.positions) == 0
    assert broker.state.equity > 10.0

    # 再开空，触止损
    broker.reset(10.0)
    short_plan = {
        "direction": "short",
        "stop_loss": 101,
        "take_profit_1": 98,
        "rationale": "test",
    }
    assert (
        broker.try_open_from_plan(
            symbol="ETH/USDT",
            timeframe="1h",
            direction="short",
            price=100.0,
            plan=short_plan,
            strategy="double_line",
        )
        is not None
    )
    closed2 = broker.on_mark("ETH/USDT", 101.0)
    assert closed2 and closed2[0]["trade"]["outcome"] == "sl"
    assert closed2[0]["trade"]["pnl_usd"] < 0
    assert closed2[0]["trade"]["strategy"] == "double_line"


def test_paper_skips_duplicate_same_strategy(tmp_path, monkeypatch):
    broker = _reset_broker(tmp_path, monkeypatch)
    plan = {"stop_loss": 99, "take_profit_1": 102}
    assert broker.try_open_from_plan(
        symbol="BTC/USDT",
        timeframe="15m",
        direction="long",
        price=100,
        plan=plan,
        strategy="ai_plan",
    )
    assert (
        broker.try_open_from_plan(
            symbol="BTC/USDT",
            timeframe="15m",
            direction="long",
            price=100,
            plan=plan,
            strategy="ai_plan",
        )
        is None
    )


def test_paper_multi_strategy_same_symbol(tmp_path, monkeypatch):
    broker = _reset_broker(tmp_path, monkeypatch)
    plan = {"stop_loss": 99, "take_profit_1": 102}
    assert broker.try_open_from_plan(
        symbol="BTC/USDT",
        timeframe="15m",
        direction="long",
        price=100,
        plan=plan,
        strategy="ai_plan",
    )
    assert broker.try_open_from_plan(
        symbol="BTC/USDT",
        timeframe="15m",
        direction="long",
        price=100,
        plan=plan,
        strategy="double_line",
    )
    assert len(broker.state.positions) == 2

    broker.on_mark("BTC/USDT", 101.0)
    st = broker.status()
    assert st["unrealized_pnl"] > 0
    by = {b["strategy"]: b for b in st["by_strategy"]}
    assert by["ai_plan"]["open"] == 1
    assert by["double_line"]["open"] == 1
    assert by["ai_plan"]["unrealized_pnl"] > 0


def test_paper_cycle_switch_sync(tmp_path, monkeypatch):
    broker = _reset_broker(tmp_path, monkeypatch)
    events = broker.sync_cycle_target(
        symbol="ETH/USDT",
        timeframe="4h",
        target_position=0.5,
        price=2000.0,
        regime="bull",
    )
    assert len(events) == 1
    assert events[0]["type"] == "paper_open"
    assert events[0]["position"]["strategy"] == "cycle_switch"
    assert events[0]["position"]["direction"] == "long"
    assert events[0]["position"]["stop_loss"] is None

    # 同向只调权重，不再开仓
    assert (
        broker.sync_cycle_target(
            symbol="ETH/USDT",
            timeframe="4h",
            target_position=0.8,
            price=2100.0,
        )
        == []
    )
    assert len(broker.state.positions) == 1

    # 清仓
    closed = broker.sync_cycle_target(
        symbol="ETH/USDT",
        timeframe="4h",
        target_position=0.0,
        price=2200.0,
    )
    assert len(closed) == 1
    assert closed[0]["type"] == "paper_close"
    assert closed[0]["trade"]["outcome"] == "signal"
    assert closed[0]["trade"]["pnl_usd"] > 0
    assert len(broker.state.positions) == 0

    st = broker.status()
    by = {b["strategy"]: b for b in st["by_strategy"]}
    assert by["cycle_switch"]["trades"] == 1
    assert by["cycle_switch"]["realized_pnl"] > 0
