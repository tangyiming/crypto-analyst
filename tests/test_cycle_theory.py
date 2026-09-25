"""Wolfy 周期日历 + 狼波指数测试。"""

from datetime import datetime, timedelta

from analyst.compute.cycle_theory import (
    WOLFY_BEAR_BOTTOMS,
    WOLFY_BULL_DAYS,
    WOLFY_BEAR_DAYS,
    compute_wolfy_wave,
    evaluate_cycle_outlook,
    wolfy_calendar_phase,
)
from analyst.data.fetcher import Candle, CandleSeries


def _c(i: int, close: float) -> Candle:
    return Candle(
        timestamp=datetime(2024, 1, 1) + timedelta(days=i),
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=1000,
    )


def test_wolfy_bull_phase_from_2022_bottom():
    # 2022-11-21 底后 100 天应在牛市
    ts = WOLFY_BEAR_BOTTOMS[-1] + timedelta(days=100)
    st = wolfy_calendar_phase(ts)
    assert st.phase == "bull"
    assert st.phase_day == 101
    assert st.days_to_milestone == WOLFY_BULL_DAYS - 100


def test_wolfy_bear_phase_after_bull_top():
    bottom = WOLFY_BEAR_BOTTOMS[-1]
    bull_top = bottom + timedelta(days=WOLFY_BULL_DAYS)
    ts = bull_top + timedelta(days=50)
    st = wolfy_calendar_phase(ts)
    assert st.phase == "bear"
    assert st.next_milestone.kind == "bear_bottom"
    assert st.days_to_milestone == WOLFY_BEAR_DAYS - 50


def test_wolfy_oct_2025_near_bull_top():
    # Wolfy 图预测顶约 2025-10-06；底 2022-11-21 + 1064 ≈ 2025-10-19
    ts = datetime(2025, 10, 6)
    st = wolfy_calendar_phase(ts)
    assert st.phase == "bull"
    assert st.days_to_milestone <= 30


def test_wolfy_wave_extreme_hot():
    # 连涨 30 天 → RSI 应偏高
    closes = [100.0 * (1.02 ** i) for i in range(40)]
    w = compute_wolfy_wave(closes)
    assert w.rsi >= 65
    assert w.heat in ("hot", "extreme_hot")


def test_build_wolfy_timeline_has_segments():
    from analyst.compute.cycle_theory import build_wolfy_timeline

    ts = datetime(2025, 10, 6)
    tl = build_wolfy_timeline(ts)
    assert tl["segments"]
    assert 0 <= tl["now_pct"] <= 100
    assert any(m["kind"] == "halving" for m in tl["markers"])


def test_evaluate_cycle_outlook_has_alerts_near_milestone():
    bottom = WOLFY_BEAR_BOTTOMS[-1]
    bull_top = bottom + timedelta(days=WOLFY_BULL_DAYS)
    ts = bull_top - timedelta(days=20)
    candles = [_c(i, 50000 + i * 10) for i in range(60)]
    candles[-1] = Candle(
        timestamp=ts,
        open=90000,
        high=91000,
        low=89000,
        close=90000,
        volume=1,
    )
    outlook = evaluate_cycle_outlook(
        CandleSeries("BTC/USDT", "1d", candles),
        as_of=ts,
    )
    assert outlook.calendar.phase == "bull"
    assert outlook.alerts
    assert "转折点" in outlook.alerts[0]
    assert "20 天" in outlook.alerts[0]
    assert any("见顶" in a for a in outlook.alerts)


def test_evaluate_cycle_outlook_always_has_countdown():
    from analyst.compute.cycle_theory import outlook_to_api_dict

    ts = WOLFY_BEAR_BOTTOMS[-1] + timedelta(days=200)
    outlook = evaluate_cycle_outlook(
        CandleSeries("BTC/USDT", "1d", [_c(i, 50000) for i in range(40)]),
        as_of=ts,
    )
    assert "转折点" in outlook.alerts[0]
    payload = outlook_to_api_dict(outlook)
    assert payload["countdown"]["days"] > 0
    assert payload["countdown"]["label"]
    assert payload["wave_index"] is None


def test_wave_index_matches_block_formula():
    from analyst.compute.cycle_theory import HALVING_INTERVAL, WAVE_BULL_HALF, wave_index_at, wave_index_state

    halving = HALVING_INTERVAL * 4
    assert wave_index_at(halving) == 0.5
    assert wave_index_at(halving - WAVE_BULL_HALF) == 0.0
    assert wave_index_at(halving + WAVE_BULL_HALF) == 1.0

    bull = wave_index_state(halving)
    assert bull.phase == "bull"
    assert bull.blocks_to_turn == WAVE_BULL_HALF

    bear = wave_index_state(halving + WAVE_BULL_HALF + 1000)
    assert bear.phase == "bear"
    assert bear.value < 1.0
    assert "理论熊底" in bear.turn_label


def test_wave_chart_covers_top_bottom_and_halving():
    from analyst.compute.cycle_theory import wave_chart

    chart = wave_chart(968_562, now_ts=1_700_000_000)
    values = [p["value"] for p in chart["points"]]
    kinds = {m["kind"] for m in chart["markers"]}
    assert min(values) == 0.0
    assert max(values) == 1.0
    assert any(abs(m["value"] - 0.5) < 1e-9 for m in chart["markers"] if m["kind"] == "halving")
    assert {"bottom", "top", "halving"} <= kinds
    assert any(p["future"] for p in chart["points"])
    assert any(not p["future"] for p in chart["points"])
