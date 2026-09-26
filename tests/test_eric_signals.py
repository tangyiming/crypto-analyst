"""CycleStudies / Eric 风格规则测试。"""

from datetime import datetime, timedelta

from analyst.compute.eric_signals import evaluate_eric, rsi_series
from analyst.data.fetcher import Candle, CandleSeries
from analyst.monitor.rules import RuleConfig, evaluate_closed_bar_rules, is_ai_candidate


def _c(i: int, close: float) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1) + timedelta(hours=i),
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=1000,
    )


def _series(closes: list[float]) -> CandleSeries:
    return CandleSeries(
        symbol="BTC/USDT",
        timeframe="1d",
        candles=[_c(i, c) for i, c in enumerate(closes)],
    )


def _closes_entering_oversold() -> list[float]:
    """构造最后一根才让波段过滤器（滞回状态机）进入超卖的收盘序列。"""
    from analyst.compute.band_filter import compute_band_filter

    closes = [100.0]
    for _ in range(60):
        closes.append(closes[-1] + 0.8)
    for _ in range(80):
        closes.append(closes[-1] - 1.5)
        bf = compute_band_filter(_series(closes))
        if bf is not None and bf.entered_oversold:
            return closes
    raise AssertionError("未能构造出进入超卖的序列")


def test_band_filter_enters_oversold_once():
    from analyst.compute.band_filter import compute_band_filter

    closes = _closes_entering_oversold()
    bf = compute_band_filter(_series(closes))
    assert bf is not None and bf.entered_oversold and bf.state == "oversold"
    assert bf.value <= -40
    # 事件只报一次：再跌一根仍在状态内，但不再是「刚进入」
    bf2 = compute_band_filter(_series(closes + [closes[-1] - 1.5]))
    assert bf2 is not None and bf2.state == "oversold" and not bf2.entered_oversold
    sigs = evaluate_eric(_series(closes), atr=3.0)
    kinds = {s.kind for s in sigs}
    assert "oversold" in kinds
    # RSI 不再兜底：RSI 单独跌破 30 但过滤器未进入时不发 oversold
    r = rsi_series(closes, 14)
    assert r[-1] < 50  # 序列确实在下跌
    sigs2 = evaluate_eric(_series(closes), oversold=30, overbought=70, atr=3.0, min_buff=1)
    assert "rebound_long" in {s.kind for s in sigs2} or "oversold" in {s.kind for s in sigs2}


def _eric_only_cfg(**kwargs) -> RuleConfig:
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
        enable_market_regime=False,
        enable_eric=True,
        **kwargs,
    )


def test_eric_rule_fires_on_closed_bar():
    closes = _closes_entering_oversold()
    events, _ = evaluate_closed_bar_rules(
        _series(closes), {}, _eric_only_cfg(eric_cooldown_bars=1)
    )
    eric = [e for e in events if e.rule.startswith("eric_")]
    assert eric, f"应触发 Eric 规则，实际 events={[e.rule for e in events]}"
    assert any(e.direction == "long" for e in eric)
    assert is_ai_candidate([e.rule for e in eric])


def test_eric_cooldown_suppresses_repeat():
    closes = _closes_entering_oversold()
    cfg = _eric_only_cfg(eric_cooldown_bars=20)
    events1, state = evaluate_closed_bar_rules(_series(closes), {}, cfg)
    assert [e for e in events1 if e.rule.startswith("eric_")]
    events2, _ = evaluate_closed_bar_rules(_series(closes), state, cfg)
    assert not [e for e in events2 if e.rule.startswith("eric_")]


def test_eric_rules_only_for_btc_eth_daily_weekly():
    closes = _closes_entering_oversold()
    cfg = _eric_only_cfg(eric_cooldown_bars=1)
    base = _series(closes).candles
    ok_d, _ = evaluate_closed_bar_rules(CandleSeries("BTC/USDT", "1d", base), {}, cfg)
    ok_w, _ = evaluate_closed_bar_rules(CandleSeries("ETH/USDT", "1w", base), {}, cfg)
    assert [e for e in ok_d if e.rule.startswith("eric_")]
    assert [e for e in ok_w if e.rule.startswith("eric_")]
    for sym, tf in (("SOL/USDT", "1d"), ("BNB/USDT", "1w"), ("BTC/USDT", "4h"), ("BTC/USDT", "1h"), ("ETH/USDT", "15m")):
        ev, _ = evaluate_closed_bar_rules(CandleSeries(sym, tf, base), {}, cfg)
        assert not [e for e in ev if e.rule.startswith("eric_")], (sym, tf)
