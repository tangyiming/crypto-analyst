"""FastAPI 路由 - 所有 API 端点。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from analyst.config import get_settings
from analyst.storage import repo
from analyst.web.dto import session_to_dto

router = APIRouter()


# ─────────────────────────────────────
# Request / Response Models
# ─────────────────────────────────────
class PracticeRequest(BaseModel):
    symbol: str
    timeframe: str = "4h"
    verify_after_hours: int | None = None  # None = 用 timeframe 的智能默认


def _registered_retry_post_paths() -> list[str]:
    """用于 /api/health：确认当前进程里是否注册了补跑 POST（排查「404 无路由」多为旧包/未重启）。"""
    out: list[str] = []
    for r in router.routes:
        methods = getattr(r, "methods", None)
        if not methods or "POST" not in methods:
            continue
        p = getattr(r, "path", "") or ""
        if "/retry" in p or p.endswith("/run-ai"):
            out.append(p)
    return sorted(set(out))


def _sqlite_db_basename(database_url: str) -> str | None:
    if not (database_url or "").strip().lower().startswith("sqlite"):
        return None
    raw = database_url.replace("sqlite:///", "", 1)
    return Path(raw).name


# ─────────────────────────────────────
# 健康检查 / 配置
# ─────────────────────────────────────
@router.get("/api/health")
def health():
    s = get_settings()
    gkq = (getattr(s, "groq_api_key", "") or "").strip()
    bk = (getattr(s, "bai_api_key", "") or "").strip()
    bm = (getattr(s, "bai_model", "") or "").strip()
    return {
        "status": "ok",
        "provider": s.llm_provider,
        "model": s.llm_model,
        "groq_try_first": bool(gkq) and getattr(s, "llm_try_groq_first", True),
        "groq_model": s.groq_model if gkq else None,
        "bai_try_second": bool(bk and bm) and getattr(s, "llm_try_bai_after_groq", True),
        "bai_model": s.bai_model if (bk and bm) else None,
        "llm_chain": _llm_chain_summary(s),
        "sqlite_db": _sqlite_db_basename(s.database_url),
        # 浏览器打开 /api/health 可对齐「当前进程」是否为本仓库：与终端 startup 打印的 routes 路径应一致。
        "routes_module": str(Path(__file__).resolve()),
        "retry_post_paths": _registered_retry_post_paths(),
        "endpoints": {
            "practice_sync": "POST /api/sessions/practice",
            "practice_async": "POST /api/sessions/analyze-async",
            "retry_ai": "POST /api/sessions/{id}/retry",
            "retry_ai_alt": "POST /api/sessions/{id}/run-ai",
        },
    }


def _llm_chain_summary(s) -> str:
    """免费层 → b.ai → 主线路（仅展示已启用段）。"""
    from analyst.llm.analyst import list_free_endpoints

    parts: list[str] = []
    for ep in list_free_endpoints(s):
        parts.append(f"{ep['name']}「{ep['model']}」")
    bk = (getattr(s, "bai_api_key", "") or "").strip()
    bm = (getattr(s, "bai_model", "") or "").strip()
    if bk and bm and getattr(s, "llm_try_bai_after_groq", True):
        parts.append(f"b.ai「{bm}」")
    parts.append(f"{(s.llm_provider or 'deepseek').capitalize()}「{s.llm_model}」")
    return " → ".join(parts)


def _infer_real_provider(base_url: str, _configured: str) -> str:
    """主线路 URL 对应三类服务商里哪一种（其余一律视作 DeepSeek）。"""
    url = (base_url or "").lower()
    if "groq.com" in url:
        return "Groq"
    if "b.ai" in url:
        return "b.ai"
    return "DeepSeek"


@router.get("/api/config")
def get_config():
    from analyst.training.session import _DEFAULT_VERIFY_HOURS

    s = get_settings()
    gkq = (getattr(s, "groq_api_key", "") or "").strip()
    bk = (getattr(s, "bai_api_key", "") or "").strip()
    bm = (getattr(s, "bai_model", "") or "").strip()
    return {
        "provider": s.llm_provider,
        "provider_display": _infer_real_provider(s.llm_base_url, s.llm_provider),
        "groq_try_first": bool(gkq) and getattr(s, "llm_try_groq_first", True),
        "groq_model": s.groq_model if gkq else None,
        "bai_try_second": bool(bk and bm) and getattr(s, "llm_try_bai_after_groq", True),
        "bai_model": s.bai_model if (bk and bm) else None,
        "llm_chain": _llm_chain_summary(s),
        "slow_llm": "api.b.ai" in (s.llm_base_url or "").lower()
        or "reasoner" in (s.llm_model or "").lower()
        or (
            bool(bk and bm)
            and getattr(s, "llm_try_bai_after_groq", True)
            and "api.b.ai" in (s.bai_base_url or "").lower()
        ),
        "model": s.llm_model,
        "base_url": s.llm_base_url,
        "symbols": ["BTC", "ETH", "SOL", "BNB", "DOGE", "XRP", "PEPE", "SUI", "AVAX"],
        "timeframes": ["1d", "4h", "2h", "1h", "30m"],
        "verification_delay_hours": s.verification_delay_hours,
        "verify_hours_by_timeframe": _DEFAULT_VERIFY_HOURS,
        "verify_hours_options": [
            {"value": 4, "label": "4h（极短）"},
            {"value": 8, "label": "8h"},
            {"value": 12, "label": "12h"},
            {"value": 24, "label": "24h（推荐 4h 周期）"},
            {"value": 48, "label": "48h"},
            {"value": 72, "label": "72h"},
            {"value": 168, "label": "168h（一周）"},
        ],
    }


# ─────────────────────────────────────
# 会话列表 / 详情
# ─────────────────────────────────────
@router.get("/api/sessions")
def list_sessions(
    limit: int = 50,
    symbol: Optional[str] = None,
):
    """列出所有会话（推文流）。"""
    sym = None
    if symbol:
        sym = symbol if "/" in symbol else f"{symbol.upper()}/USDT"

    sessions = repo.list_sessions(limit=limit, symbol=sym)
    return [session_to_dto(s) for s in sessions]


@router.get("/api/sessions/{sid}")
def get_session(sid: int):
    from analyst.training.session import recompute_market_extras_from_db

    s = repo.get_session(sid)
    if not s:
        raise HTTPException(404, f"会话 #{sid} 不存在")
    extras = recompute_market_extras_from_db(s)
    return session_to_dto(s, market_extras=extras)


# ─────────────────────────────────────
# 触发新分析（核心）
# ─────────────────────────────────────
@router.post("/api/sessions/practice")
def practice(req: PracticeRequest):
    """触发一次 quick 模式分析（同步执行）。

    FastAPI 会把同步函数自动跑在线程池里，不阻塞事件循环。
    一次约 5-15 秒（数据 + LLM）。
    """
    from analyst.training.session import run_practice_quick

    try:
        return run_practice_quick(
            req.symbol,
            req.timeframe,
            verify_after_hours=req.verify_after_hours,
        )
    except Exception as e:
        raise HTTPException(500, f"分析失败：{e}") from e


@router.post("/api/sessions/analyze-async")
def analyze_async(req: PracticeRequest, background_tasks: BackgroundTasks):
    """异步分析：立刻返回会话（含快照）；AI 在后台生成，前端轮询 GET /api/sessions/{id}。"""
    from analyst.training.session import run_ai_job, start_async_analysis

    try:
        dto = start_async_analysis(
            req.symbol,
            req.timeframe,
            req.verify_after_hours,
        )
    except Exception as e:
        raise HTTPException(500, f"创建分析失败：{e}") from e

    sid = dto.get("id")
    if sid is None:
        raise HTTPException(500, "会话创建失败：无 id")
    background_tasks.add_task(run_ai_job, sid)
    return dto


def _retry_ai_browser_page(sid: int) -> HTMLResponse:
    """浏览器用 GET 打开 /retry 时返回可提交的 POST 表单（API 本身只接受 POST）。"""
    s = repo.get_session(sid)
    if not s:
        raise HTTPException(404, f"会话 #{sid} 不存在")
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>补跑 AI · 会话 #{sid}</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;padding:1.5rem;max-width:28rem;margin:0 auto;line-height:1.5}}
button,a.btn{{display:inline-block;background:#2563eb;color:#fff;border:none;padding:.65rem 1.1rem;border-radius:.5rem;font-size:.95rem;cursor:pointer;text-decoration:none}}
a.btn{{background:#475569}}
.muted{{color:#94a3b8;font-size:.85rem;margin-top:1rem}}
</style></head><body>
<h1 style="font-size:1.15rem">会话 #{sid}</h1>
<p>补跑 AI 接口需要使用 <strong>POST</strong>，地址栏直接打开只会触发本说明页。</p>
<form method="post" action="/api/sessions/{sid}/retry">
  <button type="submit">🔄 补跑 AI 分析</button>
</form>
<p class="muted">或在终端执行：<code style="background:#1e293b;padding:.15rem .4rem;border-radius:.25rem">curl -X POST http://127.0.0.1:8000/api/sessions/{sid}/retry</code></p>
<p style="margin-top:1.25rem"><a class="btn" href="/">← 返回首页</a></p>
</body></html>"""
    )


@router.get("/api/sessions/{sid}/retry-ai", response_class=HTMLResponse)
@router.get("/api/sessions/{sid}/retry", response_class=HTMLResponse)
def retry_ai_browser_get(sid: int):
    return _retry_ai_browser_page(sid)


def _retry_ai_post_impl(sid: int):
    from analyst.training.session import retry_quick_session_ai

    try:
        return retry_quick_session_ai(sid)
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            raise HTTPException(404, msg) from e
        raise HTTPException(400, msg) from e
    except Exception as e:
        raise HTTPException(500, f"补跑失败：{e}") from e


@router.post("/api/sessions/{sid}/retry")
def retry_ai(sid: int):
    """补跑 AI（路径 `/retry`）。"""
    return _retry_ai_post_impl(sid)


@router.post("/api/sessions/{sid}/retry-ai")
def retry_ai_dash(sid: int):
    """补跑 AI（路径 `/retry-ai`，与 `/retry` 等价）。"""
    return _retry_ai_post_impl(sid)


@router.post("/api/sessions/{sid}/run-ai")
def retry_ai_run_ai(sid: int):
    """补跑 AI 的备用路径（少数环境对路径中含 retry 的 POST 异常拦截时可改用此路径）。"""
    return _retry_ai_post_impl(sid)


# ─────────────────────────────────────
# 验证
# ─────────────────────────────────────
@router.post("/api/sessions/{sid}/verify")
def verify_one(sid: int, force: bool = False):
    """验证单个会话。

    Args:
        force: 默认 False，K 线不足时拒绝。设为 True 强制验证（结果可能不准）。
    """
    from analyst.compute.plan import TradePlan
    from analyst.storage.models import Verification
    from analyst.training.verify import (
        TradeOutcome,
        fetch_future_candles,
        find_optimal_trade,
        verify_plan,
    )

    s = repo.get_session(sid)
    if not s:
        raise HTTPException(404, f"会话 #{sid} 不存在")

    if s.status == "verified":
        return session_to_dto(s)

    user_plan_db = repo.get_user_plan(sid)
    ai_plan_db = repo.get_ai_plan(sid)
    if not ai_plan_db:
        raise HTTPException(400, "没有 AI 计划，无法验证")

    candles = fetch_future_candles(s.symbol, s.created_at, timeframe="1h")
    min_candles = 3
    if not candles:
        raise HTTPException(400, "还没有任何后续 K 线数据")
    if len(candles) < min_candles and not force:
        raise HTTPException(
            409,
            f"目前只有 {len(candles)} 根 1h K 线（建议至少 {min_candles} 根再验证）。"
            f"如确认要验证，重试时加 ?force=true"
        )

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

    optimal_dir = (
        ai_plan.direction
        if ai_plan.direction != "wait"
        else (user_plan_db.direction if user_plan_db else "wait")
    )
    optimal = find_optimal_trade(optimal_dir, candles)

    v = Verification(
        session_id=sid,
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
    repo.update_session_status(sid, "verified")

    refreshed = repo.get_session(sid)
    return session_to_dto(refreshed)


@router.post("/api/verify-all")
def verify_all():
    """批量验证所有到期会话。"""
    pending = repo.list_pending_verification()
    results = []
    for s in pending:
        try:
            results.append(verify_one(s.id))
        except HTTPException as e:
            results.append({"id": s.id, "error": e.detail})
    return {"verified": len(results), "items": results}
