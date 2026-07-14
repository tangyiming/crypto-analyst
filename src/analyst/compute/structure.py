"""结构识别 - 支撑/阻力、趋势判定。

简化算法（够用 + 透明）：
- Fractal 识别局部高低点（5-bar fractal）
- 价格聚类（threshold_pct 内的归一类）
- 用 EMA52 斜率与位置判趋势
"""

from dataclasses import dataclass

from analyst.compute.indicators import compute_ema
from analyst.data.fetcher import CandleSeries


@dataclass
class Structure:
    """市场结构识别结果。"""
    trend: str                       # 'up' / 'down' / 'range'
    supports: list[float]            # 主要支撑（近到远，至多 3 个）
    resistances: list[float]         # 主要阻力（近到远，至多 3 个）
    key_pivot: float                 # 多空分界（用 EMA52）
    recent_high: float               # 用于斐波计算
    recent_low: float


def find_pivots(
    highs: list[float],
    lows: list[float],
    window: int = 3,
) -> tuple[list[int], list[int]]:
    """5-bar fractal: 中心 K线高/低于左右各 window 根。"""
    n = len(highs)
    pivot_highs: list[int] = []
    pivot_lows: list[int] = []

    for i in range(window, n - window):
        if highs[i] == max(highs[i - window:i + window + 1]):
            pivot_highs.append(i)
        if lows[i] == min(lows[i - window:i + window + 1]):
            pivot_lows.append(i)

    return pivot_highs, pivot_lows


def cluster_levels(
    prices: list[float],
    threshold_pct: float = 0.005,
) -> list[float]:
    """聚类相近价位，避免太多碎点。

    threshold_pct: 距离簇心多少百分比内的价位归一类。
    """
    if not prices:
        return []

    sorted_prices = sorted(prices)
    clusters: list[list[float]] = [[sorted_prices[0]]]

    for p in sorted_prices[1:]:
        last_avg = sum(clusters[-1]) / len(clusters[-1])
        if last_avg > 0 and abs(p - last_avg) / last_avg < threshold_pct:
            clusters[-1].append(p)
        else:
            clusters.append([p])

    return [sum(c) / len(c) for c in clusters]


def detect_structure(
    series: CandleSeries,
    lookback: int = 100,
) -> Structure:
    """识别当前市场结构。"""
    candles = series.candles[-lookback:] if len(series.candles) > lookback else series.candles
    if not candles:
        return Structure(
            trend="range",
            supports=[],
            resistances=[],
            key_pivot=0.0,
            recent_high=0.0,
            recent_low=0.0,
        )

    highs = [c.high for c in candles]
    lows = [c.low for c in candles]

    pivot_high_idx, pivot_low_idx = find_pivots(highs, lows)
    pivot_highs = [highs[i] for i in pivot_high_idx]
    pivot_lows = [lows[i] for i in pivot_low_idx]

    resistance_levels = cluster_levels(pivot_highs)
    support_levels = cluster_levels(pivot_lows)

    current = candles[-1].close

    # 阻力 = 高于当前的关键位（升序，近到远）
    resistances = sorted([p for p in resistance_levels if p > current])[:3]
    # 支撑 = 低于当前的关键位（降序，近到远）
    supports = sorted([p for p in support_levels if p < current], reverse=True)[:3]

    # 趋势判定：用 EMA52 与当前价的相对位置 + 1% 缓冲带
    ema_result = compute_ema(series)
    if current > ema_result.ema52 * 1.01:
        trend = "up"
    elif current < ema_result.ema52 * 0.99:
        trend = "down"
    else:
        trend = "range"

    return Structure(
        trend=trend,
        supports=supports,
        resistances=resistances,
        key_pivot=ema_result.ema52,
        recent_high=max(highs),
        recent_low=min(lows),
    )
