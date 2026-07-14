"""成交量结构分析。

判定要素：
- 放量 / 缩量（vs 20 周期均量）
- 量价配合 / 量价背离
- OBV 趋势（On-Balance Volume）

Jack Li 风格的量能解读关注：
- 突破时是否放量（无量突破 = 假突破）
- 高位放量是否出货
- 下跌缩量企稳信号
"""

from __future__ import annotations

from dataclasses import dataclass

from analyst.data.fetcher import CandleSeries


@dataclass
class VolumeAnalysis:
    """成交量分析结果。"""

    recent_volume: float        # 最近一根 K 线的成交量
    avg_volume_20: float        # 20 周期均量
    volume_ratio: float         # 放量倍数（recent / avg_20）
    obv: float                  # 当前 OBV
    obv_trend: str              # "rising" / "falling" / "flat"
    price_volume_signal: str    # 量价配合状态


def analyze_volume(series: CandleSeries) -> VolumeAnalysis:
    """对一个周期的成交量做综合分析。"""
    candles = series.candles
    n = len(candles)
    if n < 21:
        return VolumeAnalysis(
            recent_volume=candles[-1].volume if candles else 0,
            avg_volume_20=0,
            volume_ratio=1.0,
            obv=0,
            obv_trend="flat",
            price_volume_signal="数据不足",
        )

    volumes = [c.volume for c in candles]
    closes = [c.close for c in candles]

    recent = volumes[-1]
    avg_20 = sum(volumes[-21:-1]) / 20
    ratio = recent / avg_20 if avg_20 > 0 else 1.0

    # ─── OBV ───
    obv_values = [0.0]
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            obv_values.append(obv_values[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv_values.append(obv_values[-1] - volumes[i])
        else:
            obv_values.append(obv_values[-1])

    obv_now = obv_values[-1]
    # 用最近 10 根 OBV 的斜率判断趋势
    if n >= 10:
        obv_recent = obv_values[-10:]
        slope = obv_recent[-1] - obv_recent[0]
        scale = max(abs(v) for v in obv_recent) or 1
        slope_pct = slope / scale
        if slope_pct > 0.05:
            obv_trend = "rising"
        elif slope_pct < -0.05:
            obv_trend = "falling"
        else:
            obv_trend = "flat"
    else:
        obv_trend = "flat"

    # ─── 量价配合 ───
    # 看最近 3 根：价格方向 + 量能方向
    if n >= 3:
        price_change = closes[-1] - closes[-3]
        vol_change = volumes[-1] - volumes[-3]
        if price_change > 0 and vol_change > 0:
            signal = "量价齐升（健康上涨）"
        elif price_change > 0 and vol_change < 0:
            signal = "价升量缩 ⚠️ 顶背离风险"
        elif price_change < 0 and vol_change > 0:
            signal = "价跌量增（恐慌抛售）"
        elif price_change < 0 and vol_change < 0:
            signal = "缩量回调（弱势调整）"
        else:
            signal = "量价中性"
    else:
        signal = "量价中性"

    # 异常放量优先级最高
    if ratio >= 2.5:
        signal = f"放量 {ratio:.1f}× · {signal}"
    elif ratio >= 1.5:
        signal = f"温和放量 {ratio:.1f}× · {signal}"
    elif ratio < 0.5:
        signal = f"明显缩量 {ratio:.1f}× · {signal}"

    return VolumeAnalysis(
        recent_volume=recent,
        avg_volume_20=avg_20,
        volume_ratio=ratio,
        obv=obv_now,
        obv_trend=obv_trend,
        price_volume_signal=signal,
    )
