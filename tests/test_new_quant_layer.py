"""新量化层测试：资金费回测、波动率目标、横截面动量、资金费套利、CVD/OI 规则。"""

from datetime import datetime, timedelta

from analyst.backtest.classic import (
    CostModel,
    apply_vol_target,
    rolling_window_report,
    simulate,
)
from analyst.compute.strategies.funding_carry import (
    FundingCarryConfig,
    backtest_funding_carry,
    current_carry_status,
)
from analyst.compute.strategies.xs_momentum import (
    XsMomentumConfig,
    backtest_xs_momentum,
    weights_xs_momentum,
)
from analyst.data.fetcher import Candle, CandleSeries
from analyst.monitor.rules import RuleConfig, evaluate_closed_bar_rules, evaluate_oi_rules

T0 = datetime(2024, 1, 1)


def _c(i, o, h, l, c, v=1000.0, tbv=None):
    return Candle(
        timestamp=T0 + timedelta(hours=4 * i),
        open=o, high=h, low=l, close=c, volume=v,
        taker_buy_volume=v / 2 if tbv is None else tbv,
    )


def _trend(n, start=100.0, step=1.0):
    out, px = [], start
    for i in range(n):
        out.append(_c(i, px, px + abs(step) * 1.2, px - abs(step) * 0.3, px + step))
        px += step
    return out


# ── simulate 资金费 ─────────────────────────────────────────
def test_simulate_funding_long_pays_short_receives():
    candles = [_c(i, 100, 100.5, 99.5, 100) for i in range(10)]  # 价格不动
    # 每根 4h 一档结算，费率 +0.01%
    funding = [
        (int(c.timestamp.timestamp() * 1000), 0.0001) for c in candles[1:]
    ]
    nocost = CostModel(fee_pct=0.0, slippage_pct=0.0)
    long_rep = simulate(candles, [1.0] * 10, strategy="x", symbol="T",
                        timeframe="4h", cost=nocost, funding=funding)
    short_rep = simulate(candles, [-1.0] * 10, strategy="x", symbol="T",
                         timeframe="4h", cost=nocost, funding=funding)
    assert long_rep.funding_pnl_pct < 0      # 多头付
    assert short_rep.funding_pnl_pct > 0     # 空头收
    assert long_rep.total_return_pct < 0 < short_rep.total_return_pct


# ── 波动率目标化 ────────────────────────────────────────────
def test_vol_target_scales_down_high_vol():
    # 高波动段：日内 8% 振幅
    candles = []
    px = 100.0
    for i in range(80):
        step = 8.0 if i % 2 else -8.0
        candles.append(_c(i, px, px + 9, px - 9, px + step))
        px += step
    pos = [1.0] * len(candles)
    scaled = apply_vol_target(candles, pos, timeframe="4h",
                              target_annual_vol=0.30, lookback=20)
    assert scaled[-1] < 0.5  # 高波动被显著降杠杆
    # 低波动段几乎不缩
    calm = [_c(i, 100, 100.15, 99.85, 100 + (0.05 if i % 2 else -0.05)) for i in range(80)]
    scaled2 = apply_vol_target(calm, [1.0] * 80, timeframe="4h",
                               target_annual_vol=0.30, lookback=20)
    assert scaled2[-1] == 1.0


def test_rolling_window_report_segments():
    candles = _trend(540)  # 90 天
    reps = rolling_window_report(candles, [1.0] * len(candles),
                                 strategy="x", symbol="T", timeframe="4h",
                                 window_days=30)
    assert len(reps) == 3
    assert all(r.total_return_pct > 0 for r in reps)


# ── 横截面动量 ──────────────────────────────────────────────
def test_xs_momentum_longs_strongest_in_bull():
    strong = _trend(300, step=2.0)     # 强势币
    weak = _trend(300, step=0.1)       # 弱势币
    flat = [_c(i, 100, 100.5, 99.5, 100) for i in range(300)]
    series_map = {"S/USDT": strong, "W/USDT": weak, "F/USDT": flat}
    regime = {c.timestamp: "bull" for c in strong}
    cfg = XsMomentumConfig(lookback=30, rebalance=10, top_n=1)
    _, weights = weights_xs_momentum(series_map, regime, cfg)
    assert weights["S/USDT"][-1] == 1.0
    assert weights["W/USDT"][-1] == 0.0

    rep = backtest_xs_momentum(series_map, regime, cfg, timeframe="4h",
                               cost=CostModel(fee_pct=0.0, slippage_pct=0.0))
    assert rep.total_return_pct > 0


def test_xs_momentum_shorts_weakest_in_bear():
    up = _trend(300, step=0.5)
    down = _trend(300, start=500.0, step=-1.0)
    flat = [_c(i, 100, 100.5, 99.5, 100) for i in range(300)]
    series_map = {"U/USDT": up, "D/USDT": down, "F/USDT": flat}
    regime = {c.timestamp: "bear" for c in up}
    cfg = XsMomentumConfig(lookback=30, rebalance=10, top_n=1, short_size=0.5)
    _, weights = weights_xs_momentum(series_map, regime, cfg)
    assert weights["D/USDT"][-1] == -0.5   # 做空最弱
    assert weights["U/USDT"][-1] == 0.0


def test_xs_momentum_skips_unlisted_symbol():
    long_hist = _trend(300)
    late = _trend(40)  # 历史不足 lookback
    series_map = {"A/USDT": long_hist, "B/USDT": late}
    regime = {c.timestamp: "bull" for c in long_hist}
    cfg = XsMomentumConfig(lookback=100, rebalance=10, top_n=1)
    _, weights = weights_xs_momentum(series_map, regime, cfg)
    assert all(w == 0.0 for w in weights["B/USDT"])


# ── 资金费套利 ──────────────────────────────────────────────
def test_funding_carry_collects_positive_funding():
    t0 = int(T0.timestamp() * 1000)
    # 300 档持续 +0.01%
    funding = [(t0 + i * 8 * 3600 * 1000, 0.0001) for i in range(300)]
    rep = backtest_funding_carry("BTC/USDT", funding,
                                 cost=CostModel(fee_pct=0.0, slippage_pct=0.0))
    assert rep.total_return_pct > 2.5
    assert rep.max_drawdown_pct == 0.0
    assert rep.exposure > 0.9


def test_funding_carry_exits_on_negative_funding():
    t0 = int(T0.timestamp() * 1000)
    funding = [(t0 + i * 8 * 3600 * 1000, 0.0001) for i in range(100)]
    funding += [(t0 + (100 + i) * 8 * 3600 * 1000, -0.0002) for i in range(100)]
    rep = backtest_funding_carry("BTC/USDT", funding,
                                 cost=CostModel(fee_pct=0.0, slippage_pct=0.0))
    assert rep.round_trips >= 1
    # 负费率段应基本空仓：亏损远小于全程扛着（100 档 × -0.02% = -2%）
    assert rep.total_return_pct > 0
    st = current_carry_status(funding)
    assert st["signal"] == "flat"


# ── CVD 背离规则 ────────────────────────────────────────────
def test_cvd_divergence_fires_on_hollow_breakout():
    candles = [_c(i, 100, 100.6, 99.4, 100 + (0.2 if i % 2 else -0.2), 1000, 500)
               for i in range(60)]
    # 突破新高，但主动买占比极低（tbv=100/1000 → CVD 大幅下滑）
    candles.append(_c(60, 100, 103, 100, 102.8, 1000, 100))
    series = CandleSeries("T", "15m", candles)
    cfg = RuleConfig(
        enable_macd=False, enable_ema_stack=False, enable_boll=False,
        enable_volume=False, enable_structure_touch=False,
        enable_structure_flip=False, enable_fib_zone=False,
        enable_baseline=False, enable_break_level=False,
        cvd_lookback=40,
    )
    events, _ = evaluate_closed_bar_rules(series, {}, cfg)
    cvd_events = [e for e in events if e.rule == "cvd_divergence"]
    assert len(cvd_events) == 1
    assert cvd_events[0].direction == "short"


def test_cvd_rule_silent_without_taker_data():
    candles = [_c(i, 100, 100.6, 99.4, 100 + (0.2 if i % 2 else -0.2), 1000, 0.0)
               for i in range(60)]
    candles.append(_c(60, 100, 103, 100, 102.8, 1000, 0.0))
    series = CandleSeries("T", "15m", candles)
    cfg = RuleConfig(
        enable_macd=False, enable_ema_stack=False, enable_boll=False,
        enable_volume=False, enable_structure_touch=False,
        enable_structure_flip=False, enable_fib_zone=False,
        enable_baseline=False, enable_break_level=False,
    )
    events, _ = evaluate_closed_bar_rules(series, {}, cfg)
    assert not [e for e in events if e.rule == "cvd_divergence"]


# ── OI 背离规则 ─────────────────────────────────────────────
def test_oi_divergence_price_down_oi_up():
    events, st = evaluate_oi_rules(
        price=100.0, price_chg_pct_4h=-2.0, oi_chg_pct_4h=5.0,
        long_short_ratio=1.8, state={}, cfg=RuleConfig(),
    )
    assert len(events) == 1
    assert events[0].rule == "oi_divergence"
    assert events[0].direction == "short"
    # 同一 bucket 去抖
    events2, _ = evaluate_oi_rules(
        price=100.0, price_chg_pct_4h=-2.0, oi_chg_pct_4h=5.0,
        long_short_ratio=1.8, state=st, cfg=RuleConfig(),
    )
    assert not events2


def test_oi_divergence_healthy_move_silent():
    # 价涨 + OI 升 = 健康，不告警
    events, _ = evaluate_oi_rules(
        price=100.0, price_chg_pct_4h=2.0, oi_chg_pct_4h=5.0,
        state={}, cfg=RuleConfig(),
    )
    assert not events
