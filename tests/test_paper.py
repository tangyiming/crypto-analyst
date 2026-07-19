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
        "MONITOR_PAPER_SOURCES", "ai_plan,cycle_switch"
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
            strategy="cycle_switch",
        )
        is not None
    )
    closed2 = broker.on_mark("ETH/USDT", 101.0)
    assert closed2 and closed2[0]["trade"]["outcome"] == "sl"
    assert closed2[0]["trade"]["pnl_usd"] < 0
    assert closed2[0]["trade"]["strategy"] == "cycle_switch"


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
        strategy="cycle_switch",
    )
    assert len(broker.state.positions) == 2

    broker.on_mark("BTC/USDT", 101.0)
    st = broker.status()
    assert st["unrealized_pnl"] > 0
    by = {b["strategy"]: b for b in st["by_strategy"]}
    assert by["ai_plan"]["open"] == 1
    assert by["cycle_switch"]["open"] == 1
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


# ── 风控熔断 ─────────────────────────────────────────────────
def _plan(direction="long", entry=100.0, stop=99.0, tp=102.0):
    return {
        "direction": direction,
        "entry_low": entry,
        "entry_high": entry,
        "stop_loss": stop,
        "take_profit_1": tp,
        "rr_ratio": 2,
        "rationale": "test",
    }


def _open(broker, symbol="BTC/USDT", strategy="ai_plan", **kw):
    return broker.try_open_from_plan(
        symbol=symbol,
        timeframe="15m",
        direction="long",
        price=100.0,
        plan=_plan(),
        strategy=strategy,
        **kw,
    )


def test_daily_loss_fuse_blocks_new_opens(tmp_path, monkeypatch):
    broker = _reset_broker(tmp_path, monkeypatch, PAPER_DAILY_LOSS_LIMIT_PCT="5")
    assert _open(broker, "BTC/USDT") is not None
    # 打到止损：亏 risk 0.1U ≈ 1% —— 未触发熔断
    broker.on_mark("BTC/USDT", 99.0)
    assert _open(broker, "ETH/USDT") is not None
    # 人为制造 >5% 当日回撤
    broker.state.cash -= 1.0
    broker._revalue()
    assert broker._daily_fuse_active() is True
    assert _open(broker, "SOL/USDT") is None  # 新仓被拒
    st = broker.status()
    assert st["risk_fuse"]["daily_fuse_active"] is True


def test_daily_fuse_resets_next_day(tmp_path, monkeypatch):
    broker = _reset_broker(tmp_path, monkeypatch, PAPER_DAILY_LOSS_LIMIT_PCT="5")
    broker.state.cash -= 1.0
    broker._revalue()
    assert broker._daily_fuse_active() is True
    # 模拟跨日：改锚到昨天 → 复位
    broker.state.day_anchor = "2000-01-01"
    broker.state.daily_fuse_date = "2000-01-01"
    assert broker._daily_fuse_active() is False
    assert _open(broker, "SOL/USDT") is not None


def test_strategy_dd_fuse_disables_and_clears(tmp_path, monkeypatch):
    broker = _reset_broker(
        tmp_path, monkeypatch, PAPER_STRATEGY_DD_DISABLE_PCT="10",
        PAPER_DAILY_LOSS_LIMIT_PCT="0",
    )
    broker._record_strategy_pnl("cycle_switch", -1.5)
    assert "cycle_switch" in broker.state.disabled_strategies
    opened = broker.try_open_from_plan(
        symbol="BTC/USDT", timeframe="15m", direction="long",
        price=100.0, plan=_plan(), strategy="cycle_switch",
    )
    assert opened is None
    # 其他策略不受影响
    assert _open(broker, "ETH/USDT", strategy="ai_plan") is not None
    # 手动恢复
    cleared = broker.clear_strategy_fuse("cycle_switch")
    assert cleared == ["cycle_switch"]
    assert broker.state.disabled_strategies == []
    broker._record_strategy_pnl("cycle_switch", 0.01)
    assert "cycle_switch" not in broker.state.disabled_strategies


def test_gross_exposure_cap_blocks(tmp_path, monkeypatch):
    broker = _reset_broker(
        tmp_path, monkeypatch, PAPER_MAX_GROSS_EXPOSURE="1.5",
        PAPER_DAILY_LOSS_LIMIT_PCT="0",
    )
    assert _open(broker, "BTC/USDT") is not None
    assert _open(broker, "ETH/USDT") is None
    st = broker.status()
    assert st["risk_fuse"]["max_gross_exposure"] == 1.5


# ── 通用目标仓位同步（xs_momentum 等） ───────────────────────
def test_sync_target_position_open_flip_close(tmp_path, monkeypatch):
    broker = _reset_broker(
        tmp_path, monkeypatch,
        MONITOR_PAPER_SOURCES="cycle_switch,xs_momentum,funding_carry",
        PAPER_DAILY_LOSS_LIMIT_PCT="0",
    )
    ev = broker.sync_target_position(
        strategy="xs_momentum", symbol="SOL/USDT", timeframe="4h",
        target_position=0.5, price=100.0, rationale="xs weight=+0.50",
    )
    assert ev and ev[0]["type"] == "paper_open"
    assert broker.state.positions[0].strategy == "xs_momentum"
    assert broker.state.positions[0].direction == "long"
    assert broker.sync_target_position(
        strategy="xs_momentum", symbol="SOL/USDT", timeframe="4h",
        target_position=0.5, price=101.0,
    ) == []
    ev2 = broker.sync_target_position(
        strategy="xs_momentum", symbol="SOL/USDT", timeframe="4h",
        target_position=-0.25, price=102.0,
    )
    types = [e["type"] for e in ev2]
    assert "paper_close" in types and "paper_open" in types
    assert broker.state.positions[0].direction == "short"
    ev3 = broker.sync_target_position(
        strategy="xs_momentum", symbol="SOL/USDT", timeframe="4h",
        target_position=0.0, price=100.0,
    )
    assert [e["type"] for e in ev3] == ["paper_close"]
    assert not broker.state.positions


def test_sync_target_respects_sources_gate(tmp_path, monkeypatch):
    broker = _reset_broker(
        tmp_path, monkeypatch, MONITOR_PAPER_SOURCES="cycle_switch",
    )
    assert broker.sync_target_position(
        strategy="xs_momentum", symbol="SOL/USDT", timeframe="4h",
        target_position=1.0, price=100.0,
    ) == []


# ── 资金费套利台账 ───────────────────────────────────────────
def test_carry_open_accrue_close(tmp_path, monkeypatch):
    broker = _reset_broker(
        tmp_path, monkeypatch,
        MONITOR_PAPER_SOURCES="funding_carry",
        MONITOR_PAPER_FEE_BPS="4",
        PAPER_DAILY_LOSS_LIMIT_PCT="0",
    )
    ev = broker.sync_carry(symbol="BTC/USDT", active=True, notional=5.0, note="test")
    assert ev and ev[0]["type"] == "paper_carry_open"
    assert "BTC/USDT" in broker.state.carry_book
    cash_after_open = broker.state.cash
    assert cash_after_open < 10.0

    import time as _t
    base = int(_t.time() * 1000) + 1000
    got = broker.apply_carry_funding("BTC/USDT", [
        (base, 0.0001), (base + 1, 0.0001), (base + 2, -0.00005),
    ])
    assert abs(got - 5.0 * 0.00015) < 1e-9
    assert abs(broker.state.carry_book["BTC/USDT"]["accrued"] - got) < 1e-12
    assert broker.apply_carry_funding("BTC/USDT", [(base, 0.0001)]) == 0.0

    ev2 = broker.sync_carry(symbol="BTC/USDT", active=False, notional=0.0)
    assert ev2 and ev2[0]["type"] == "paper_carry_close"
    assert "BTC/USDT" not in broker.state.carry_book
    trades = [t for t in broker.state.trades if t.strategy == "funding_carry"]
    assert len(trades) == 1 and trades[0].direction == "carry"


def test_carry_open_only_when_signal_and_idempotent(tmp_path, monkeypatch):
    broker = _reset_broker(
        tmp_path, monkeypatch, MONITOR_PAPER_SOURCES="funding_carry",
        PAPER_DAILY_LOSS_LIMIT_PCT="0",
    )
    assert broker.sync_carry(symbol="ETH/USDT", active=False, notional=5.0) == []
    broker.sync_carry(symbol="ETH/USDT", active=True, notional=5.0)
    assert broker.sync_carry(symbol="ETH/USDT", active=True, notional=5.0) == []
