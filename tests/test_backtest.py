"""回测引擎测试：合成 K 线验证交易模拟与规则前瞻统计。"""

from datetime import datetime, timedelta

from analyst.backtest.engine import (
    _rule_forward_outcome,
    _simulate_trade,
    run_backtest,
)
from analyst.compute.strategies.double_line_reversal import DoubleLineConfig
from analyst.data.fetcher import Candle, CandleSeries


def _c(i: int, o: float, h: float, l: float, c: float, v: float = 1000) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1) + timedelta(minutes=15 * i),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
    )


def _flat_series(n: int = 80, base: float = 100.0) -> list[Candle]:
    return [_c(i, base, base + 0.3, base - 0.3, base) for i in range(n)]


# ── _simulate_trade ──


def test_simulate_trade_hits_tp():
    candles = _flat_series(10)
    # 第 6 根冲高触发 TP
    candles[6] = _c(6, 100, 106, 99.8, 105)
    tr = _simulate_trade(candles, 5, "long", entry=100.0, stop=98.0, tp=104.0, max_hold=10)
    assert tr.outcome == "tp"
    assert abs(tr.pnl_r - 2.0) < 1e-9
    assert tr.bars_held == 1


def test_simulate_trade_hits_sl_conservative_same_bar():
    candles = _flat_series(10)
    # 同一根同时打到 SL 与 TP → 保守按 SL
    candles[6] = _c(6, 100, 106, 97.0, 105)
    tr = _simulate_trade(candles, 5, "long", entry=100.0, stop=98.0, tp=104.0, max_hold=10)
    assert tr.outcome == "sl"
    assert tr.pnl_r == -1.0


def test_simulate_trade_timeout_closes_at_last_close():
    candles = _flat_series(20)
    tr = _simulate_trade(candles, 5, "long", entry=100.0, stop=98.0, tp=110.0, max_hold=5)
    assert tr.outcome == "timeout"
    # 横盘收盘 ≈100 → pnl ≈ 0
    assert abs(tr.pnl_r) < 0.2


def test_simulate_trade_short_direction():
    candles = _flat_series(10)
    candles[7] = _c(7, 100, 100.2, 94, 95)
    tr = _simulate_trade(candles, 5, "short", entry=100.0, stop=102.0, tp=96.0, max_hold=10)
    assert tr.outcome == "tp"
    assert abs(tr.pnl_r - 2.0) < 1e-9


# ── _rule_forward_outcome ──


def test_rule_forward_win_long():
    candles = _flat_series(20)
    candles[8] = _c(8, 100, 103, 99.9, 102.5)  # 上破 +1×ATR
    outcome, ret = _rule_forward_outcome(candles, 5, "long", horizon=10, atr=2.0)
    assert outcome == "win"


def test_rule_forward_loss_short():
    candles = _flat_series(20)
    candles[8] = _c(8, 100, 103, 99.9, 102.5)  # 对 short 是逆向
    outcome, ret = _rule_forward_outcome(candles, 5, "short", horizon=10, atr=2.0)
    assert outcome == "loss"


def test_rule_forward_flat_when_rangebound():
    candles = _flat_series(20)
    outcome, ret = _rule_forward_outcome(candles, 5, "long", horizon=10, atr=5.0)
    assert outcome == "flat"


# ── run_backtest（合成数据端到端） ──


def _series_with_double_line(n_pad: int = 70) -> CandleSeries:
    """横盘后插入 强阴+强阳 双线反转，随后上破并冲高。"""
    candles = _flat_series(n_pad)
    i = n_pad
    candles.append(_c(i, 100, 100.5, 96, 96.2))       # 大阴
    candles.append(_c(i + 1, 96.3, 100.6, 96.0, 100.55))  # 大阳，收上突破位
    # 随后一路上行触发 TP
    px = 100.6
    for j in range(2, 40):
        px *= 1.01
        candles.append(_c(i + j, px / 1.01, px * 1.002, px / 1.012, px))
    return CandleSeries(symbol="TEST/USDT", timeframe="15m", candles=candles)


def test_run_backtest_end_to_end_synthetic():
    s = _series_with_double_line()
    cfg = DoubleLineConfig(
        min_body_ratio=0.5,
        min_overlap_ratio=0.4,
        min_sudden_atr_mult=0.5,
        require_ema200=False,
    )
    report = run_backtest(
        "TEST/USDT",
        "15m",
        series=s,
        strategy_cfg=cfg,
        include_rules=True,
        rule_horizon=8,
        max_hold=50,
    )
    assert report.bars == len(s.candles)
    # 至少捕获一笔双线反转多单且盈利离场
    assert report.trades, "应触发至少一笔交易"
    first = report.trades[0]
    assert first.direction == "long"
    assert first.outcome == "tp"
    assert first.pnl_r > 0
    # 规则统计应有样本（上行趋势中 MACD/EMA 等会触发）
    assert isinstance(report.rule_stats, dict)
    # 汇总字段可计算
    assert 0.0 <= report.win_rate <= 1.0
    d = report.to_dict()
    assert d["strategy"]["trades"] >= 1


def test_run_backtest_insufficient_data():
    s = CandleSeries(symbol="TEST/USDT", timeframe="15m", candles=_flat_series(30))
    report = run_backtest("TEST/USDT", "15m", series=s)
    assert report.trades == []
    assert report.rule_stats == {}
