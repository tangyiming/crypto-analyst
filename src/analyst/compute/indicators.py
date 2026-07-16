"""技术指标计算 - MACD / EMA / BOLL。

为最小依赖，自己实现核心指标（不依赖 pandas-ta）。
数学已通过 tests/test_indicators.py 验证。
"""

from dataclasses import dataclass, field

from analyst.data.fetcher import CandleSeries


def ema(values: list[float], period: int) -> list[float]:
    """递推计算 EMA。返回每根 K线对应的 EMA 序列。"""
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(alpha * v + (1 - alpha) * result[-1])
    return result


def sma(values: list[float], period: int) -> float:
    """简单移动平均（最后 period 根的均值）。"""
    if len(values) < period:
        return sum(values) / len(values) if values else 0.0
    return sum(values[-period:]) / period


def stddev(values: list[float], period: int) -> float:
    """最近 period 根的总体标准差。"""
    if len(values) < period:
        return 0.0
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((v - mean) ** 2 for v in window) / period
    return variance**0.5


@dataclass
class MACDResult:
    dif: float
    dea: float
    histogram: float
    above_zero: bool
    cross_signal: str | None       # 'golden' / 'death' / None
    series_dif: list[float] = field(default_factory=list, repr=False)
    series_dea: list[float] = field(default_factory=list, repr=False)


@dataclass
class EMAResult:
    ema7: float
    ema30: float
    ema52: float


@dataclass
class BOLLResult:
    upper: float
    middle: float
    lower: float
    width: float


@dataclass
class IndicatorSnapshot:
    timeframe: str
    macd: MACDResult
    ema: EMAResult
    boll: BOLLResult


def compute_macd(
    series: CandleSeries,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MACDResult:
    """MACD(12, 26, 9)"""
    closes = series.closes
    if len(closes) < slow + signal:
        c = closes[-1] if closes else 0.0
        return MACDResult(
            dif=0.0, dea=0.0, histogram=0.0,
            above_zero=False, cross_signal=None,
        )

    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif_series = [f - s for f, s in zip(ema_fast, ema_slow, strict=True)]
    dea_series = ema(dif_series, signal)

    dif = dif_series[-1]
    dea = dea_series[-1]
    hist = (dif - dea) * 2

    cross_signal: str | None = None
    if len(dif_series) >= 2:
        prev_dif, prev_dea = dif_series[-2], dea_series[-2]
        if prev_dif < prev_dea and dif > dea:
            cross_signal = "golden"
        elif prev_dif > prev_dea and dif < dea:
            cross_signal = "death"

    return MACDResult(
        dif=dif,
        dea=dea,
        histogram=hist,
        above_zero=dif > 0,
        cross_signal=cross_signal,
        series_dif=dif_series,
        series_dea=dea_series,
    )


def compute_ema(series: CandleSeries) -> EMAResult:
    """EMA 7/30/52"""
    closes = series.closes
    if not closes:
        return EMAResult(ema7=0, ema30=0, ema52=0)
    return EMAResult(
        ema7=ema(closes, 7)[-1] if len(closes) >= 7 else closes[-1],
        ema30=ema(closes, 30)[-1] if len(closes) >= 30 else closes[-1],
        ema52=ema(closes, 52)[-1] if len(closes) >= 52 else closes[-1],
    )


def _wilder_smooth(values: list[float], period: int) -> list[float]:
    """Wilder RMA：首值=SMA，其后 prev - prev/period + x。"""
    if len(values) < period:
        return []
    out: list[float] = []
    seed = sum(values[:period]) / period
    out.append(seed)
    prev = seed
    for x in values[period:]:
        prev = prev - (prev / period) + x
        out.append(prev)
    return out


def compute_adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float:
    """返回最新 ADX（0–100）。数据不足返回 0。"""
    n = min(len(highs), len(lows), len(closes))
    if period < 1 or n < period * 2 + 1:
        return 0.0
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up = h - highs[i - 1]
        down = lows[i - 1] - l
        trs.append(tr)
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    atr_s = _wilder_smooth(trs, period)
    p_s = _wilder_smooth(plus_dm, period)
    m_s = _wilder_smooth(minus_dm, period)
    if not atr_s or len(atr_s) != len(p_s) or len(atr_s) != len(m_s):
        return 0.0

    dx_vals: list[float] = []
    for atr_v, pv, mv in zip(atr_s, p_s, m_s, strict=True):
        if atr_v <= 0:
            dx_vals.append(0.0)
            continue
        pdi = 100.0 * pv / atr_v
        mdi = 100.0 * mv / atr_v
        denom = pdi + mdi
        dx_vals.append(0.0 if denom <= 0 else 100.0 * abs(pdi - mdi) / denom)

    adx_s = _wilder_smooth(dx_vals, period)
    return float(adx_s[-1]) if adx_s else 0.0


def compute_boll(
    series: CandleSeries,
    period: int = 20,
    k: float = 2.0,
) -> BOLLResult:
    """布林带 BOLL(20, 2)"""
    closes = series.closes
    if len(closes) < period:
        c = closes[-1] if closes else 0.0
        return BOLLResult(upper=c, middle=c, lower=c, width=0.0)

    middle = sma(closes, period)
    std = stddev(closes, period)
    upper = middle + k * std
    lower = middle - k * std
    return BOLLResult(upper=upper, middle=middle, lower=lower, width=upper - lower)


def compute_all(series: CandleSeries) -> IndicatorSnapshot:
    """一次性算齐所有指标。"""
    return IndicatorSnapshot(
        timeframe=series.timeframe,
        macd=compute_macd(series),
        ema=compute_ema(series),
        boll=compute_boll(series),
    )
