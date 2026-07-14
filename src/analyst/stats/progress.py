"""胜率与成长统计。"""

from dataclasses import dataclass, field
from datetime import datetime

from analyst.storage import repo


@dataclass
class ProgressReport:
    period_days: int
    total_sessions: int
    completed: int                          # 实际触发的会话数
    win_rate: float                         # 触发且盈利的比例
    avg_rr: float                           # 计划的平均 R:R
    avg_user_pnl_r: float                   # 实际平均 R 倍数

    vs_ai_agreement_rate: float             # 方向一致率
    vs_ai_outperform_rate: float            # 跑赢 AI 比例
    vs_ai_pnl_diff: float                   # 平均 R 倍数差距

    vs_optimal_capture_rate: float          # 最优捕获率（你/最优）

    weekly_win_rates: list[float] = field(default_factory=list)


def calculate(period_days: int = 30) -> ProgressReport:
    """计算用户在指定周期内的进步指标。"""
    sessions = repo.list_verified_sessions(period_days)

    if not sessions:
        return _empty_report(period_days)

    # 只统计有用户计划的会话（排除 quick 模式）
    with_user = [t for t in sessions if t[1] is not None]
    if not with_user:
        return _empty_report(period_days)

    total = len(with_user)

    # 胜率（按已触发计算）
    triggered = sum(
        1 for s, u, a, v in with_user
        if v.user_outcome != "no_trigger"
    )
    wins = sum(
        1 for s, u, a, v in with_user
        if v.user_pnl_r > 0 and v.user_outcome != "no_trigger"
    )
    win_rate = wins / triggered if triggered > 0 else 0.0

    avg_user_pnl_r = sum(v.user_pnl_r for s, u, a, v in with_user) / total
    avg_rr = sum(u.rr_ratio for s, u, a, v in with_user) / total

    agreement = sum(
        1 for s, u, a, v in with_user
        if u.direction == a.direction
    )
    vs_ai_agreement_rate = agreement / total

    outperform = sum(
        1 for s, u, a, v in with_user
        if v.user_pnl_r > v.ai_pnl_r
    )
    vs_ai_outperform_rate = outperform / total

    vs_ai_pnl_diff = (
        sum(v.user_pnl_r - v.ai_pnl_r for s, u, a, v in with_user) / total
    )

    capture_ratios: list[float] = []
    for s, u, a, v in with_user:
        if v.optimal_pnl_r > 0:
            capture_ratios.append(v.user_pnl_r / v.optimal_pnl_r)
    vs_optimal_capture_rate = (
        sum(capture_ratios) / len(capture_ratios) if capture_ratios else 0.0
    )

    weekly_win_rates = _calculate_weekly_win_rates(with_user, period_days)

    return ProgressReport(
        period_days=period_days,
        total_sessions=total,
        completed=triggered,
        win_rate=win_rate,
        avg_rr=avg_rr,
        avg_user_pnl_r=avg_user_pnl_r,
        vs_ai_agreement_rate=vs_ai_agreement_rate,
        vs_ai_outperform_rate=vs_ai_outperform_rate,
        vs_ai_pnl_diff=vs_ai_pnl_diff,
        vs_optimal_capture_rate=vs_optimal_capture_rate,
        weekly_win_rates=weekly_win_rates,
    )


def _empty_report(period_days: int) -> ProgressReport:
    return ProgressReport(
        period_days=period_days,
        total_sessions=0,
        completed=0,
        win_rate=0.0,
        avg_rr=0.0,
        avg_user_pnl_r=0.0,
        vs_ai_agreement_rate=0.0,
        vs_ai_outperform_rate=0.0,
        vs_ai_pnl_diff=0.0,
        vs_optimal_capture_rate=0.0,
        weekly_win_rates=[],
    )


def _calculate_weekly_win_rates(
    sessions: list,
    period_days: int,
) -> list[float]:
    """按周分组算胜率（只算已触发）。"""
    if not sessions:
        return []

    weeks = max(1, period_days // 7)
    now = datetime.utcnow()
    weekly_results: list[list[bool]] = [[] for _ in range(weeks)]

    for s, u, a, v in sessions:
        days_ago = (now - s.created_at).days
        week_idx = min(days_ago // 7, weeks - 1)
        if v.user_outcome != "no_trigger":
            weekly_results[week_idx].append(v.user_pnl_r > 0)

    # 旧 → 新
    return [
        sum(w) / len(w) if w else 0.0
        for w in reversed(weekly_results)
    ]
