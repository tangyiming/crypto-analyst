"""盯盘收盘规则接入 Jack 三盘。"""

from dataclasses import replace
from datetime import datetime, timedelta

from analyst.compute.fibonacci import compute_fib
from analyst.compute.jack_levels import compute_jack_levels
from analyst.compute.jack_regime import compute_jack_regime
from analyst.compute.structure import Structure
from analyst.data.fetcher import Candle, CandleSeries
from analyst.monitor.jack_live import compute_monitor_jack
from analyst.monitor.rules import RuleConfig, evaluate_closed_bar_rules, is_ai_candidate


def _c(i: int, o: float, h: float, l: float, c: float, v: float = 1000) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1) + timedelta(minutes=15 * i),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
    )


def _series(candles: list[Candle]) -> CandleSeries:
    return CandleSeries(symbol="BTC/USDT", timeframe="15m", candles=candles)


def _flat(n: int, base: float = 100.0) -> list[Candle]:
    return [
        _c(i, base, base + 0.3, base - 0.3, base + (0.1 if i % 2 else -0.1))
        for i in range(n)
    ]


def _quiet_cfg() -> RuleConfig:
    return RuleConfig(
        enable_macd=False,
        enable_ema_stack=False,
        enable_boll=False,
        enable_volume=False,
        enable_structure_touch=False,
        enable_structure_flip=False,
        enable_fib_zone=False,
        enable_baseline=False,
        enable_cvd=False,
        enable_jack=True,
    )


def _sample_jack_regime():
    st = Structure(
        trend="up",
        supports=[62000.0],
        resistances=[67000.0],
        key_pivot=64000.0,
        recent_high=82828.0,
        recent_low=57758.0,
    )
    fib = compute_fib(st.recent_high, st.recent_low)
    jack = compute_jack_levels(
        current_price=72000.0,
        structure=st,
        fib=fib,
        daily_indicators={
            "macd": {"histogram": 10, "above_zero": True, "cross_signal": "golden"},
            "ema": {"ema7": 67000, "ema30": 62000},
        },
        symbol="BTC/USDT",
    )
    reg = compute_jack_regime(current_price=72000.0, jack=jack, structure=st)
    return jack, reg


def test_jack_regime_first_bar_silent_then_change_fires():
    series = _series(_flat(60))
    cfg = _quiet_cfg()
    jack, reg = _sample_jack_regime()
    range_reg = replace(reg, regime="range", regime_zh="震荡盘", trade_side="wait")

    events, state = evaluate_closed_bar_rules(
        series, {}, cfg, jack=jack, jack_regime=range_reg
    )
    assert not [e for e in events if e.rule.startswith("jack_")]
    assert state.get("jack_regime") == "range"

    strong = replace(reg, regime="strong_trend", regime_zh="强势盘", trade_side="long")
    events2, state2 = evaluate_closed_bar_rules(
        series, state, cfg, jack=jack, jack_regime=strong
    )
    hits = [e for e in events2 if e.rule == "jack_regime"]
    assert hits, "三盘切换应告警"
    assert "强势盘" in hits[0].title
    assert hits[0].direction == "long"

    events3, _ = evaluate_closed_bar_rules(
        series, state2, cfg, jack=jack, jack_regime=strong
    )
    assert not [e for e in events3 if e.rule == "jack_regime"]


def test_jack_setup_fires_on_new_flag():
    series = _series(_flat(60))
    cfg = _quiet_cfg()
    jack, reg = _sample_jack_regime()
    seeded = {
        "jack_regime": reg.regime,
        "jack_side": reg.trade_side,
        "jack_flags": [],
    }
    flagged = replace(reg, below_waist=True)
    events, state = evaluate_closed_bar_rules(
        series, seeded, cfg, jack=jack, jack_regime=flagged
    )
    setups = [e for e in events if e.rule == "jack_setup"]
    assert setups
    assert "腰斩" in "".join(setups[0].reasons)
    assert "below_waist" in (state.get("jack_flags") or [])


def test_jack_disabled_skips_events():
    series = _series(_flat(60))
    cfg = _quiet_cfg()
    cfg.enable_jack = False
    jack, reg = _sample_jack_regime()
    state = {"jack_regime": "range", "jack_side": "wait", "jack_flags": []}
    events, _ = evaluate_closed_bar_rules(
        series, state, cfg, jack=jack, jack_regime=reg
    )
    assert not [e for e in events if e.rule.startswith("jack_")]


def test_jack_regime_is_ai_quality_rule():
    assert is_ai_candidate(["jack_regime"])
    assert is_ai_candidate(["jack_setup"])


def test_compute_monitor_jack_returns_regime():
    candles = _flat(80, base=64000.0)
    series = CandleSeries(symbol="BTC/USDT", timeframe="15m", candles=candles)
    jack, reg = compute_monitor_jack(
        symbol="BTC/USDT",
        current_price=64000.0,
        worker_series=series,
    )
    assert jack.swing_high > 0
    assert reg.regime_zh
    assert reg.playbook_line
