"""盯盘侧预计算 Jack 锁点 / 三盘（无 LLM）。

优先用 4h 结构定调，日线/1h/5m 只补 playbook 所需序列。
"""

from __future__ import annotations

from analyst.compute.fibonacci import compute_fib
from analyst.compute.indicators import compute_all
from analyst.compute.jack_levels import JackLevels, compute_jack_levels
from analyst.compute.jack_regime import JackRegime, compute_jack_regime
from analyst.compute.structure import detect_structure
from analyst.data.fetcher import CandleSeries


def indicators_dict(series: CandleSeries | None) -> dict | None:
    if not series or len(series.candles) < 26:
        return None
    snap = compute_all(series)
    return {
        "macd": {
            "histogram": snap.macd.histogram,
            "above_zero": snap.macd.above_zero,
            "cross_signal": snap.macd.cross_signal,
        },
        "ema": {
            "ema7": snap.ema.ema7,
            "ema30": snap.ema.ema30,
            "ema52": snap.ema.ema52,
        },
        "boll": {
            "upper": snap.boll.upper,
            "middle": snap.boll.middle,
            "lower": snap.boll.lower,
            "width": snap.boll.width,
        },
    }


def _structure_series(
    worker_series: CandleSeries,
    hourly_series: CandleSeries | None,
    h4_series: CandleSeries | None,
) -> CandleSeries:
    for series in (h4_series, hourly_series, worker_series):
        if series and len(series.candles) >= 40:
            return series
    return worker_series


def compute_monitor_jack(
    *,
    symbol: str,
    current_price: float,
    worker_series: CandleSeries,
    daily_series: CandleSeries | None = None,
    hourly_series: CandleSeries | None = None,
    h4_series: CandleSeries | None = None,
    m5_series: CandleSeries | None = None,
    btc_series: CandleSeries | None = None,
    high_24h: float | None = None,
    low_24h: float | None = None,
) -> tuple[JackLevels, JackRegime]:
    """用盯盘内存序列 + 日线补丁算出锁点与三盘。"""
    structure_src = _structure_series(worker_series, hourly_series, h4_series)
    structure = detect_structure(structure_src)
    fib = compute_fib(structure.recent_high, structure.recent_low)
    jack = compute_jack_levels(
        current_price=current_price,
        structure=structure,
        fib=fib,
        daily_indicators=indicators_dict(daily_series),
        primary_series=worker_series,
        btc_series=btc_series,
        symbol=symbol,
    )
    regime = compute_jack_regime(
        current_price=current_price,
        jack=jack,
        structure=structure,
        primary_series=worker_series,
        daily_series=daily_series,
        hourly_series=hourly_series,
        h4_series=h4_series,
        m5_series=m5_series,
        high_24h=high_24h,
        low_24h=low_24h,
    )
    return jack, regime
