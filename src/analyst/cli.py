"""CLI 入口 - 所有命令都从这里注册。"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(
    name="analyst",
    help="📊 Crypto Analyst - AI 行情分析、预测与结果验证",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════
def _normalize_symbol(symbol: str) -> str:
    """BTC -> BTC/USDT"""
    s = symbol.upper()
    if "/" not in s:
        s = f"{s}/USDT"
    return s


def _parse_period(period: str) -> int:
    """解析 30d / 4w 等格式为天数。"""
    p = period.strip().lower()
    if p.endswith("d"):
        return int(p[:-1])
    if p.endswith("w"):
        return int(p[:-1]) * 7
    if p.endswith("m"):
        return int(p[:-1]) * 30
    return int(p)


def _render_market(ctx) -> None:
    """渲染市场快照面板。"""
    m = ctx.market
    structure = ctx.structure
    fib = ctx.fib

    price_panel = Panel(
        f"[bold yellow]{m.current_price:.4f}[/bold yellow]\n"
        f"24h: {m.low_24h:.2f} - {m.high_24h:.2f}\n"
        f"7d:  {m.low_7d:.2f} - {m.high_7d:.2f}\n"
        f"30d: {m.low_30d:.2f} - {m.high_30d:.2f}",
        title=f"💹 {m.symbol}",
        border_style="cyan",
    )

    structure_panel = Panel(
        f"趋势：[bold]{structure.trend}[/bold]\n"
        f"近高：{structure.recent_high:.2f}\n"
        f"近低：{structure.recent_low:.2f}\n"
        f"分界：{structure.key_pivot:.2f}\n"
        f"阻力：{', '.join(f'{r:.2f}' for r in structure.resistances) or '-'}\n"
        f"支撑：{', '.join(f'{s:.2f}' for s in structure.supports) or '-'}",
        title="📐 结构",
        border_style="magenta",
    )

    fib_panel = Panel(
        f"H: {fib.high:.2f}  L: {fib.low:.2f}  Δ: {fib.range:.2f}\n"
        f"0.382: {fib.retr_382:.2f}\n"
        f"0.500: {fib.retr_500:.2f}\n"
        f"0.618: {fib.retr_618:.2f}\n"
        f"0.786: {fib.retr_786:.2f}\n"
        f"1.272: {fib.ext_1272:.2f}",
        title="🔢 斐波回撤",
        border_style="blue",
    )

    console.print(price_panel)
    console.print(structure_panel)
    console.print(fib_panel)


def _plan_to_table_text(plan) -> str:
    if plan.direction == "wait":
        return f"[yellow]观望[/yellow]\n{plan.rationale}"
    return (
        f"方向：[bold]{plan.direction}[/bold]\n"
        f"入场区：{plan.entry_low:.2f} - {plan.entry_high:.2f}\n"
        f"止损：{plan.stop_loss:.2f}\n"
        f"止盈 1：{plan.take_profit_1:.2f}\n"
        + (f"止盈 2：{plan.take_profit_2:.2f}\n" if plan.take_profit_2 else "")
        + f"R:R：{plan.rr_ratio:.2f}\n\n{plan.rationale}"
    )


# ═══════════════════════════════════════════════════════════════
# 分析会话命令
# ═══════════════════════════════════════════════════════════════
@app.command()
def practice(
    symbol: str = typer.Argument(..., help="品种，如 BTC / ETH / SOL"),
    timeframe: str = typer.Option("4h", "--tf", help="主时间周期"),
):
    """🎯 开启一次 AI 分析会话（拉盘 → AI 计划 → 落库，稍后 CLI verify 回溯）。"""
    from analyst.config import get_settings
    from analyst.llm.analyst import analyze_market
    from analyst.storage import repo
    from analyst.storage.models import AIPlan
    from analyst.training import session as ts

    sym = _normalize_symbol(symbol)

    # 1. 创建会话
    try:
        with console.status(f"[bold cyan]拉取 {sym} 多周期数据..."):
            ctx = ts.create_session(sym, timeframe)
    except Exception as e:
        console.print(f"[bold red]❌ 数据拉取失败：{e}[/bold red]")
        raise typer.Exit(1) from e

    # 2. 渲染市场快照
    _render_market(ctx)

    # 3. 调用 AI
    try:
        with console.status("[bold green]🤖 AI 分析中..."):
            market_dict = ctx.market.to_dict()
            market_dict["primary_timeframe"] = ctx.db_session.timeframe
            ai_response = analyze_market(
                market_snapshot=market_dict,
                indicators_snapshot=ctx.indicators,
            )
    except Exception as e:
        console.print(f"[bold red]❌ AI 调用失败：{e}[/bold red]")
        raise typer.Exit(1) from e

    repo.save_ai_plan(
        AIPlan(
            session_id=ctx.db_session.id,
            direction=ai_response.plan.direction,
            entry_low=ai_response.plan.entry_low,
            entry_high=ai_response.plan.entry_high,
            stop_loss=ai_response.plan.stop_loss,
            take_profit_1=ai_response.plan.take_profit_1,
            take_profit_2=ai_response.plan.take_profit_2,
            confidence=3,
            rationale=ai_response.plan.rationale,
            rr_ratio=ai_response.plan.rr_ratio,
            raw_response=ai_response.raw_text,
            prompt_version=ai_response.prompt_version,
            model_id=ai_response.model,
            cost_usd=ai_response.cost_usd,
        )
    )
    repo.update_session_status(ctx.db_session.id, "ai_planned")

    console.print(
        Panel(
            _plan_to_table_text(ai_response.plan),
            title="🤖 AI 计划",
            border_style="green",
        )
    )

    # 4. 提示验证时机
    settings = get_settings()
    verify_at = datetime.now(timezone.utc) + timedelta(
        hours=settings.verification_delay_hours
    )

    console.print(
        f"\n[bold green]✅ 会话 #{ctx.db_session.id} 已记录[/bold green]"
    )
    console.print(
        f"[dim]💰 本次 AI 成本：${ai_response.cost_usd:.4f} "
        f"({ai_response.latency_ms}ms)[/dim]"
    )
    console.print(
        f"[dim]⏰ 请在 {verify_at:%Y-%m-%d %H:%M UTC} 后运行 "
        f"[cyan]analyst verify[/cyan][/dim]"
    )


# ═══════════════════════════════════════════════════════════════
# 验证命令
# ═══════════════════════════════════════════════════════════════
@app.command()
def verify(
    session: Optional[int] = typer.Option(None, "--session", help="只验证指定会话"),
):
    """✅ 验证已到期会话"""
    from analyst.storage import repo

    if session is not None:
        s = repo.get_session(session)
        if not s:
            console.print(f"[red]会话 #{session} 不存在[/red]")
            raise typer.Exit(1)
        if s.status != "ai_planned":
            console.print(
                f"[yellow]会话 #{session} 状态是 {s.status}，已不需要验证[/yellow]"
            )
            return
        sessions_to_verify = [s]
    else:
        sessions_to_verify = repo.list_pending_verification()

    if not sessions_to_verify:
        console.print("[dim]✨ 没有待验证的会话[/dim]")
        return

    console.print(f"[cyan]共 {len(sessions_to_verify)} 个会话待验证[/cyan]\n")
    for s in sessions_to_verify:
        try:
            _verify_one(s)
        except Exception as e:
            console.print(f"[red]会话 #{s.id} 验证失败：{e}[/red]")


def _verify_one(s) -> None:
    """验证单个会话。"""
    from analyst.compute.plan import TradePlan
    from analyst.storage import repo
    from analyst.storage.models import Verification
    from analyst.training.verify import (
        TradeOutcome,
        fetch_future_candles,
        find_optimal_trade,
        verify_plan,
    )

    user_plan_db = repo.get_user_plan(s.id)
    ai_plan_db = repo.get_ai_plan(s.id)

    if not ai_plan_db:
        console.print(f"[yellow]会话 #{s.id} 没有 AI 计划，跳过[/yellow]")
        return

    console.print(
        f"[cyan]验证会话 #{s.id}[/cyan] {s.symbol} {s.timeframe} "
        f"[dim]@ {s.created_at}[/dim]"
    )

    candles = fetch_future_candles(s.symbol, s.created_at, timeframe="1h")
    if not candles:
        console.print("[yellow]  验证 K线不足，跳过[/yellow]")
        return

    # 用户结果
    if user_plan_db:
        user_plan = TradePlan(
            direction=user_plan_db.direction,
            entry_low=user_plan_db.entry_low,
            entry_high=user_plan_db.entry_high,
            stop_loss=user_plan_db.stop_loss,
            take_profit_1=user_plan_db.take_profit_1,
            take_profit_2=user_plan_db.take_profit_2,
            rr_ratio=user_plan_db.rr_ratio,
            rationale=user_plan_db.rationale,
        )
        user_result = verify_plan(user_plan, candles)
        user_outcome = user_result.outcome.value
        user_pnl = user_result.pnl_r
    else:
        user_outcome = TradeOutcome.NO_TRIGGER.value
        user_pnl = 0.0

    # AI 结果
    ai_plan = TradePlan(
        direction=ai_plan_db.direction,
        entry_low=ai_plan_db.entry_low,
        entry_high=ai_plan_db.entry_high,
        stop_loss=ai_plan_db.stop_loss,
        take_profit_1=ai_plan_db.take_profit_1,
        take_profit_2=ai_plan_db.take_profit_2,
        rr_ratio=ai_plan_db.rr_ratio,
        rationale=ai_plan_db.rationale,
    )
    ai_result = verify_plan(ai_plan, candles)

    # 最优参考线
    optimal_dir = (
        ai_plan.direction
        if ai_plan.direction != "wait"
        else (user_plan_db.direction if user_plan_db else "wait")
    )
    optimal = find_optimal_trade(optimal_dir, candles)

    v = Verification(
        session_id=s.id,
        actual_high=max(c.high for c in candles),
        actual_low=min(c.low for c in candles),
        actual_close=candles[-1].close,
        user_outcome=user_outcome,
        user_pnl_r=user_pnl,
        ai_outcome=ai_result.outcome.value,
        ai_pnl_r=ai_result.pnl_r,
        optimal_pnl_r=optimal.pnl_r,
        notes="",
    )
    repo.save_verification(v)
    repo.update_session_status(s.id, "verified")

    table = Table(show_header=True, header_style="bold")
    table.add_column("方", style="cyan")
    table.add_column("结果")
    table.add_column("R 倍数", justify="right")

    table.add_row("AI", ai_result.outcome.value, _fmt_r(ai_result.pnl_r))
    table.add_row("最优参考", "-", _fmt_r(optimal.pnl_r))

    console.print(table)
    console.print()


# ═══════════════════════════════════════════════════════════════
# 统计命令
# ═══════════════════════════════════════════════════════════════
@app.command()
def progress(
    period: str = typer.Option("30d", help="周期，如 7d, 30d, 90d"),
):
    """📈 查看你的成长曲线"""
    from analyst.stats.progress import calculate
    from analyst.stats.report import render_progress

    days = _parse_period(period)
    report = calculate(days)
    render_progress(report, console)


@app.command()
def weakness(top: int = typer.Option(5, "--top", help="只看前 N 个弱点")):
    """🎯 找出你最常犯的错"""
    from analyst.stats.report import render_weakness
    from analyst.stats.weakness import detect_weaknesses

    patterns = detect_weaknesses(top_n=top)
    render_weakness(patterns, console)


@app.command("ai-benchmark")
def ai_benchmark_cmd(
    period: str = typer.Option("30d", help="统计周期，如 7d, 30d"),
):
    """📊 已验证会话里 AI 侧表现摘要（不依赖用户是否提交计划）"""
    from rich.table import Table

    from analyst.stats.ai_benchmark import calculate_ai_benchmark

    days = _parse_period(period)
    r = calculate_ai_benchmark(period_days=days)
    if r.verified_count == 0:
        console.print(f"[dim]近 {days} 天暂无已验证会话[/dim]")
        return
    table = Table(title=f"📊 AI 基准（近 {days} 天 · 已验证）")
    table.add_column("项", style="cyan")
    table.add_column("值", justify="right")
    table.add_row("已验证会话", str(r.verified_count))
    table.add_row("实际触发次数", str(r.ai_triggered))
    table.add_row("触发后胜率", f"{r.ai_win_rate:.0%}")
    table.add_row("平均 AI R", f"{r.avg_ai_pnl_r:+.2f}")
    table.add_row("含用户计划的会话", str(r.sessions_with_user_plan))
    console.print(table)


@app.command()
def history(
    limit: int = typer.Option(20, help="显示条数"),
    symbol: Optional[str] = typer.Option(None, help="按品种过滤"),
):
    """📚 历史会话列表"""
    from analyst.storage import repo

    sym = _normalize_symbol(symbol) if symbol else None
    sessions = repo.list_sessions(limit=limit, symbol=sym)

    if not sessions:
        console.print("[dim]暂无会话[/dim]")
        return

    table = Table(title="📚 历史会话")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("时间")
    table.add_column("品种")
    table.add_column("周期")
    table.add_column("状态", style="bold")

    for s in sessions:
        status_style = {
            "verified": "green",
            "ai_planned": "yellow",
            "user_planned": "blue",
            "ai_running": "cyan",
            "ai_failed": "red",
            "created": "dim",
            "expired": "red",
        }.get(s.status, "white")

        table.add_row(
            str(s.id),
            s.created_at.strftime("%m-%d %H:%M"),
            s.symbol,
            s.timeframe,
            f"[{status_style}]{s.status}[/{status_style}]",
        )

    console.print(table)


@app.command()
def review(session_id: int = typer.Argument(..., help="会话 ID")):
    """🔍 复盘单个会话（AI 计划 + 验证）"""
    from analyst.storage import repo

    s = repo.get_session(session_id)
    if not s:
        console.print(f"[red]会话 #{session_id} 不存在[/red]")
        raise typer.Exit(1)

    ai_plan_db = repo.get_ai_plan(session_id)
    verification = repo.get_verification(session_id)

    console.print(
        Panel.fit(
            f"[bold]{s.symbol} {s.timeframe}[/bold]\n"
            f"创建：{s.created_at}\n"
            f"状态：[bold]{s.status}[/bold]",
            title=f"🔍 会话 #{s.id}",
            border_style="cyan",
        )
    )

    if ai_plan_db:
        a = _row_to_plan(ai_plan_db)
        console.print(
            Panel(_plan_to_table_text(a), title="🤖 AI 计划", border_style="green")
        )

    if verification:
        table = Table(title="✅ 验证结果", show_header=True)
        table.add_column("项", style="cyan")
        table.add_column("结果")
        table.add_column("R 倍数", justify="right")
        table.add_row("AI", verification.ai_outcome, _fmt_r(verification.ai_pnl_r))
        table.add_row("最优参考", "-", _fmt_r(verification.optimal_pnl_r))
        console.print(table)


def _row_to_plan(row):
    from analyst.compute.plan import TradePlan

    return TradePlan(
        direction=row.direction,
        entry_low=row.entry_low,
        entry_high=row.entry_high,
        stop_loss=row.stop_loss,
        take_profit_1=row.take_profit_1,
        take_profit_2=row.take_profit_2,
        rr_ratio=row.rr_ratio,
        rationale=row.rationale,
    )


def _fmt_r(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f} R"


# ═══════════════════════════════════════════════════════════════
# data 子命令
# ═══════════════════════════════════════════════════════════════
data_app = typer.Typer(help="数据相关命令")
app.add_typer(data_app, name="data")


@data_app.command("status")
def data_status():
    """查看数据缓存状态"""
    from analyst.data.fetcher import get_cache

    cache = get_cache()
    console.print(f"缓存目录：[cyan]{cache.directory}[/cyan]")
    console.print(f"条目数：{len(cache)}")
    console.print(f"占用：{cache.volume() / 1024:.1f} KB")


@data_app.command("refresh")
def data_refresh(symbol: str = typer.Argument(...)):
    """强制刷新指定品种的所有周期"""
    from analyst.data import fetcher

    sym = _normalize_symbol(symbol)
    with console.status(f"刷新 {sym}..."):
        for tf in ["1d", "4h", "1h", "30m"]:
            fetcher.fetch_candles(sym, timeframe=tf, use_cache=False)
    console.print(f"[green]✅ {sym} 已刷新[/green]")


# ═══════════════════════════════════════════════════════════════
# db 子命令
# ═══════════════════════════════════════════════════════════════
db_app = typer.Typer(help="数据库管理命令")
app.add_typer(db_app, name="db")


@db_app.command("init")
def db_init():
    """初始化数据库"""
    from analyst.storage.db import init_db

    init_db()
    console.print("[green]✅ 数据库已初始化[/green]")


@db_app.command("backup")
def db_backup(
    output: str = typer.Option("backup.db", help="备份文件名"),
):
    """备份数据库"""
    from analyst.config import get_settings

    settings = get_settings()
    db_path = settings.database_url.replace("sqlite:///", "")
    if db_path.startswith("./"):
        db_path = db_path[2:]
    try:
        shutil.copy(db_path, output)
        console.print(f"[green]✅ 已备份到 {output}[/green]")
    except FileNotFoundError:
        console.print(f"[red]数据库文件不存在：{db_path}[/red]")
        raise typer.Exit(1) from None


# ═══════════════════════════════════════════════════════════════
# config 子命令
# ═══════════════════════════════════════════════════════════════
config_app = typer.Typer(help="配置管理")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show():
    """显示当前配置"""
    from analyst.config import get_settings

    settings = get_settings()
    table = Table(title="⚙️  当前配置")
    table.add_column("键", style="cyan")
    table.add_column("值")

    for k, v in settings.model_dump().items():
        if "key" in k.lower() and v:
            v = f"{str(v)[:8]}***"
        table.add_row(k, str(v))

    console.print(table)


@config_app.command("test-llm")
def config_test_llm():
    """🔌 测试 LLM API 连通性（不消耗大额 token）"""
    from analyst.config import get_settings
    from analyst.llm.analyst import analyze_market

    settings = get_settings()
    gkq = (settings.groq_api_key or "").strip()
    bk = (settings.bai_api_key or "").strip()
    bm = (settings.bai_model or "").strip()
    console.print(
        f"[cyan]Provider:[/cyan] {settings.llm_provider}  "
        f"[cyan]Model:[/cyan] {settings.llm_model}"
    )
    if gkq and getattr(settings, "llm_try_groq_first", True):
        console.print(f"[cyan]链路 1 Groq:[/cyan] {settings.groq_model}")
    from analyst.llm.analyst import list_free_endpoints

    free_eps = list_free_endpoints(settings)
    if free_eps:
        console.print(
            "[cyan]免费层:[/cyan] "
            + " → ".join(f"{e['name']}「{e['model']}」" for e in free_eps)
        )
    if bk and bm and getattr(settings, "llm_try_bai_after_groq", True):
        console.print(
            f"[cyan]链路 b.ai:[/cyan] {settings.bai_model}（再失败用主 Provider）"
        )

    fake_market = {
        "symbol": "BTC/USDT",
        "captured_at": 0,
        "current_price": 100000.0,
        "high_24h": 102000.0,
        "low_24h": 98000.0,
        "high_7d": 105000.0,
        "low_7d": 95000.0,
        "high_30d": 110000.0,
        "low_30d": 90000.0,
    }
    fake_indicators = {
        "1d": {"macd": {"dif": 100.0, "dea": 80.0, "histogram": 40.0,
                        "above_zero": True, "cross_signal": None},
               "ema": {"ema7": 100500, "ema30": 99000, "ema52": 97000},
               "boll": {"upper": 105000, "middle": 100000, "lower": 95000}},
        "4h": {"macd": {"dif": 50, "dea": 60, "histogram": -20,
                        "above_zero": True, "cross_signal": "death"},
               "ema": {"ema7": 100200, "ema30": 99800, "ema52": 99200},
               "boll": {"upper": 102000, "middle": 100000, "lower": 98000}},
        "1h": {"macd": {"dif": 10, "dea": 20, "histogram": -20,
                        "above_zero": True, "cross_signal": None},
               "ema": {"ema7": 100100, "ema30": 100000, "ema52": 99900}},
    }

    try:
        with console.status("[bold green]测试调用..."):
            response = analyze_market(fake_market, fake_indicators)
    except Exception as e:
        console.print(f"[bold red]❌ 失败：{e}[/bold red]")
        raise typer.Exit(1) from e

    console.print("[bold green]✅ 连通成功[/bold green]")
    console.print(f"  方向：{response.plan.direction}")
    console.print(f"  延迟：{response.latency_ms} ms")
    console.print(f"  成本：${response.cost_usd:.6f}")
    console.print(f"  R:R：{response.plan.rr_ratio:.2f}")


# ═══════════════════════════════════════════════════════════════
# 实时监控（双线反转 + Binance WS）
# ═══════════════════════════════════════════════════════════════
monitor_app = typer.Typer(help="📡 实时监控与可交易提醒（不下单）")
app.add_typer(monitor_app, name="monitor")


@monitor_app.command("once")
def monitor_once(
    symbol: str = typer.Argument("BTC", help="币种，如 BTC / ETH"),
    timeframe: str = typer.Option(None, "--timeframe", "-t", help="K 线周期"),
    market: str = typer.Option(None, "--market", help="spot 或 futures"),
    fib_zone: bool = typer.Option(False, "--fib-zone", help="要求价格在斐波入场区"),
    volume: bool = typer.Option(False, "--volume", help="启用量能过滤"),
):
    """一眼评估当前双线反转信号（REST，不挂 WS）。"""
    from analyst.compute.strategies.double_line_reversal import DoubleLineConfig
    from analyst.config import get_settings
    from analyst.monitor.engine import MonitorConfig, MonitorEngine
    from analyst.monitor.notifier import build_default_notifier

    settings = get_settings()
    sym = _normalize_symbol(symbol)
    tf = timeframe or settings.monitor_timeframe
    mkt = market or settings.monitor_market
    cfg = MonitorConfig(
        symbol=sym,
        timeframe=tf,
        market=mkt,
        strategy=DoubleLineConfig(
            kelly_scale=settings.monitor_kelly_scale,
            stop_buffer_pct=settings.monitor_stop_buffer_pct,
            stop_buffer_atr_mult=settings.monitor_stop_buffer_atr_mult,
            take_profit_r=settings.monitor_take_profit_r,
            max_chase_atr=settings.monitor_max_chase_atr,
            ema_trend_period=settings.monitor_ema_trend_period,
            require_ema200=settings.monitor_require_ema200,
            require_ema_slope=settings.monitor_require_ema_slope,
            trail_to_8r=settings.monitor_trail_to_8r,
            require_fib_zone=fib_zone or settings.monitor_require_fib_zone,
            require_volume=volume or settings.monitor_require_volume,
            require_adx=settings.monitor_require_adx,
            adx_period=settings.monitor_adx_period,
            adx_min=settings.monitor_adx_min,
            use_conditional_edge=settings.monitor_use_conditional_edge,
            min_conditional_win_rate=settings.monitor_min_conditional_win_rate,
        ),
    )
    engine = MonitorEngine(cfg, notifier=build_default_notifier(
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
    ))
    with console.status(f"[bold green]拉取 {sym} {tf} …"):
        signal = engine.evaluate_once()

    if signal.direction == "wait":
        console.print(
            Panel(
                f"[yellow]观望[/yellow]\n"
                f"价格：{signal.price:.4f}\n"
                f"形态：{signal.pattern or '-'}  突破位：{signal.break_level or '-'}\n"
                + "\n".join(signal.reasons),
                title=f"📡 {sym} · {tf}",
                border_style="yellow",
            )
        )
    else:
        engine.notifier.notify(sym, tf, signal)


@monitor_app.command("start")
def monitor_start(
    symbol: str = typer.Argument("BTC", help="币种，如 BTC / ETH"),
    timeframe: str = typer.Option(None, "--timeframe", "-t", help="K 线周期"),
    market: str = typer.Option(None, "--market", help="spot 或 futures"),
    fib_zone: bool = typer.Option(False, "--fib-zone", help="要求价格在斐波入场区"),
    volume: bool = typer.Option(False, "--volume", help="启用量能过滤"),
):
    """订阅 Binance WebSocket，收盘 K 触发双线反转提醒（Ctrl+C 停止）。"""
    from analyst.compute.strategies.double_line_reversal import DoubleLineConfig
    from analyst.config import get_settings
    from analyst.monitor.engine import MonitorConfig, run_monitor_blocking
    from analyst.monitor.notifier import build_default_notifier

    settings = get_settings()
    sym = _normalize_symbol(symbol)
    tf = timeframe or settings.monitor_timeframe
    mkt = market or settings.monitor_market
    console.print(
        Panel(
            f"品种：[bold]{sym}[/bold]\n"
            f"周期：{tf} · 市场：{mkt}\n"
            f"策略：双线反转(K线形态) + EMA{settings.monitor_ema_trend_period}"
            f" + 止损缓冲{settings.monitor_stop_buffer_pct:g}%"
            f" + {settings.monitor_take_profit_r:g}R"
            f" + Kelly×{settings.monitor_kelly_scale}\n"
            f"[dim]对齐视频口述规则；仅提醒，不自动下单。Ctrl+C 退出。[/dim]",
            title="📡 实时监控启动",
            border_style="cyan",
        )
    )
    cfg = MonitorConfig(
        symbol=sym,
        timeframe=tf,
        market=mkt,
        strategy=DoubleLineConfig(
            kelly_scale=settings.monitor_kelly_scale,
            stop_buffer_pct=settings.monitor_stop_buffer_pct,
            stop_buffer_atr_mult=settings.monitor_stop_buffer_atr_mult,
            take_profit_r=settings.monitor_take_profit_r,
            max_chase_atr=settings.monitor_max_chase_atr,
            ema_trend_period=settings.monitor_ema_trend_period,
            require_ema200=settings.monitor_require_ema200,
            require_ema_slope=settings.monitor_require_ema_slope,
            trail_to_8r=settings.monitor_trail_to_8r,
            require_fib_zone=fib_zone or settings.monitor_require_fib_zone,
            require_volume=volume or settings.monitor_require_volume,
            require_adx=settings.monitor_require_adx,
            adx_period=settings.monitor_adx_period,
            adx_min=settings.monitor_adx_min,
            use_conditional_edge=settings.monitor_use_conditional_edge,
            min_conditional_win_rate=settings.monitor_min_conditional_win_rate,
        ),
    )
    notifier = build_default_notifier(
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
    )
    run_monitor_blocking(cfg, notifier=notifier)


# ═══════════════════════════════════════════════════════════════
# 回测
# ═══════════════════════════════════════════════════════════════
@app.command()
def backtest(
    symbol: str = typer.Argument("BTC", help="币种，如 BTC / ETH / SOL"),
    timeframe: str = typer.Option("15m", "--timeframe", "-t", help="K 线周期"),
    bars: int = typer.Option(1000, "--bars", help="回放历史根数（≤1500）"),
    market: str = typer.Option("futures", "--market", help="spot 或 futures"),
    rules: bool = typer.Option(True, "--rules/--no-rules", help="是否统计规则告警命中率"),
    horizon: int = typer.Option(12, "--horizon", help="规则前瞻窗口（根）"),
    max_hold: int = typer.Option(96, "--max-hold", help="策略单笔最长持仓（根）"),
    json_out: Optional[str] = typer.Option(None, "--json", help="结果另存 JSON 文件"),
):
    """🧪 历史回放回测：双线反转策略胜率 + 各规则告警前瞻命中率。"""
    import json as _json

    from analyst.backtest import run_backtest
    from analyst.compute.strategies.double_line_reversal import DoubleLineConfig
    from analyst.config import get_settings

    settings = get_settings()
    sym = _normalize_symbol(symbol)
    strategy_cfg = DoubleLineConfig(
        kelly_scale=settings.monitor_kelly_scale,
        stop_buffer_pct=settings.monitor_stop_buffer_pct,
        stop_buffer_atr_mult=settings.monitor_stop_buffer_atr_mult,
        take_profit_r=settings.monitor_take_profit_r,
        max_chase_atr=settings.monitor_max_chase_atr,
        ema_trend_period=settings.monitor_ema_trend_period,
        require_ema200=settings.monitor_require_ema200,
        require_ema_slope=settings.monitor_require_ema_slope,
        require_volume=settings.monitor_require_volume,
        require_adx=settings.monitor_require_adx,
        adx_period=settings.monitor_adx_period,
        adx_min=settings.monitor_adx_min,
        use_conditional_edge=settings.monitor_use_conditional_edge,
        min_conditional_win_rate=settings.monitor_min_conditional_win_rate,
    )

    with console.status(f"[bold cyan]回放 {sym} {timeframe} × {bars} 根..."):
        report = run_backtest(
            sym,
            timeframe,
            bars=bars,
            market=market,
            strategy_cfg=strategy_cfg,
            include_rules=rules,
            rule_horizon=horizon,
            max_hold=max_hold,
        )

    span = ""
    if report.start and report.end:
        span = f"{report.start:%m-%d %H:%M} → {report.end:%m-%d %H:%M} UTC"
    console.print(
        Panel(
            f"品种：[bold]{report.symbol}[/bold] · 周期：{report.timeframe} · "
            f"共 {report.bars} 根\n{span}",
            title="🧪 回测范围",
            border_style="cyan",
        )
    )

    # ── 策略结果 ──
    closed = report.closed_trades
    if closed:
        t = Table(title="📐 双线反转策略（触发即模拟下单）")
        t.add_column("项", style="cyan")
        t.add_column("值", justify="right")
        t.add_row("信号次数", str(len(report.trades)))
        t.add_row("已结算", str(len(closed)))
        t.add_row("胜率(TP/SL)", f"{report.win_rate:.0%}")
        t.add_row("累计 R", f"{report.total_r:+.2f}")
        t.add_row("加权累计 R", f"{report.total_weighted_r:+.2f}")
        t.add_row("平均 R/笔", f"{report.avg_r:+.2f}")
        pf = report.profit_factor
        t.add_row("盈亏比 PF", "∞" if pf == float("inf") else f"{pf:.2f}")
        t.add_row("最大回撤", f"{report.max_drawdown_r:.2f} R")
        console.print(t)

        dt = Table(title="交易明细", show_header=True)
        dt.add_column("时间")
        dt.add_column("方向")
        dt.add_column("入场", justify="right")
        dt.add_column("SL", justify="right")
        dt.add_column("TP", justify="right")
        dt.add_column("结果")
        dt.add_column("R", justify="right")
        dt.add_column("持仓根数", justify="right")
        for tr in report.trades:
            style = {"tp": "green", "sl": "red"}.get(tr.outcome, "yellow")
            dt.add_row(
                tr.entry_time.strftime("%m-%d %H:%M"),
                tr.direction,
                f"{tr.entry:.6g}",
                f"{tr.stop_loss:.6g}",
                f"{tr.take_profit:.6g}",
                f"[{style}]{tr.outcome}[/{style}]",
                _fmt_r(tr.pnl_r),
                str(tr.bars_held),
            )
        console.print(dt)
    else:
        console.print(
            "[yellow]该区间内双线反转策略未触发任何可交易信号"
            "（形态+EMA200+突破全过滤后为空）[/yellow]"
        )

    # ── 规则命中率 ──
    if rules and report.rule_stats:
        rt = Table(
            title=f"📡 规则告警前瞻命中率（{horizon} 根内先走 ±1×ATR）"
        )
        rt.add_column("规则", style="cyan")
        rt.add_column("样本", justify="right")
        rt.add_column("命中", justify="right")
        rt.add_column("打脸", justify="right")
        rt.add_column("未决", justify="right")
        rt.add_column("命中率", justify="right")
        rt.add_column("平均前瞻收益", justify="right")
        for name, st in sorted(
            report.rule_stats.items(), key=lambda kv: -kv[1].win_rate
        ):
            wr = st.win_rate
            wr_style = "green" if wr >= 0.55 else ("red" if wr < 0.45 else "yellow")
            rt.add_row(
                name,
                str(st.n),
                str(st.wins),
                str(st.losses),
                str(st.flat),
                f"[{wr_style}]{wr:.0%}[/{wr_style}]",
                f"{st.avg_fwd_ret_pct:+.3f}%",
            )
        console.print(rt)
        console.print(
            "[dim]命中率≈50% 说明该规则单独使用无优势，只适合当上下文参考；"
            "样本 < 10 时结论不可靠。[/dim]"
        )

    if json_out:
        from pathlib import Path as _P

        _P(json_out).write_text(
            _json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        console.print(f"[green]✅ 已保存 {json_out}[/green]")


@app.command("strategies")
def strategies_list():
    """📚 列出策略库（双线反转 + 经典组合策略）。"""
    from analyst.compute.strategies.registry import list_strategies

    t = Table(title="策略库")
    t.add_column("ID", style="cyan")
    t.add_column("名称")
    t.add_column("类型")
    t.add_column("说明")
    t.add_column("CLI 示例", style="dim")
    for s in list_strategies():
        kind = "实时" if s.kind == "realtime" else "组合回测"
        t.add_row(s.id, s.name, kind, s.description, s.cli or "-")
    console.print(t)
    console.print(
        "[dim]实时策略走 monitor；组合策略走 backtest-classic / cycle-status。[/dim]"
    )


@app.command("backtest-classic")
def backtest_classic(
    symbol: str = typer.Argument("BTC", help="币种，如 BTC / ETH / SOL"),
    timeframe: str = typer.Option("4h", "--timeframe", "-t", help="K 线周期"),
    days: int = typer.Option(1095, "--days", help="回测历史天数（自动分页拉取）"),
    strategy: str = typer.Option(
        "donchian", "--strategy", "-s",
        help="donchian / ema_cross / boll_mr / cycle_switch / buy_hold "
             "/ bull_trend / bear_defense / chop_range（分相位手选腿）",
    ),
    long_only: bool = typer.Option(
        True, "--long-only/--long-short",
        help="只做多（5 年回测显示做空腿在加密市场拖累收益）",
    ),
    fee_pct: float = typer.Option(0.05, "--fee", help="单边手续费 %"),
    slippage_pct: float = typer.Option(0.02, "--slippage", help="单边滑点 %"),
    oos_days: int = typer.Option(365, "--oos-days", help="样本外天数（0=不分割）"),
    include_funding: bool = typer.Option(
        True, "--funding/--no-funding",
        help="计入历史资金费（多头付正费率、空头收）",
    ),
    vol_target: float = typer.Option(
        0.0, "--vol-target",
        help="波动率目标化：目标年化波动（如 0.3）；0=关。降回撤、也降收益",
    ),
    window_days: int = typer.Option(
        0, "--windows",
        help="滚动窗口稳健性检查：每段天数（如 180）；0=关",
    ),
):
    """📈 经典组合策略回测：复利收益口径、含成本+资金费、牛熊震荡分段 + 样本外。"""
    from analyst.backtest.classic import (
        STRATEGIES,
        CostModel,
        apply_vol_target,
        build_cycle_regime,
        label_regimes,
        rolling_window_report,
        simulate,
    )
    from analyst.data.derivatives import fetch_funding_history
    from analyst.data.fetcher import fetch_candles_history

    if strategy not in STRATEGIES:
        console.print(f"[red]未知策略 {strategy}，可选：{', '.join(STRATEGIES)}[/red]")
        raise typer.Exit(1)

    sym = _normalize_symbol(symbol)
    cost = CostModel(fee_pct=fee_pct, slippage_pct=slippage_pct)
    with console.status(f"[bold cyan]拉取 {sym} {timeframe} × {days} 天历史..."):
        series = fetch_candles_history(sym, timeframe, days=days, market="futures")
    candles = series.candles
    if len(candles) < 300:
        console.print(f"[red]历史数据不足（{len(candles)} 根）[/red]")
        raise typer.Exit(1)

    fn = STRATEGIES[strategy]
    kwargs = {}
    if strategy in ("donchian", "ema_cross", "boll_mr"):
        kwargs["long_only"] = long_only
    elif strategy == "cycle_switch":
        # 牛熊用 BTC 判定（山寨跟随 BTC beta）
        with console.status("[bold cyan]拉取 BTC 历史构建牛熊判定..."):
            btc = fetch_candles_history(
                "BTC/USDT", timeframe, days=days, market="futures"
            )
        kwargs["regime"] = build_cycle_regime(btc.candles)
        kwargs["symbol"] = sym
    positions = fn(candles, **kwargs) if kwargs else fn(candles)
    if vol_target > 0:
        positions = apply_vol_target(
            candles, positions, timeframe=timeframe, target_annual_vol=vol_target
        )
    funding = None
    if include_funding:
        funding = fetch_funding_history(sym, days=days) or None
    labels = label_regimes(candles)
    rep = simulate(
        candles, positions, strategy=strategy, symbol=sym,
        timeframe=timeframe, cost=cost, regime_labels=labels, funding=funding,
    )

    if strategy == "cycle_switch":
        mode = "（牛市多/熊市反弹空+破位空）"
    elif strategy == "bull_trend":
        mode = "（牛市腿·只多）"
    elif strategy == "bear_defense":
        mode = "（熊市腿·只空半仓）"
    elif strategy == "chop_range":
        mode = "（震荡腿·双向半仓）"
    elif strategy == "buy_hold":
        mode = ""
    else:
        mode = "（只多）" if long_only else "（多空）"
    t = Table(title=f"📈 {strategy}{mode} · "
                    f"{sym} {timeframe} · {candles[0].timestamp:%Y-%m-%d} → "
                    f"{candles[-1].timestamp:%Y-%m-%d}")
    t.add_column("指标", style="cyan")
    t.add_column("值", justify="right")
    t.add_row("总收益（复利）", f"{rep.total_return_pct:+.1f}%")
    t.add_row("年化 CAGR", f"{rep.cagr_pct:+.1f}%")
    t.add_row("最大回撤", f"{rep.max_drawdown_pct:.1f}%")
    t.add_row("夏普（年化）", f"{rep.sharpe:.2f}")
    t.add_row("调仓次数", str(rep.trades))
    t.add_row("持仓时间占比", f"{rep.exposure:.0%}")
    t.add_row("成本假设", f"单边 {cost.one_way * 100:.3f}%")
    if funding is not None:
        t.add_row("资金费净损益", f"{rep.funding_pnl_pct:+.2f}%")
    if vol_target > 0:
        t.add_row("波动率目标", f"年化 {vol_target:.0%}")
    for k, zh in (("bull", "牛市段"), ("bear", "熊市段"), ("chop", "震荡段")):
        t.add_row(
            f"{zh}贡献（{rep.regime_bars.get(k, 0)} 根）",
            f"{rep.regime_return_pct.get(k, 0):+.1f}%",
        )
    console.print(t)

    if oos_days > 0:
        cutoff = candles[-1].timestamp.timestamp() - oos_days * 86400
        idx = next(
            (i for i, c in enumerate(candles)
             if c.timestamp.timestamp() >= cutoff),
            None,
        )
        if idx and idx > 60:
            oos_rep = simulate(
                candles[idx:], positions[idx:], strategy=strategy, symbol=sym,
                timeframe=timeframe, cost=cost, funding=funding,
            )
            console.print(
                f"[bold]样本外（最近 {oos_days} 天）[/bold]：收益 "
                f"{oos_rep.total_return_pct:+.1f}% · 回撤 "
                f"{oos_rep.max_drawdown_pct:.1f}% · 夏普 {oos_rep.sharpe:.2f}"
            )
    if window_days > 0:
        wins = rolling_window_report(
            candles, positions, strategy=strategy, symbol=sym,
            timeframe=timeframe, window_days=window_days,
            cost=cost, funding=funding,
        )
        if wins:
            pos_cnt = sum(1 for r in wins if r.total_return_pct > 0)
            seg = "  ".join(
                f"{r.start:%y-%m}:{r.total_return_pct:+.0f}%" for r in wins
            )
            console.print(
                f"[bold]滚动窗口（每 {window_days} 天）[/bold]："
                f"盈利 {pos_cnt}/{len(wins)} 段\n[dim]{seg}[/dim]"
            )
            if pos_cnt < len(wins) * 0.6:
                console.print(
                    "[yellow]⚠ 超过四成窗口亏损：整体数字可能靠个别行情段撑起，谨慎。[/yellow]"
                )
    console.print(
        "[dim]提示：回测≠未来。上线前先 paper trading，单笔风险 ≤ 账户 1%。[/dim]"
    )


@app.command("backtest-xs")
def backtest_xs(
    symbols: str = typer.Option(
        "BTC,ETH,SOL,BNB,AAVE,UNI,SUI,DOGE", "--symbols",
        help="观察池（逗号分隔），上市晚的币自动延后进入排序",
    ),
    timeframe: str = typer.Option("4h", "--timeframe", "-t"),
    days: int = typer.Option(1095, "--days"),
    lookback_days: int = typer.Option(14, "--lookback", help="动量窗口（天）"),
    top_n: int = typer.Option(2, "--top"),
    rebalance_days: int = typer.Option(7, "--rebalance", help="调仓间隔（天）"),
    short_in_bear: bool = typer.Option(
        True, "--bear-short/--bear-flat", help="熊市做空最弱 / 熊市空仓"
    ),
    include_funding: bool = typer.Option(True, "--funding/--no-funding"),
):
    """🏁 横截面动量组合回测：做多最强、熊市做空最弱（BTC 定相位）。"""
    from analyst.backtest.classic import BARS_PER_YEAR
    from analyst.compute.strategies.cycle_switch import build_cycle_regime
    from analyst.compute.strategies.xs_momentum import (
        XsMomentumConfig,
        backtest_xs_momentum,
        current_xs_ranking,
    )
    from analyst.data.derivatives import fetch_funding_history
    from analyst.data.fetcher import fetch_candles_history

    bars_per_day = BARS_PER_YEAR.get(timeframe, 2190) // 365
    syms = [_normalize_symbol(s) for s in symbols.split(",") if s.strip()]
    series_map = {}
    with console.status(f"[bold cyan]拉取 {len(syms)} 币 × {days} 天..."):
        for s in syms:
            try:
                sr = fetch_candles_history(s, timeframe, days=days, market="futures")
                if len(sr.candles) > 300:
                    series_map[s] = sr.candles
            except Exception as e:
                console.print(f"[yellow]{s} 拉取失败，跳过：{e}[/yellow]")
    if "BTC/USDT" not in series_map:
        console.print("[red]观察池必须含 BTC（定牛熊相位）[/red]")
        raise typer.Exit(1)

    regime = build_cycle_regime(series_map["BTC/USDT"])
    funding_map = None
    if include_funding:
        with console.status("[bold cyan]拉取各币历史资金费..."):
            funding_map = {s: fetch_funding_history(s, days=days) for s in series_map}

    cfg = XsMomentumConfig(
        lookback=lookback_days * bars_per_day,
        rebalance=rebalance_days * bars_per_day,
        top_n=top_n,
        short_in_bear=short_in_bear,
    )
    rep = backtest_xs_momentum(
        series_map, regime, cfg, timeframe=timeframe, funding_map=funding_map
    )

    t = Table(
        title=f"🏁 横截面动量 · {len(series_map)} 币池 · "
              f"{rep.start:%Y-%m-%d} → {rep.end:%Y-%m-%d}"
    )
    t.add_column("指标", style="cyan")
    t.add_column("值", justify="right")
    t.add_row("总收益（复利）", f"{rep.total_return_pct:+.1f}%")
    t.add_row("年化 CAGR", f"{rep.cagr_pct:+.1f}%")
    t.add_row("最大回撤", f"{rep.max_drawdown_pct:.1f}%")
    t.add_row("夏普（年化）", f"{rep.sharpe:.2f}")
    t.add_row("调仓次数", str(rep.rebalances))
    t.add_row("持仓时间占比", f"{rep.exposure:.0%}")
    if include_funding:
        t.add_row("资金费净损益", f"{rep.funding_pnl_pct:+.2f}%")
    t.add_row(
        "参数",
        f"动量 {lookback_days}天 · top{top_n} · {rebalance_days}天调仓 · "
        f"熊市{'做空最弱' if short_in_bear else '空仓'}",
    )
    console.print(t)

    console.print("\n[bold]当前动量排名[/bold]")
    for s, m in current_xs_ranking(series_map, cfg):
        bar = "█" * min(20, int(abs(m) * 60))
        color = "green" if m >= 0 else "red"
        console.print(f"  {s:12s} [{color}]{m:+7.1%} {bar}[/{color}]")
    console.print(
        "[dim]与 cycle_switch 相关性低，适合并行分资金跑；参数平原 12~25 天。[/dim]"
    )


@app.command("backtest-carry")
def backtest_carry(
    symbol: str = typer.Argument("BTC", help="币种"),
    days: int = typer.Option(1095, "--days"),
    enter_apr: float = typer.Option(
        5.5, "--enter-apr", help="建仓门槛：费率 EMA 年化 %（÷1095 得每档）"
    ),
    ema_days: int = typer.Option(7, "--ema-days", help="费率 EMA 窗口（天）"),
    fee_pct: float = typer.Option(0.05, "--fee", help="每腿单边费率 %"),
):
    """💰 资金费套利回测：现货多+永续空 delta 中性收费（方向无关）。"""
    from analyst.backtest.classic import CostModel
    from analyst.compute.strategies.funding_carry import (
        FundingCarryConfig,
        backtest_funding_carry,
        current_carry_status,
    )
    from analyst.data.derivatives import fetch_funding_history

    sym = _normalize_symbol(symbol)
    with console.status(f"[bold cyan]拉取 {sym} 历史资金费 × {days} 天..."):
        funding = fetch_funding_history(sym, days=days)
    if len(funding) < 100:
        console.print(f"[red]资金费样本不足（{len(funding)}）[/red]")
        raise typer.Exit(1)

    cfg = FundingCarryConfig(
        ema_n=ema_days * 3,
        enter_rate=enter_apr / 100.0 / (3 * 365),
    )
    rep = backtest_funding_carry(
        sym, funding, cfg, cost=CostModel(fee_pct=fee_pct, slippage_pct=0.02)
    )

    t = Table(
        title=f"💰 资金费套利 · {sym} · {rep.start:%Y-%m-%d} → {rep.end:%Y-%m-%d}"
    )
    t.add_column("指标", style="cyan")
    t.add_column("值", justify="right")
    t.add_row("总收益（复利）", f"{rep.total_return_pct:+.2f}%")
    t.add_row("年化（全程）", f"{rep.apr_pct:+.2f}%")
    t.add_row("年化（在仓时段）", f"{rep.apr_in_position_pct:+.2f}%")
    t.add_row("最大回撤", f"{rep.max_drawdown_pct:.2f}%")
    t.add_row("在仓时间占比", f"{rep.exposure:.0%}")
    t.add_row("进出往返次数", str(rep.round_trips))
    t.add_row("累计成本", f"{rep.cost_paid_pct:.2f}%")
    t.add_row("在仓平均费率", f"{rep.avg_rate_collected_pct:+.5f}%/8h")
    console.print(t)

    st = current_carry_status(funding[-270:], cfg)
    icon = "🟢" if st.get("signal") == "carry" else "⚪"
    console.print(
        f"\n{icon} 当前：费率 EMA {st.get('ema_rate_pct', 0):+.5f}%/8h"
        f"（年化 {st.get('ema_apr_pct', 0):+.2f}%）· {st.get('note', '')}"
    )
    console.print(
        "[dim]delta 中性：价格涨跌无关，赚多头杠杆的融资成本；"
        "牛熊震荡皆可收，负费率期自动离场。实操需现货+合约双账户等名义对冲。[/dim]"
    )


@app.command("digest")
def digest(
    send: bool = typer.Option(False, "--send", help="生成后推送 Telegram"),
):
    """📋 生成 AI 交易日报（事实来自系统数据，AI 只做总结）。"""
    from analyst.llm.digest import compose_daily_digest

    with console.status("[bold cyan]聚合系统事实并生成日报..."):
        out = compose_daily_digest()
    console.print(
        f"[dim]来源: {out.get('source')}"
        + (f" · {out.get('provider')}/{out.get('model')}" if out.get("model") else "")
        + "[/dim]\n"
    )
    console.print(out.get("text", ""))
    if send:
        from analyst.config import get_settings
        from analyst.monitor.notifier import build_default_notifier

        s = get_settings()
        n = build_default_notifier(
            telegram_bot_token=s.telegram_bot_token,
            telegram_chat_id=s.telegram_chat_id,
        )
        try:
            n.send_text(out.get("text", ""))
            console.print("[green]已推送 Telegram[/green]")
        except Exception as e:
            console.print(f"[yellow]TG 发送失败：{e}[/yellow]")


@app.command("research-ideas")
def research_ideas():
    """🔬 AI 研究助手：基于系统近况提出可回测的改进假设（AI 提假设，回测当法官）。"""
    from analyst.llm.digest import compose_research_ideas

    with console.status("[bold cyan]AI 生成研究假设..."):
        out = compose_research_ideas()
    if not out.get("text"):
        console.print(f"[red]{out.get('error') or '无可用 LLM 线路'}[/red]")
        raise typer.Exit(1)
    console.print(f"[dim]via {out.get('provider')}/{out.get('model')}[/dim]\n")
    console.print(out["text"])
    console.print(
        "\n[dim]提醒：假设 ≠ 结论。逐条回测（5年×3币、看滚动窗口），"
        "单币变好不算数。[/dim]"
    )


@app.command("paper-fuse")
def paper_fuse(
    action: str = typer.Argument("status", help="status / clear"),
    strategy: str = typer.Argument(None, help="clear 时指定策略名；省略=全部"),
):
    """🧯 纸面风控熔断：查看状态 / 恢复被停用的策略。"""
    from analyst.trading.paper import get_paper_broker

    broker = get_paper_broker()
    if action == "clear":
        cleared = broker.clear_strategy_fuse(strategy)
        if cleared:
            console.print(f"[green]已恢复策略：{', '.join(cleared)}[/green]")
        else:
            console.print("[dim]没有可恢复的停用策略。[/dim]")
        return
    st = broker.status()
    fuse = st.get("risk_fuse", {})
    t = Table(title="🧯 纸面风控熔断状态")
    t.add_column("项", style="cyan")
    t.add_column("值", justify="right")
    t.add_row(
        "单日亏损熔断",
        "🔴 生效中（今日停开新仓）" if fuse.get("daily_fuse_active") else "🟢 未触发",
    )
    t.add_row("单日亏损限额", f"{fuse.get('daily_loss_limit_pct', 0):g}%")
    t.add_row("当日起始权益", f"{fuse.get('day_start_equity', 0):.2f}")
    t.add_row("当前权益", f"{st.get('equity', 0):.2f}")
    dis = fuse.get("disabled_strategies") or []
    t.add_row("回撤停用策略", ", ".join(dis) if dis else "无")
    t.add_row("总敞口上限", f"equity × {fuse.get('max_gross_exposure', 0):g}")
    console.print(t)
    if dis:
        console.print("[dim]恢复：analyst paper-fuse clear <策略名>[/dim]")


@app.command("cycle-outlook")
def cycle_outlook(
    timeframe: str = typer.Option("1d", "--timeframe", "-t", help="狼波计算周期（建议 1d）"),
    days: int = typer.Option(800, "--days", help="拉取历史天数"),
    telegram: bool = typer.Option(False, "--telegram", help="同时推送到 Telegram"),
):
    """🔮 Wolfy 四年周期展望：日历牛熊进度 + 狼波动能提醒。"""
    from analyst.compute.cycle_theory import evaluate_cycle_outlook, format_outlook_text
    from analyst.config import get_settings
    from analyst.data.fetcher import fetch_candles_history
    from analyst.monitor.notifier import build_default_notifier

    with console.status(f"[bold cyan]拉取 BTC {timeframe} × {days} 天..."):
        series = fetch_candles_history(
            "BTC/USDT", timeframe, days=days, market="futures"
        )
    outlook = evaluate_cycle_outlook(series)
    cal = outlook.calendar
    zh = {"bull": "牛市", "bear": "熊市"}

    body = (
        f"[bold]图1 刻舟求剑（日历）[/bold]\n"
        f"当前相位：{zh[cal.phase]} 第 {cal.phase_day}/{cal.phase_total_days} 天\n"
        f"下一轮里程碑：{cal.next_milestone.label}\n"
        f"预计日期：{cal.next_milestone.date:%Y-%m-%d}（还有 {cal.days_to_milestone} 天）\n"
        f"本周期牛市起点：{cal.cycle_bull_start:%Y-%m-%d}\n"
    )
    if outlook.wave:
        w = outlook.wave
        body += (
            f"\n[bold]图2 狼波动能（RSI 近似）[/bold]\n"
            f"RSI={w.rsi:.1f} · {w.heat_label} · 20根涨跌 {w.roc_20_pct:+.1f}%\n"
        )
    if outlook.alerts:
        body += "\n[bold yellow]提醒[/bold yellow]\n" + "\n".join(outlook.alerts)
    else:
        body += "\n[dim]暂无临近里程碑提醒（距节点 >90 天）[/dim]"

    console.print(
        Panel(
            body,
            title=f"🔮 周期展望 · BTC · {outlook.as_of:%Y-%m-%d}",
            border_style="yellow",
        )
    )
    console.print(
        "[dim]图1：熊市底起算牛 1064 天 / 熊 364 天；"
        "图2：RSI 热度近似狼波，非 TV 原指标。[/dim]"
    )

    if telegram:
        settings = get_settings()
        text = format_outlook_text(outlook)
        build_default_notifier(
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
        ).send_text(text)
        console.print("[green]已尝试推送 Telegram[/green]")


@app.command("cycle-status")
def cycle_status(
    symbols: str = typer.Argument(
        "BTC,ETH,SOL,BNB", help="逗号分隔的币种列表"
    ),
    timeframe: str = typer.Option("4h", "--timeframe", "-t", help="K 线周期"),
):
    """🧭 当前牛熊相位 + cycle_switch 策略各币的实时目标仓位。"""
    from analyst.compute.cycle_theory import evaluate_cycle_outlook
    from analyst.backtest.classic import (
        HALVING_DATES,
        build_cycle_regime,
        halving_phase,
        positions_cycle_switch,
    )
    from analyst.data.fetcher import fetch_candles_history

    syms = [_normalize_symbol(s) for s in symbols.split(",") if s.strip()]
    with console.status("[bold cyan]拉取 BTC 历史构建牛熊判定..."):
        btc = fetch_candles_history("BTC/USDT", timeframe, days=1825,
                                    market="futures")
    # Wolfy 日历展望（1d 精度更适合四年周期）
    btc_1d = fetch_candles_history("BTC/USDT", "1d", days=800, market="futures")
    outlook = evaluate_cycle_outlook(btc_1d)
    wcal = outlook.calendar
    wzh = {"bull": "牛市", "bear": "熊市"}
    wolfy_lines = (
        f"刻舟求剑：{wzh[wcal.phase]} 第 {wcal.phase_day}/{wcal.phase_total_days} 天 · "
        f"距{wcal.next_milestone.label} {wcal.days_to_milestone} 天"
        f"（{wcal.next_milestone.date:%Y-%m-%d}）"
    )
    if outlook.wave:
        wolfy_lines += f"\n狼波 RSI={outlook.wave.rsi:.0f}（{outlook.wave.heat_label}）"
    if outlook.alerts:
        wolfy_lines += "\n" + "\n".join(outlook.alerts[:4])

    regime = build_cycle_regime(btc.candles)
    now_ts = btc.candles[-1].timestamp
    cal = halving_phase(now_ts)
    reg = regime.get(now_ts, "accum")
    days_since = (now_ts - max(h for h in HALVING_DATES if h <= now_ts)).days
    zh = {"bull": "牛市", "bear": "熊市", "accum": "筑底/中性"}
    console.print(
        Panel(
            wolfy_lines,
            title="🔮 Wolfy 周期展望（图1日历 + 图2狼波）",
            border_style="yellow",
        )
    )
    console.print(
        Panel(
            f"减半日历相位：[bold]{zh[cal]}[/bold]（距上次减半 {days_since} 天）\n"
            f"双确认最终判定：[bold]{zh[reg]}[/bold]\n"
            f"[dim]规则：日历+200日线双确认才算熊；熊市清多、只空 z>1.5 的反弹[/dim]",
            title=f"🧭 cycle_switch 执行相位（BTC · {timeframe} · {now_ts:%Y-%m-%d %H:%M} UTC）",
            border_style="cyan",
        )
    )

    t = Table(title="cycle_switch 当前目标仓位")
    t.add_column("品种", style="cyan")
    t.add_column("目标仓位", justify="right")
    t.add_column("现价", justify="right")
    t.add_column("40根高点(入场)", justify="right")
    t.add_column("20根低点(离场)", justify="right")
    t.add_column("z-score", justify="right")
    for sym in syms:
        s = fetch_candles_history(sym, timeframe, days=1825, market="futures")
        c = s.candles
        pos = positions_cycle_switch(c, regime, symbol=sym)[-1]
        closes = [x.close for x in c]
        w = closes[-20:]
        mean = sum(w) / 20
        std = (sum((v - mean) ** 2 for v in w) / 20) ** 0.5
        z = (closes[-1] - mean) / std if std > 0 else 0.0
        hh = max(x.high for x in c[-41:-1])
        ll = min(x.low for x in c[-21:-1])
        pos_txt = (
            "[green]做多 100%[/green]" if pos > 0
            else (f"[red]做空 {abs(pos):.0%}[/red]" if pos < 0 else "空仓")
        )
        t.add_row(
            sym, pos_txt, f"{closes[-1]:.6g}", f"{hh:.6g}", f"{ll:.6g}",
            f"{z:+.2f}",
        )
    console.print(t)
    console.print(
        "[dim]仅提醒不下单；数据为最近收盘 K（缓存 24h 内）。[/dim]"
    )


# ═══════════════════════════════════════════════════════════════
# Web 界面
# ═══════════════════════════════════════════════════════════════
@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8000, "--port", help="监听端口"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="自动打开浏览器"),
):
    """🌐 启动 Web 界面（推文流风格）"""
    try:
        import analyst.web.server as web_server_mod
    except ImportError:
        console.print(
            "[bold red]❌ Web 依赖未安装[/bold red]\n"
            "请运行: [cyan]pip install -e \".[web]\"[/cyan]"
        )
        raise typer.Exit(1) from None

    url = f"http://{host}:{port}"
    console.print(f"[bold green]🌐 Web 界面启动中: [link]{url}[/link][/bold green]")
    console.print(f"[dim]web.server 源码: {web_server_mod.__file__}[/dim]")

    if open_browser:
        import threading
        import webbrowser

        def _open():
            import time
            time.sleep(1.0)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    web_server_mod.serve(host, port)


if __name__ == "__main__":
    app()
