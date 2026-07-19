"""回测引擎测试：规则前瞻统计。"""

from datetime import datetime, timedelta

from analyst.backtest.engine import (
    _rule_forward_outcome,
    run_backtest,
)
from analyst.data.fetcher import Candle, CandleSeries
from analyst.monitor.rules import RuleConfig


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


def _series_with_uptrend(n_pad: int = 70) -> CandleSeries:
    candles = _flat_series(n_pad)
    px = 100.0
    for j in range(40):
        px *= 1.01
        candles.append(_c(n_pad + j, px / 1.01, px * 1.002, px / 1.012, px))
    return CandleSeries(symbol="TEST/USDT", timeframe="15m", candles=candles)


def test_run_backtest_rules_only():
    s = _series_with_uptrend()
    report = run_backtest(
        "TEST/USDT",
        "15m",
        series=s,
        include_rules=True,
        rule_horizon=8,
        rule_cfg=RuleConfig(enable_baseline=True),
    )
    assert report.bars == len(s.candles)
    assert isinstance(report.rule_stats, dict)
    d = report.to_dict()
    assert "rules" in d
    assert "strategy" not in d


def test_run_backtest_insufficient_data():
    s = CandleSeries(symbol="TEST/USDT", timeframe="15m", candles=_flat_series(30))
    report = run_backtest("TEST/USDT", "15m", series=s)
    assert report.rule_stats == {}
