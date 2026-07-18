"""双线条件优势：用上下文更新胜率先验（简化贝叶斯）。

P(win) 不是固定 47%，而是在已知 ADX / 时段 / 形态强度后的后验。
系数刻意保守、少量特征，避免多重比较过拟合。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _session_tag(ts: datetime | None) -> str:
    """UTC 时段标签：asia / eu / us / off。"""
    if ts is None:
        return "off"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    # 周末欧美好流动性窗口弱一些
    weekend = ts.weekday() >= 5
    h = ts.hour + ts.minute / 60.0
    if 0.0 <= h < 3.0:
        return "asia"
    if (not weekend) and 7.0 <= h < 9.0:
        return "eu"
    if (not weekend) and 13.0 <= h < 15.0:
        return "us"
    return "off"


def _near_funding_utc(ts: datetime | None, window_min: int = 30) -> bool:
    """币安 U 本位常见结算：00/08/16 UTC 附近。"""
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    minutes = ts.hour * 60 + ts.minute
    for mark in (0, 8 * 60, 16 * 60):
        # 环形距离（跨日 00:00）
        d = abs(minutes - mark)
        d = min(d, 24 * 60 - d)
        if d <= window_min:
            return True
    return False


@dataclass(frozen=True)
class ConditionalEdge:
    """后验胜率与建议风险缩放。"""

    base_win_rate: float
    win_rate: float
    risk_scale: float
    session: str
    adx: float
    sudden: float
    overlap: float
    near_funding: bool
    deltas: tuple[tuple[str, float], ...]
    skip: bool
    skip_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_win_rate": round(self.base_win_rate, 4),
            "win_rate": round(self.win_rate, 4),
            "risk_scale": round(self.risk_scale, 4),
            "session": self.session,
            "adx": round(self.adx, 2),
            "sudden": round(self.sudden, 2),
            "overlap": round(self.overlap, 2),
            "near_funding": self.near_funding,
            "deltas": [{"name": n, "delta": d} for n, d in self.deltas],
            "skip": self.skip,
            "skip_reason": self.skip_reason,
        }


def estimate_conditional_edge(
    *,
    base_win_rate: float = 0.47,
    adx: float = 0.0,
    sudden: float = 0.0,
    overlap: float = 0.0,
    bar_ts: datetime | None = None,
    min_win_rate: float = 0.42,
    max_win_rate: float = 0.58,
) -> ConditionalEdge:
    """由上下文特征估计后验胜率，并映射到 risk_scale∈[0.35, 1.0]。"""
    p = float(base_win_rate)
    deltas: list[tuple[str, float]] = []

    # ADX：趋势强度
    if adx >= 30:
        deltas.append(("adx_strong", 0.04))
    elif adx >= 25:
        deltas.append(("adx_ok", 0.02))
    elif adx >= 20:
        deltas.append(("adx_bare", -0.02))
    else:
        deltas.append(("adx_weak", -0.05))

    # 时段：开盘窗口流动性更好；亚盘噪音略高；清淡时段略降
    session = _session_tag(bar_ts)
    if session == "us":
        deltas.append(("session_us", 0.025))
    elif session == "eu":
        deltas.append(("session_eu", 0.02))
    elif session == "asia":
        deltas.append(("session_asia", -0.025))
    else:
        deltas.append(("session_off", -0.02))

    # 资金费前：假突破/轧空轧多增多
    near_f = _near_funding_utc(bar_ts, 30)
    if near_f:
        deltas.append(("near_funding", -0.04))

    # 形态质量
    if sudden >= 2.8:
        deltas.append(("sudden_strong", 0.03))
    elif sudden < 2.2:
        deltas.append(("sudden_weak", -0.025))
    if overlap >= 0.70:
        deltas.append(("overlap_high", 0.02))
    elif overlap < 0.55:
        deltas.append(("overlap_low", -0.015))

    for _, d in deltas:
        p += d
    p = _clamp(p, 0.30, max_win_rate)

    skip = p < min_win_rate
    skip_reason = ""
    if skip:
        skip_reason = f"条件胜率 {p:.0%} < 门槛 {min_win_rate:.0%}"

    # 相对先验缩放仓位：后验越高仓越大，但封顶 1.0
    if base_win_rate > 0:
        raw = (p / base_win_rate) ** 1.2
    else:
        raw = 1.0
    risk_scale = 0.0 if skip else _clamp(raw, 0.35, 1.0)

    return ConditionalEdge(
        base_win_rate=base_win_rate,
        win_rate=p,
        risk_scale=risk_scale,
        session=session,
        adx=float(adx or 0.0),
        sudden=float(sudden or 0.0),
        overlap=float(overlap or 0.0),
        near_funding=near_f,
        deltas=tuple(deltas),
        skip=skip,
        skip_reason=skip_reason,
    )
