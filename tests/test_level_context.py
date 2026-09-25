"""关键位语境测试。"""

from datetime import datetime, timedelta

from analyst.compute.fibonacci import compute_fib
from analyst.compute.level_context import (
    LevelSnapshot,
    compute_level_context,
    distance_pct,
    in_zone,
    snapshot_from_series,
    supplement_resistances,
    tf_priority,
)
from analyst.compute.structure import Structure
from analyst.data.fetcher import Candle, CandleSeries


def _series(closes: list[float], *, high_bump: float = 2.0) -> CandleSeries:
    candles: list[Candle] = []
    t0 = datetime(2024, 1, 1)
    for i, c in enumerate(closes):
        candles.append(
            Candle(
                timestamp=t0 + timedelta(hours=4 * i),
                open=c,
                high=c + high_bump,
                low=c - high_bump,
                close=c,
                volume=1000.0,
            )
        )
    return CandleSeries(symbol="BTC/USDT", timeframe="4h", candles=candles)


def test_distance_pct_above_and_below():
    assert distance_pct(105, 100) == 5.0
    assert distance_pct(95, 100) == -5.0


def test_in_zone():
    assert in_zone(50, 40, 60)
    assert not in_zone(70, 40, 60)


def test_snapshot_requires_enough_bars():
    assert snapshot_from_series(_series([100.0] * 10)) is None
    assert snapshot_from_series(_series([100.0 + i for i in range(40)])) is not None


def test_dip_buy_in_fib_retracement_uptrend():
    closes = [80 + i * 0.5 for i in range(50)]
    closes[-1] = 104
    snap = snapshot_from_series(_series(closes))
    assert snap is not None
    snap.structure.trend = "up"
    fib = compute_fib(snap.structure.recent_high, snap.structure.recent_low)
    mid = (fib.retr_500 + fib.retr_618) / 2
    ctx = compute_level_context(mid, snap)
    assert ctx["zone"]["dip_buy"] is True
    assert ctx["zone"]["label"] in ("可低吸", "震荡分向")


def test_try_short_near_resistance_downtrend():
    closes = [120 - i * 0.4 for i in range(50)]
    snap = snapshot_from_series(_series(closes))
    assert snap is not None
    snap.structure = Structure(
        trend="down",
        supports=[90.0, 85.0],
        resistances=[100.0, 105.0, 110.0],
        key_pivot=100.0,
        recent_high=120.0,
        recent_low=80.0,
    )
    snap.fib = compute_fib(120.0, 80.0)
    ctx = compute_level_context(100.1, snap)
    assert ctx["zone"]["try_short"] is True
    assert len(ctx["resistances"]) <= 3


def test_tf_priority():
    assert tf_priority("4h") > tf_priority("15m")


def test_supplement_resistances_when_structure_is_above_all_pivots():
    """价格已在全部结构阻力之上时，用近阻力与枢轴阻力补行，不发明价位。"""
    rows = supplement_resistances(
        121.25,
        [],
        nearest_resistance=128.3,
        pivot_resistances=(128.3, 110.0),
    )
    assert [r["price"] for r in rows] == [128.3]
    assert rows[0]["dist_pct"] < 0


def test_supplement_resistances_keeps_structure_and_caps_nearest_three():
    rows = supplement_resistances(
        154.0,
        [{"kind": "resistance", "price": 163.82, "dist_pct": -6.0, "near": False}],
        nearest_resistance=157.09,
        pivot_resistances=(155.25, 163.82, 170.0),
    )
    assert [round(r["price"], 2) for r in rows] == [155.25, 157.09, 163.82]


def test_supplement_resistances_empty_when_nothing_above_price():
    assert supplement_resistances(
        100.0,
        [{"kind": "resistance", "price": 90.0, "dist_pct": 11.1, "near": False}],
        nearest_resistance=95.0,
        pivot_resistances=(80.0,),
    ) == []
