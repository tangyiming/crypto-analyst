"""Rich 报告渲染。"""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from analyst.stats.progress import ProgressReport
from analyst.stats.weakness import WeaknessPattern


def render_progress(report: ProgressReport, console: Console | None = None) -> None:
    """用 Rich 表格 + 面板渲染进度报告。"""
    if console is None:
        console = Console()

    if report.total_sessions == 0:
        console.print(
            Panel(
                "暂无已验证会话。\n"
                "请先用 [cyan]analyst practice <symbol>[/cyan] 创建会话，"
                "并在到期后用 [cyan]analyst verify[/cyan] 验证。",
                title="📈 分析进度",
                border_style="dim",
            )
        )
        return

    table = Table(
        title=f"📈 最近 {report.period_days} 天分析摘要",
        show_header=False,
        show_lines=True,
    )
    table.add_column("指标", style="cyan", no_wrap=True)
    table.add_column("数值", style="bold")

    table.add_row("总会话数", f"{report.total_sessions}")
    table.add_row("实际触发", f"{report.completed}")
    table.add_row("胜率", _fmt_pct(report.win_rate))
    table.add_row("平均 R 倍数", _fmt_r(report.avg_user_pnl_r))
    table.add_row("计划 R:R", f"{report.avg_rr:.2f}")
    table.add_row("与 AI 一致率", _fmt_pct(report.vs_ai_agreement_rate))
    table.add_row("跑赢 AI 比例", _fmt_pct(report.vs_ai_outperform_rate))
    table.add_row("vs AI 收益差", _fmt_r(report.vs_ai_pnl_diff))
    table.add_row("最优捕获率", _fmt_pct(report.vs_optimal_capture_rate))

    console.print(table)

    if report.weekly_win_rates:
        weekly = " → ".join(f"{w:.0%}" for w in report.weekly_win_rates)
        console.print(
            Panel(weekly, title="📊 周度胜率（旧 → 新）", border_style="blue")
        )


def render_weakness(
    patterns: list[WeaknessPattern],
    console: Console | None = None,
) -> None:
    if console is None:
        console = Console()

    if not patterns:
        console.print(
            Panel(
                "尚无足够数据分析弱点。至少需要 5 次已验证会话才能给出有意义的归纳。",
                title="🎯 弱点分析",
                border_style="dim",
            )
        )
        return

    table = Table(title="🎯 你最常犯的错", show_lines=True)
    table.add_column("#", no_wrap=True, style="dim")
    table.add_column("弱点", style="red")
    table.add_column("次数", justify="right", style="yellow")
    table.add_column("占比", justify="right")
    table.add_column("改进建议", style="green")

    for i, p in enumerate(patterns, 1):
        table.add_row(
            str(i),
            p.description,
            str(p.occurrences),
            f"{p.pct:.1f}%",
            p.suggestion,
        )

    console.print(table)


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _fmt_r(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f} R"
