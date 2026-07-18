"""经典组合回测测试：成本、不偷看未来、策略仓位逻辑。"""

import math
from datetime import datetime, timedelta

from analyst.backtest.classic import (
    CostModel,
    label_regimes,
    positions_boll_mr,
    positions_donchian,
    positions_ema_cross,
    run_classic_backtest,
    simulate,
)
from analyst.data.fetcher import Candle


def _c(i: int, o: float, h: float, l: float, c: float, v: float = 1000) -> Candle:
    return Candle(
        timestamp=datetime(2024, 1, 1) + timedelta(hours=4 * i),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
    )


def _trend_up(n: int, start: float = 100.0, step: float = 1.0) -> list[Candle]:
    out = []
    px = start
    for i in range(n):
        out.append(_c(i, px, px + step * 1.2, px - step * 0.2, px + step))
        px += step
    return out


def _flat(n: int, base: float = 100.0) -> list[Candle]:
    return [
        _c(i, base, base + 0.5, base - 0.5, base + (0.2 if i % 2 else -0.2))
        for i in range(n)
    ]


def test_buy_hold_matches_price_change_without_cost():
    candles = _trend_up(50)
    rep = run_classic_backtest(
        candles, strategy="buy_hold", symbol="T", timeframe="4h",
        cost=CostModel(fee_pct=0.0, slippage_pct=0.0),
    )
    expect = (candles[-1].close / candles[0].close - 1.0) * 100
    assert math.isclose(rep.total_return_pct, expect, rel_tol=1e-9)


def test_costs_reduce_equity():
    candles = _trend_up(50)
    # 每根都翻转仓位 → 成本拉满
    flip = [1.0 if i % 2 else -1.0 for i in range(len(candles))]
    no_cost = simulate(
        candles, flip, strategy="x", symbol="T", timeframe="4h",
        cost=CostModel(fee_pct=0.0, slippage_pct=0.0),
    )
    with_cost = simulate(
        candles, flip, strategy="x", symbol="T", timeframe="4h",
        cost=CostModel(fee_pct=0.1, slippage_pct=0.05),
    )
    assert with_cost.total_return_pct < no_cost.total_return_pct
    assert with_cost.trades > 40


def test_position_takes_effect_next_bar():
    # 第 i 根的仓位吃的是 i-1→i 的收益：最后一根暴涨但当根才开仓 → 吃不到
    candles = _flat(10)
    candles.append(_c(10, 100, 130, 100, 130))
    positions = [0.0] * 10 + [1.0]
    rep = simulate(
        candles, positions, strategy="x", symbol="T", timeframe="4h",
        cost=CostModel(fee_pct=0.0, slippage_pct=0.0),
    )
    assert rep.total_return_pct < 1.0  # 只承担 flat 段的微小波动


def test_donchian_goes_long_in_uptrend_and_flat_in_chop():
    up = positions_donchian(_trend_up(120), entry_n=20, exit_n=10)
    assert up[-1] == 1.0
    chop = positions_donchian(_flat(120), entry_n=20, exit_n=10)
    assert all(p == 0.0 for p in chop[30:])


def test_ema_cross_short_in_downtrend():
    candles = []
    px = 500.0
    for i in range(300):
        candles.append(_c(i, px, px + 0.5, px - 2.5, px - 2))
        px -= 2
    pos = positions_ema_cross(candles, fast=20, slow=60)
    assert pos[-1] == -1.0
    pos_lo = positions_ema_cross(candles, fast=20, slow=60, long_only=True)
    assert pos_lo[-1] == 0.0


def test_boll_mr_buys_dip():
    candles = _flat(60)
    # 突然砸出 z < -2 的深坑
    candles.append(_c(60, 100, 100, 92, 93))
    pos = positions_boll_mr(candles, period=20, entry_z=2.0)
    assert pos[-1] == 1.0


def test_halving_phase_boundaries():
    from analyst.backtest.classic import halving_phase

    h = datetime(2024, 4, 19)
    assert halving_phase(h + timedelta(days=100)) == "bull"
    assert halving_phase(h + timedelta(days=600)) == "bear"
    assert halving_phase(h + timedelta(days=1000)) == "accum"


def test_build_cycle_regime_double_confirmation():
    from analyst.backtest.classic import build_cycle_regime

    # 日历熊区间（减半后 600 天 ≈ 2025-12），价格远高于均线 → 不算熊
    start = datetime(2025, 12, 1)
    up = []
    px = 100.0
    for i in range(1500):
        up.append(Candle(
            timestamp=start + timedelta(hours=4 * i),
            open=px, high=px + 1.4, low=px - 0.2, close=px + 1, volume=1,
        ))
        px += 1
    regime_up = build_cycle_regime(up, ma_period=200)
    assert regime_up[up[-1].timestamp] == "bull"

    # 同一日历熊区间 + 价格持续跌破均线 → 双确认为熊
    down = []
    px = 5000.0
    for i in range(1500):
        down.append(Candle(
            timestamp=start + timedelta(hours=4 * i),
            open=px, high=px + 0.4, low=px - 2.4, close=px - 2, volume=1,
        ))
        px -= 2
    regime_down = build_cycle_regime(down, ma_period=200)
    assert regime_down[down[-1].timestamp] == "bear"


def test_cycle_switch_positions():
    from analyst.backtest.classic import positions_cycle_switch

    candles = _trend_up(120)
    # 全程牛市 → 唐奇安逻辑，趋势尾部持多
    bull_regime = {c.timestamp: "bull" for c in candles}
    pos = positions_cycle_switch(candles, bull_regime, entry_n=20, exit_n=10)
    assert pos[-1] == 1.0

    # 全程熊市：不持多；z 冲高才开空（半仓）
    flat = _flat(60)
    flat.append(_c(60, 100, 109, 100, 108))  # z > 1.5 的反弹
    bear_regime = {c.timestamp: "bear" for c in flat}
    pos2 = positions_cycle_switch(flat, bear_regime, entry_n=20, exit_n=10)
    assert pos2[-1] == -0.5
    assert all(p <= 0 for p in pos2)

    # 熊市翻回牛（保险丝）：空单立即清掉
    flat2 = _flat(62)
    mixed = {c.timestamp: "bear" for c in flat2[:61]}
    mixed[flat2[61].timestamp] = "accum"
    flat2[60] = _c(60, 100, 109, 100, 108)
    pos3 = positions_cycle_switch(flat2, mixed, entry_n=20, exit_n=10)
    assert pos3[60] == -0.5 and pos3[61] == 0.0


def _trend_down(n: int, start: float = 500.0, step: float = 2.0) -> list[Candle]:
    out = []
    px = start
    for i in range(n):
        out.append(_c(i, px, px + step * 0.25, px - step * 1.25, px - step))
        px -= step
    return out


def test_cycle_switch_bear_trend_short_leg():
    from analyst.backtest.classic import positions_cycle_switch

    # 熊市 + 持续阴跌（z 常年为负，反弹空腿永不触发）→ 破位空腿接管
    candles = _trend_down(120)
    bear = {c.timestamp: "bear" for c in candles}
    pos = positions_cycle_switch(candles, bear, entry_n=20, exit_n=10)
    assert pos[-1] == -0.5
    assert all(p <= 0 for p in pos)
    # 关掉破位空腿 = 旧行为：全程空仓
    pos_off = positions_cycle_switch(
        candles, bear, entry_n=20, exit_n=10, bear_trend_short=False
    )
    assert all(p == 0.0 for p in pos_off)


def test_bull_trend_long_only():
    from analyst.backtest.classic import positions_bull_trend

    pos = positions_bull_trend(_trend_up(120), entry_n=20, exit_n=10)
    assert pos[-1] == 1.0
    pos_down = positions_bull_trend(_trend_down(120), entry_n=20, exit_n=10)
    assert all(p == 0.0 for p in pos_down)


def test_bear_defense_shorts_and_never_longs():
    from analyst.backtest.classic import positions_bear_defense

    # 阴跌 → 破位空
    pos = positions_bear_defense(_trend_down(120), entry_n=20, exit_n=10)
    assert pos[-1] == -0.5
    assert all(p <= 0 for p in pos)
    # 震荡冲高（z > 1.5）→ 反弹空
    flat = _flat(60)
    flat.append(_c(60, 100, 109, 100, 108))
    pos2 = positions_bear_defense(flat, entry_n=20, exit_n=10)
    assert pos2[-1] == -0.5
    assert all(p <= 0 for p in pos2)


def test_chop_range_half_size_both_sides():
    from analyst.backtest.classic import positions_chop_range

    dip = _flat(60)
    dip.append(_c(60, 100, 100, 92, 93))     # z < -2 深坑 → 接多半仓
    assert positions_chop_range(dip, period=20)[-1] == 0.5
    spike = _flat(60)
    spike.append(_c(60, 100, 108, 100, 107))  # z > +2 冲高 → 做空半仓
    assert positions_chop_range(spike, period=20)[-1] == -0.5


def test_chop_range_atr_stop_cuts_tail():
    """震荡中突发单边暴跌：ATR 止损把逆势多仓砍掉，而非扛到底。"""
    from analyst.backtest.classic import positions_chop_range

    candles = _flat(60)
    candles.append(_c(60, 100, 100, 92, 93))          # 深坑 → 接多 0.5
    px = 93.0
    for i in range(61, 75):                            # 持续崩盘，远超 3×ATR
        candles.append(_c(i, px, px + 0.5, px - 5.5, px - 5))
        px -= 5
    pos_stop = positions_chop_range(candles, period=20, stop_atr=3.0)
    assert pos_stop[60] == 0.5
    assert pos_stop[-1] == 0.0                         # 已被止损
    # 无止损版：z 一直为负，多仓扛满全程
    pos_naked = positions_chop_range(candles, period=20, stop_atr=0.0)
    assert pos_naked[-1] == 0.5


def test_evaluate_cycle_switch_no_false_change_on_restart():
    """持有中重启（prev_position 参数=0）不应误报仓位变化。"""
    from analyst.compute.strategies.cycle_switch import evaluate_cycle_switch
    from analyst.data.fetcher import CandleSeries

    candles = _trend_up(80, step=2.0)
    regime = {c.timestamp: "bull" for c in candles}
    sig = evaluate_cycle_switch(
        CandleSeries("T", "4h", candles),
        regime,
        prev_position=0.0,
    )
    assert sig.target_position == 1.0
    assert sig.market_regime == "bull"
    assert sig.prev_position == 1.0
    assert sig.changed is False
    assert any("唐奇安" in r for r in sig.reasons)



def test_label_regimes_basic():
    candles = _trend_up(250, step=1.0)
    labels = label_regimes(candles, lookback=100, thresh=0.15)
    assert labels[-1] == "bull"
    labels_flat = label_regimes(_flat(250), lookback=100, thresh=0.15)
    assert labels_flat[-1] == "chop"
