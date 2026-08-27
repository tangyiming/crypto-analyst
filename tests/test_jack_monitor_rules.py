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
        enable_eric=False,
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


def test_jack_waist_uses_cycle_high_and_hist_decel():
    """腰斩线 = 周期最高点×0.5（非最近波段高点）；MACD 归零减速 = 柱缩短。"""
    from datetime import datetime, timedelta

    from analyst.compute.jack_regime import _macd_decel_to_zero, compute_jack_regime
    from analyst.compute.jack_levels import compute_jack_levels
    from analyst.compute.structure import detect_structure
    from analyst.data.fetcher import Candle, CandleSeries

    def c(i, close, tf_hours=24):
        return Candle(
            timestamp=datetime(2025, 8, 1) + timedelta(hours=i * tf_hours),
            open=close, high=close * 1.01, low=close * 0.99, close=close, volume=1.0,
        )

    # 日线：前 100 根冲到 126000 的周期顶，之后跌到 63000 附近横盘
    closes = [60000 + i * 660 for i in range(100)] + [126000 - i * 630 for i in range(100)] + [63000 + (i % 3) * 200 for i in range(60)]
    daily = CandleSeries("BTC/USDT", "1d", [c(i, x) for i, x in enumerate(closes)])
    h4 = CandleSeries("BTC/USDT", "4h", [c(i, 63000 + (i % 5) * 100, 4) for i in range(200)])
    structure = detect_structure(h4)
    jack = compute_jack_levels(current_price=63030.0, structure=structure, primary_series=h4, symbol="BTC/USDT")
    reg = compute_jack_regime(current_price=63030.0, jack=jack, structure=structure, primary_series=h4, daily_series=daily, h4_series=h4)
    assert reg.waist_line is not None and abs(reg.waist_line - 126000 * 1.01 / 2) < 1
    assert reg.below_waist

    # 柱缩短：急跌后横住，负柱开始向零收敛 → 归零减速（DIF 可能仍在零下）
    down = [70000 - i * 300 for i in range(60)] + [52000] * 10
    cands = [c(i, x, 8) for i, x in enumerate(down)]
    # 横住前后几根里必有一根：柱为负且较前一根缩短
    assert any(_macd_decel_to_zero(CandleSeries("BTC/USDT", "8h", cands[:n])) for n in range(56, 71))
    # 加速下跌：负柱持续放大，不应判为减速
    accel = [70000 - (i * i) * 6 for i in range(70)]
    still = CandleSeries("BTC/USDT", "8h", [c(i, x, 8) for i, x in enumerate(accel)])
    assert not _macd_decel_to_zero(still)
