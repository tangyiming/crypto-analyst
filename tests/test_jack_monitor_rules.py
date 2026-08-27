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


def test_jack_level_formulas_from_tweets():
    """2026-08 推文里可复现的公式：日内回踩位（振幅修正）、自然月 BOLL、周期斐波梯子、整数关口屏障。"""
    from datetime import datetime, timedelta

    from analyst.compute.jack_regime import (
        _cycle_fib,
        _pullback_levels,
        _resample_calendar_month,
        _round_barriers,
    )
    from analyst.data.fetcher import Candle, CandleSeries

    def c(i, lo, hi, close=None, hours=1, base=datetime(2026, 8, 20, 18)):
        cl = close if close is not None else (lo + hi) / 2
        return Candle(timestamp=base + timedelta(hours=i * hours), open=cl, high=hi, low=lo, close=cl, volume=1.0)

    # 先跌到 72280，再冲高 79556，中途回踩低 74214（最后一波冲高的起点）→ 用 74214 而不是 72280
    lows = [72500, 72280, 72600, 72459, 72700, 73000, 73635, 74347, 74214, 74483, 74872, 75066, 75577, 76253, 77255, 77601, 76393, 76538, 76237, 76539, 77157, 76757, 77163, 77300]
    highs = [l + 600 for l in lows]
    highs[14] = 79556
    bars = [c(i, lo, hi) for i, (lo, hi) in enumerate(zip(lows, highs))]
    pb50, pb618, used, note = _pullback_levels(CandleSeries("BTC/USDT", "1h", bars))
    assert used == 74214 and "改用日内回踩低" in note
    assert abs(pb618 - (79556 - (79556 - 74214) * 0.618)) < 1e-6  # ≈76255，Jack 76267，实际低 76237
    # 振幅小（<6%）时用 24h 低点本身
    calm = [c(i, 2306 + i * 2, 2306 + i * 2 + 60) for i in range(24)]
    calm[8] = c(8, 2370, 2449)
    _, pb, used2, note2 = _pullback_levels(CandleSeries("ETH/USDT", "1h", calm))
    assert used2 == 2306 and note2 == "" and abs(pb - (2449 - (2449 - 2306) * 0.618)) < 1e-6  # 2361

    # 自然月重采样：含当月未收盘 K，按 (年,月) 分组
    daily = [c(i, 100 + i, 110 + i, hours=24, base=datetime(2025, 1, 1)) for i in range(600)]
    m = _resample_calendar_month(CandleSeries("X/USDT", "1d", daily))
    assert m is not None and m.candles[-1].timestamp.month == daily[-1].timestamp.month
    assert all(a.timestamp < b.timestamp for a, b in zip(m.candles, m.candles[1:]))

    # 周期斐波：高 4957.67（idx 400）→ 其后低 1503.6；上一轮熊底 881 在高点之前
    seq = [881.0 + i * 10 for i in range(400)] + [4957.67] + [4957.67 - i * 30 for i in range(1, 116)] + [1503.6] + [1700.0] * 30
    ds = [Candle(timestamp=datetime(2023, 1, 1) + timedelta(days=i), open=x, high=x, low=x, close=x, volume=1) for i, x in enumerate(seq)]
    hi, lo, f382, f500, f618, bear = _cycle_fib(CandleSeries("ETH/USDT", "1d", ds))
    assert (hi, lo) == (4957.67, 1503.6)
    assert abs(f382 - 2823.05) < 0.1 and abs(f500 - 3230.64) < 0.1 and abs(f618 - 3638.22) < 0.1
    assert abs(bear - (4957.67 - (4957.67 - 881.0) * 0.618)) < 1e-6

    # 整数关口：SOL 97 → 关口 100，屏障 96–98，首压 104–106；BTC 78,746 → 100,000
    lvl, below, above = _round_barriers(97.0)
    assert lvl == 100 and abs(below[0] - 96) < 1e-9 and abs(above[1] - 106) < 1e-9
    assert _round_barriers(78746.0)[0] == 100000


def test_jack_4h_boll_pivots_extension():
    from datetime import datetime, timedelta

    from analyst.compute.jack_regime import _boll_4h, _extension_targets, _pivot_levels
    from analyst.data.fetcher import Candle, CandleSeries

    def c(i, lo, hi, hours=4):
        return Candle(timestamp=datetime(2026, 8, 1) + timedelta(hours=i * hours), open=(lo + hi) / 2, high=hi, low=lo, close=(lo + hi) / 2, volume=1.0)

    # 4h：两个明显枢轴低 87 / 93.2，枢轴高 102.8；现价 97 → 支撑 [93.2, 87]，阻力 [102.8]
    lows = [90, 89, 88, 87, 88.5, 90, 92, 95, 96, 97, 96.5, 95.5, 93.2, 94, 95, 96, 96.5, 97, 96.8, 96.9, 97.0, 96.6, 96.7, 96.9]
    highs = [92, 91, 90, 89.5, 91, 93, 95, 98, 100, 102.8, 101, 99, 96, 97, 98, 99, 99.5, 99.8, 99.2, 99.1, 99.3, 98.8, 98.9, 99.0]
    s = CandleSeries("SOL/USDT", "4h", [c(i, lo, hi) for i, (lo, hi) in enumerate(zip(lows, highs))])
    sup, res = _pivot_levels(s, 97.0)
    assert sup[0] == 93.2 and 87 in sup
    assert 102.8 in res and res[0] == 99.8  # 99.8 是更近的小枢轴高，102.8 在其后
    lo_, mid, up = _boll_4h(s)
    assert lo_ is not None and lo_ < mid < up
    # 延伸：基准低 87、24h 高 103.26 → 1.5 = 111.39，1.618 = 113.3
    hourly = CandleSeries("SOL/USDT", "1h", [c(i, 95, 103.26 if i == 20 else 100, hours=1) for i in range(24)])
    e150, e1618 = _extension_targets(hourly, 87.0)
    assert abs(e150 - (103.26 + 16.26 * 0.5)) < 1e-6 and abs(e1618 - (103.26 + 16.26 * 0.618)) < 1e-6
