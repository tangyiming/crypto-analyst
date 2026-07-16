"""实时关键位语境：支撑/阻力距离、回调区与试空区判定。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analyst.compute.fibonacci import FibLevels, compute_fib, find_long_zone, find_short_zone
from analyst.compute.structure import Structure, detect_structure
from analyst.data.fetcher import CandleSeries

# 距关键位多少百分比内视为「贴近」
NEAR_LEVEL_PCT = 0.35

TF_PRIORITY: dict[str, int] = {
    "1d": 5,
    "4h": 4,
    "1h": 3,
    "30m": 2,
    "15m": 1,
}


def tf_priority(timeframe: str) -> int:
    return TF_PRIORITY.get((timeframe or "").strip().lower(), 0)


def distance_pct(price: float, level: float) -> float:
    if not level:
        return 0.0
    return (price - level) / level * 100.0


def _zone_bounds(lo: float, hi: float) -> tuple[float, float]:
    return (min(lo, hi), max(lo, hi))


def in_zone(price: float, lo: float, hi: float) -> bool:
    a, b = _zone_bounds(lo, hi)
    return a <= price <= b


@dataclass
class LevelSnapshot:
    structure: Structure
    fib: FibLevels
    timeframe: str


def snapshot_from_series(series: CandleSeries) -> LevelSnapshot | None:
    if len(series.candles) < 30:
        return None
    structure = detect_structure(series)
    fib = compute_fib(structure.recent_high, structure.recent_low)
    return LevelSnapshot(structure=structure, fib=fib, timeframe=series.timeframe)


def compute_level_context(price: float, snapshot: LevelSnapshot) -> dict[str, Any]:
    """由结构快照 + 实时价生成关键位面板数据。"""
    st = snapshot.structure
    fib = snapshot.fib

    def row(kind: str, level: float) -> dict[str, Any]:
        dist = distance_pct(price, level)
        return {
            "kind": kind,
            "price": level,
            "dist_pct": round(dist, 3),
            "near": abs(dist) <= NEAR_LEVEL_PCT,
        }

    supports = [row("support", lvl) for lvl in st.supports[:3]]
    resistances = [row("resistance", lvl) for lvl in st.resistances[:3]]

    long_lo, long_hi = find_long_zone(fib)
    short_lo, short_hi = find_short_zone(fib)
    long_a, long_b = _zone_bounds(long_lo, long_hi)
    short_a, short_b = _zone_bounds(short_lo, short_hi)

    in_fib_long = in_zone(price, long_a, long_b)
    in_fib_short = in_zone(price, short_a, short_b)
    near_support = any(s["near"] for s in supports)
    near_resistance = any(r["near"] for r in resistances)

    dip_buy = False
    try_short = False
    zone_reasons: list[str] = []

    if st.trend in ("up", "range"):
        if in_fib_long:
            dip_buy = True
            zone_reasons.append(f"Fib 回踩区 {long_a:.4g}–{long_b:.4g}")
        if near_support and supports:
            dip_buy = True
            zone_reasons.append(f"近支撑 {supports[0]['price']:.4g}")

    if st.trend in ("down", "range"):
        if in_fib_short:
            try_short = True
            zone_reasons.append(f"Fib 反弹区 {short_a:.4g}–{short_b:.4g}")
        if near_resistance and resistances:
            try_short = True
            zone_reasons.append(f"近阻力 {resistances[0]['price']:.4g}")

    if dip_buy and try_short:
        zone_label = "震荡分向"
    elif dip_buy:
        zone_label = "可低吸"
    elif try_short:
        zone_label = "可试空"
    elif st.trend == "up":
        zone_label = "等回踩"
    elif st.trend == "down":
        zone_label = "等反弹"
    else:
        zone_label = "观望"

    return {
        "price": price,
        "timeframe": snapshot.timeframe,
        "trend": st.trend,
        "key_pivot": st.key_pivot,
        "supports": supports,
        "resistances": resistances,
        "fib_zones": {
            "long": {"low": long_a, "high": long_b, "inside": in_fib_long},
            "short": {"low": short_a, "high": short_b, "inside": in_fib_short},
        },
        "zone": {
            "dip_buy": dip_buy,
            "try_short": try_short,
            "label": zone_label,
            "reasons": zone_reasons[:3],
        },
    }
