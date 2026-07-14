"""弱点识别 - 找出你最容易犯的错。

通过对所有 verified 会话的事后分析，归纳出模式。
"""

from collections import Counter
from dataclasses import dataclass

from analyst.storage import repo


@dataclass
class WeaknessPattern:
    name: str
    occurrences: int
    pct: float
    description: str
    suggestion: str


PATTERN_INFO: dict[str, tuple[str, str]] = {
    "direction_wrong": (
        "方向判断错误",
        "检查多周期判定流程：先大周期定调，再小周期找入场",
    ),
    "low_rr": (
        "盈亏比不足 2:1",
        "强制 R:R ≥ 2 才入场，宁可错过不可做错",
    ),
    "no_trigger": (
        "入场区设得太死",
        "把入场区放宽到 0.5-0.618 fib 之间，不要追单点",
    ),
    "underperform_ai": (
        "方向对但收益不如 AI",
        "止损止盈可能过保守，或入场点不准",
    ),
    "stop_too_tight": (
        "止损过紧被扫",
        "至少用 1.5*ATR 作止损距离，避免噪音扫损",
    ),
    "trade_in_range": (
        "震荡市强行交易",
        "无明确趋势时观望，等结构清晰",
    ),
}


def detect_weaknesses(top_n: int = 5) -> list[WeaknessPattern]:
    """找出用户最常见的弱点。"""
    sessions = repo.list_verified_sessions(period_days=90)
    if not sessions:
        return []

    with_user = [t for t in sessions if t[1] is not None]
    if len(with_user) < 5:
        return []

    counter: Counter[str] = Counter()

    for s, u, a, v in with_user:
        if u.direction != "wait" and v.user_outcome == "loss":
            counter["direction_wrong"] += 1

        if u.direction != "wait" and u.rr_ratio < 2.0:
            counter["low_rr"] += 1

        if u.direction != "wait" and v.user_outcome == "no_trigger":
            counter["no_trigger"] += 1

        if (
            u.direction == a.direction
            and u.direction != "wait"
            and v.user_pnl_r < v.ai_pnl_r - 0.3
        ):
            counter["underperform_ai"] += 1

        if (
            u.direction == a.direction
            and u.direction != "wait"
            and v.user_pnl_r < 0
            and v.ai_pnl_r > 0
        ):
            counter["stop_too_tight"] += 1

    total = len(with_user)
    patterns: list[WeaknessPattern] = []

    for name, count in counter.most_common(top_n):
        if name not in PATTERN_INFO:
            continue
        desc, sugg = PATTERN_INFO[name]
        patterns.append(
            WeaknessPattern(
                name=name,
                occurrences=count,
                pct=count / total * 100,
                description=desc,
                suggestion=sugg,
            )
        )

    return patterns


KNOWN_PATTERNS = list(PATTERN_INFO.keys())
