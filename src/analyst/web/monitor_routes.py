"""实时监控 API + WebSocket。"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, WebSocket
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from analyst.config import get_settings
from analyst.data.fetcher import list_usdt_perp_symbols
from analyst.data.ws_kline import BINANCE_FUTURES_INTERVALS
from analyst.monitor.hub import get_monitor_hub

router = APIRouter(tags=["monitor"])

# 训练/分析快照只含这些周期
ANALYSIS_TIMEFRAMES = ("30m", "1h", "4h", "1d")


class MonitorAnalyzeRequest(BaseModel):
    symbol: str
    timeframe: str = "4h"
    verify_after_hours: int | None = None
    market: str = Field(default="futures")


def _map_analysis_timeframe(tf: str) -> str:
    """把图表周期映射到分析快照可用周期。"""
    t = (tf or "4h").strip().lower()
    if t in ANALYSIS_TIMEFRAMES:
        return t
    if t in ("1m", "3m", "5m", "15m"):
        return "30m"
    if t in ("2h",):
        return "1h"
    if t in ("6h", "8h", "12h"):
        return "4h"
    return "1d"


def _norm_symbol(symbol: str) -> str:
    s = symbol.strip().upper().replace("-", "/")
    if "/" not in s:
        if s.endswith("USDT") and len(s) > 4:
            s = f"{s[:-4]}/USDT"
        else:
            s = f"{s}/USDT"
    return s.split(":")[0]


@router.get("/monitor")
def monitor_page():
    """兼容旧地址：统一跳首页。"""
    return RedirectResponse(url="/", status_code=302)


@router.get("/api/monitor/config")
def monitor_config():
    s = get_settings()
    return {
        "symbols": s.symbols_list,
        "timeframes": BINANCE_FUTURES_INTERVALS,
        "analysis_timeframes": list(ANALYSIS_TIMEFRAMES),
        "markets": ["futures"],
        "defaults": {
            "symbol": "BTC/USDT",
            "timeframe": s.monitor_timeframe
            if s.monitor_timeframe in BINANCE_FUTURES_INTERVALS
            else "15m",
            "market": "futures",
            "chart_mode": "native",
            "analysis_timeframe": "4h",
        },
        "strategy": {
            "stop_buffer_pct": s.monitor_stop_buffer_pct,
            "take_profit_r": s.monitor_take_profit_r,
            "ema_trend_period": s.monitor_ema_trend_period,
            "require_ema200": s.monitor_require_ema200,
            "kelly_scale": s.monitor_kelly_scale,
            "trail_to_8r": s.monitor_trail_to_8r,
        },
        "telegram_ready": bool(
            s.telegram_bot_token.strip() and s.telegram_chat_id.strip()
        ),
        "always_on": bool(s.monitor_always_on),
        "daemon": {
            "symbols": s.daemon_symbols_list,
            "timeframe": s.daemon_timeframes_list[0],
            "timeframes": s.daemon_timeframes_list,
        },
    }


@router.get("/api/monitor/symbols")
def monitor_symbols():
    """可搜索的 U 本位永续交易对列表。"""
    try:
        symbols = list_usdt_perp_symbols()
    except Exception as e:
        # 回退到配置观察列表，避免搜索框不可用
        symbols = get_settings().symbols_list
        return {"symbols": symbols, "source": "config", "error": str(e)}
    return {"symbols": symbols, "source": "binanceusdm"}


@router.get("/api/monitor/history")
def monitor_history(
    symbol: str = Query("BTC/USDT"),
    timeframe: str = Query("15m"),
    market: str = Query("futures"),
    limit: int = Query(300, ge=10, le=1000),
):
    hub = get_monitor_hub()
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "market": "futures",
        "candles": hub.history(symbol, timeframe, market="futures", limit=limit),
    }


@router.get("/api/monitor/daemon")
def monitor_daemon_status():
    """常驻盯盘状态（关网页也能继续推 TG）。"""
    hub = get_monitor_hub()
    info = hub.load_daemon_state()
    running = [
        str(w.key)
        for w in hub._workers.values()
        if hub.is_daemon_key(w.key)
    ]
    health = hub.worker_health()
    info["running"] = running
    info["worker_count"] = len(hub._workers)
    info["alerts_in_memory"] = len(hub._alerts)
    info["workers"] = health
    return info


@router.post("/api/monitor/daemon/sync")
async def monitor_daemon_sync(
    symbols: str = Query("", description="逗号分隔品种"),
    timeframe: str = Query("15m"),
):
    """用当前观察列表热更新常驻盯盘 worker（增删品种无需重启）。"""
    from analyst.config import get_settings

    if not get_settings().monitor_always_on:
        raise HTTPException(
            400,
            "未开启常驻盯盘：请在 .env 设置 MONITOR_ALWAYS_ON=true 后重启一次 Web 服务",
        )
    hub = get_monitor_hub()
    syms = [_norm_symbol(x) for x in symbols.split(",") if x.strip()]
    if not syms:
        raise HTTPException(400, "symbols 不能为空")
    hub.save_daemon_state(syms)
    info = await hub.start_always_on_workers()
    info["message"] = (
        f"已热更新：{len(info.get('symbols') or [])} 品种 · "
        f"运行 {len(info.get('running') or [])} workers"
        + (
            f" · 停止 {len(info.get('stopped') or [])} 个旧流"
            if info.get("stopped")
            else ""
        )
    )
    return info


@router.get("/api/monitor/alerts")
def monitor_alerts(limit: int = Query(50, ge=1, le=200)):
    return {"alerts": get_monitor_hub().recent_alerts(limit)}


class DemoAlertRequest(BaseModel):
    symbol: str = "BTC/USDT"
    timeframe: str = "15m"
    direction: str = "long"
    market: str = "futures"


class MonitorChatRequest(BaseModel):
    question: str
    symbol: str = "BTC/USDT"
    timeframe: str = "4h"
    session_id: int | None = None
    context: dict | None = None
    history: list[dict] | None = None


@router.post("/api/monitor/alerts/demo")
async def monitor_demo_alert(req: DemoAlertRequest):
    """模拟一条双线反转告警：推 WebSocket + Telegram。"""
    hub = get_monitor_hub()
    try:
        alert = await hub.inject_demo_alert(
            symbol=_norm_symbol(req.symbol),
            timeframe=req.timeframe or "15m",
            direction=req.direction,
            market="futures",
        )
    except Exception as e:
        raise HTTPException(500, f"模拟告警失败：{e}") from e
    return {"ok": True, "alert": alert}


@router.post("/api/monitor/chat")
def monitor_chat(req: MonitorChatRequest):
    """针对当前合约追问（可带最近一次分析上下文）。"""
    from analyst.llm.chat import ask_monitor_question
    from analyst.storage import repo
    from analyst.training.session import recompute_market_extras_from_db
    from analyst.web.dto import session_to_dto

    symbol = _norm_symbol(req.symbol)
    tf = _map_analysis_timeframe(req.timeframe)
    context = dict(req.context or {})

    if req.session_id:
        s = repo.get_session(req.session_id)
        if s:
            dto = session_to_dto(s, market_extras=recompute_market_extras_from_db(s))
            for k in (
                "current_price",
                "structure",
                "fib",
                "jack_levels",
                "indicators",
                "baseline_plan",
                "ai_plan",
            ):
                if dto.get(k) is not None:
                    context.setdefault(k, dto.get(k))
            symbol = _norm_symbol(dto.get("symbol") or symbol)
            tf = dto.get("timeframe") or tf

    try:
        out = ask_monitor_question(
            req.question,
            symbol=symbol,
            timeframe=tf,
            context=context,
            history=req.history,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"追问失败：{e}") from e

    # 有会话 id 时落库，便于历史回看
    if req.session_id:
        try:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            chat_log = repo.append_session_chat(
                req.session_id,
                [
                    {"role": "user", "content": req.question.strip(), "created_at": now},
                    {
                        "role": "assistant",
                        "content": out.get("reply") or "",
                        "created_at": now,
                        "model": out.get("model"),
                    },
                ],
            )
            out["session_id"] = req.session_id
            out["chat_log"] = chat_log
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        except Exception as e:
            raise HTTPException(500, f"保存对话失败：{e}") from e
    return out


@router.get("/api/monitor/chat/{session_id}")
def monitor_chat_history(session_id: int):
    """读取某次分析会话的追问记录。"""
    from analyst.storage import repo

    s = repo.get_session(session_id)
    if not s:
        raise HTTPException(404, f"会话 #{session_id} 不存在")
    return {
        "session_id": session_id,
        "symbol": s.symbol,
        "timeframe": s.timeframe,
        "chat_log": list(s.chat_log or []),
    }


@router.post("/api/monitor/analyze")
def monitor_analyze(req: MonitorAnalyzeRequest, background_tasks: BackgroundTasks):
    """针对当前合约：拉多周期指标 + 规则基线，再异步跑 AI 综合计划。"""
    from analyst.training.session import run_ai_job, start_async_analysis

    symbol = _norm_symbol(req.symbol)
    if not symbol:
        raise HTTPException(400, "请提供交易对")

    tf = _map_analysis_timeframe(req.timeframe)
    try:
        dto = start_async_analysis(
            symbol,
            tf,
            req.verify_after_hours,
            market="futures",
        )
    except Exception as e:
        raise HTTPException(500, f"分析失败：{e}") from e

    sid = dto.get("id")
    if sid is None:
        raise HTTPException(500, "会话创建失败：无 id")
    background_tasks.add_task(run_ai_job, sid)
    dto["analysis_timeframe"] = tf
    dto["chart_timeframe"] = req.timeframe
    return dto


@router.get("/api/monitor/cycle-timeline")
def cycle_timeline(days: int = Query(800, ge=200, le=2000)):
    """Wolfy 四年周期时间轴 + 狼波动能（前端绘图用）。"""
    from analyst.compute.cycle_theory import (
        build_wolfy_timeline,
        evaluate_cycle_outlook,
        outlook_to_api_dict,
    )
    from analyst.data.fetcher import fetch_candles_history
    from analyst.monitor.serialize import candle_to_dict

    series = fetch_candles_history("BTC/USDT", "1d", days=days, market="futures")
    if len(series.candles) < 30:
        raise HTTPException(400, "历史数据不足")
    outlook = evaluate_cycle_outlook(series)
    ts = outlook.as_of
    timeline = build_wolfy_timeline(ts, past_cycles=1, future_cycles=1)
    payload = outlook_to_api_dict(outlook, timeline)
    # 日线收盘价（轻量折线背景）
    payload["candles"] = [candle_to_dict(c) for c in series.candles[-400:]]
    return payload


@router.get("/api/strategies")
def strategies_catalog():
    """策略库目录（实时 + 组合）。"""
    from analyst.compute.strategies.registry import list_strategies

    kind_zh = {"realtime": "实时盯盘", "portfolio": "组合回测"}
    return {
        "strategies": [
            {
                "id": s.id,
                "name": s.name,
                "kind": s.kind,
                "kind_label": kind_zh.get(s.kind, s.kind),
                "description": s.description,
                "cli": s.cli,
                "module": s.module,
            }
            for s in list_strategies()
        ],
    }


@router.get("/api/tools/seed-position")
def seed_position_api(
    account: float = Query(10000, gt=0, description="账户权益 USDT"),
    leverage: float = Query(25, gt=0, le=125),
    seed_pct: float = Query(0.04, gt=0, le=0.1, description="头仓占权益比例"),
    max_total_pct: float = Query(0.18, gt=0, le=0.5),
    add_mode: str = Query("pullback", description="pullback|breakout|none"),
):
    """头仓/补仓仓位建议（零下二度风格分层）。"""
    from analyst.compute.position_sizing import plan_seed_position

    try:
        plan = plan_seed_position(
            account,
            leverage=leverage,
            seed_pct=seed_pct,
            max_total_pct=max_total_pct,
            add_mode=add_mode,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return plan.to_dict()


class ClassicBacktestRequest(BaseModel):
    symbol: str = "BTC/USDT"
    timeframe: str = "4h"
    days: int = Field(1095, ge=90, le=2000)
    strategy: str = "donchian"
    long_only: bool = True
    fee_pct: float = 0.05
    slippage_pct: float = 0.02
    oos_days: int = Field(365, ge=0, le=730)


@router.post("/api/backtest/classic")
async def backtest_classic_api(req: ClassicBacktestRequest):
    """经典组合策略回测（Web 表单用）。"""
    from analyst.backtest.classic import (
        STRATEGIES,
        CostModel,
        build_cycle_regime,
        label_regimes,
        simulate,
    )
    from analyst.data.fetcher import fetch_candles_history

    if req.strategy not in STRATEGIES:
        raise HTTPException(400, f"未知策略：{req.strategy}")

    sym = _norm_symbol(req.symbol)
    tf = req.timeframe.strip().lower()
    cost = CostModel(fee_pct=req.fee_pct, slippage_pct=req.slippage_pct)

    import asyncio

    series = await asyncio.to_thread(
        fetch_candles_history, sym, tf, days=req.days, market="futures"
    )
    candles = series.candles
    if len(candles) < 300:
        raise HTTPException(400, f"历史数据不足（{len(candles)} 根）")

    fn = STRATEGIES[req.strategy]
    kwargs: dict = {}
    if req.strategy in ("donchian", "ema_cross", "boll_mr"):
        kwargs["long_only"] = req.long_only
    elif req.strategy == "cycle_switch":
        btc = await asyncio.to_thread(
            fetch_candles_history,
            "BTC/USDT",
            tf,
            days=req.days,
            market="futures",
        )
        kwargs["regime"] = build_cycle_regime(btc.candles)

    positions = fn(candles, **kwargs) if kwargs else fn(candles)
    labels = label_regimes(candles)
    rep = simulate(
        candles,
        positions,
        strategy=req.strategy,
        symbol=sym,
        timeframe=tf,
        cost=cost,
        regime_labels=labels,
    )

    oos = None
    if req.oos_days > 0:
        cutoff = candles[-1].timestamp.timestamp() - req.oos_days * 86400
        idx = next(
            (i for i, c in enumerate(candles) if c.timestamp.timestamp() >= cutoff),
            None,
        )
        if idx and idx > 60:
            oos_rep = simulate(
                candles[idx:],
                positions[idx:],
                strategy=req.strategy,
                symbol=sym,
                timeframe=tf,
                cost=cost,
            )
            oos = oos_rep.to_row()

    return {
        "report": rep.to_row(),
        "oos": oos,
        "range": {
            "start": candles[0].timestamp.isoformat(),
            "end": candles[-1].timestamp.isoformat(),
            "bars": len(candles),
        },
        "cost_one_way_pct": round(cost.one_way * 100, 4),
    }


@router.websocket("/ws/monitor")
async def monitor_ws(
    websocket: WebSocket,
    symbol: str = Query("BTC/USDT"),
    timeframe: str = Query("15m"),
    market: str = Query("futures"),
    watch: str = Query("", description="逗号分隔的后台观察列表"),
):
    hub = get_monitor_hub()
    watch_symbols = [x.strip() for x in watch.split(",") if x.strip()]
    # 监控页只做 U 本位合约多空
    await hub.connect_client(
        websocket,
        symbol,
        timeframe,
        market="futures",
        watch_symbols=watch_symbols,
    )


# ── 纸面模拟炒币 ──


@router.get("/api/paper/status")
def paper_status():
    from analyst.trading.paper import get_paper_broker

    return get_paper_broker().status()


class PaperResetRequest(BaseModel):
    confirm: bool = False
    equity: float | None = None


@router.post("/api/paper/reset")
def paper_reset(req: PaperResetRequest):
    if not req.confirm:
        raise HTTPException(400, "请传 confirm=true 以重置纸面账户")
    from analyst.trading.paper import get_paper_broker

    settings = get_settings()
    start = req.equity if req.equity is not None else settings.monitor_paper_equity
    broker = get_paper_broker()
    broker.reset(starting_equity=float(start))
    return broker.status()
