"""全市场 AI 验证表现（不依赖用户是否提交计划）。"""

from dataclasses import asdict, dataclass


@dataclass
class AIBenchmarkReport:
    period_days: int
    verified_count: int
    ai_triggered: int
    ai_win_rate: float
    avg_ai_pnl_r: float
    sessions_with_user_plan: int


def calculate_ai_benchmark(period_days: int = 30) -> AIBenchmarkReport:
    from analyst.storage import repo

    sessions = repo.list_verified_sessions(period_days)
    if not sessions:
        return AIBenchmarkReport(
            period_days=period_days,
            verified_count=0,
            ai_triggered=0,
            ai_win_rate=0.0,
            avg_ai_pnl_r=0.0,
            sessions_with_user_plan=0,
        )

    total = len(sessions)
    with_user = sum(1 for s, u, a, v in sessions if u is not None)
    triggered = sum(1 for s, u, a, v in sessions if v.ai_outcome != "no_trigger")
    wins = sum(
        1
        for s, u, a, v in sessions
        if v.ai_pnl_r > 0 and v.ai_outcome != "no_trigger"
    )
    return AIBenchmarkReport(
        period_days=period_days,
        verified_count=total,
        ai_triggered=triggered,
        ai_win_rate=wins / triggered if triggered else 0.0,
        avg_ai_pnl_r=sum(v.ai_pnl_r for s, u, a, v in sessions) / total,
        sessions_with_user_plan=with_user,
    )


def benchmark_to_dict(r: AIBenchmarkReport) -> dict:
    return asdict(r)
