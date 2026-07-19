"""训练会话状态机。

会话生命周期：
    Web 异步：created → ai_running → ai_planned | ai_failed → verified
    CLI 同步：created → ai_planned → verified
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from analyst.compute import indicators as ind
from analyst.compute.fibonacci import FibLevels, compute_fib
from analyst.compute.jack_levels import JackLevels, compute_jack_levels
from analyst.compute.structure import Structure, detect_structure
from analyst.compute.volume import analyze_volume
from analyst.config import get_settings
from analyst.data.snapshot import MarketSnapshot, build_snapshot
from analyst.storage import repo
from analyst.storage.models import Session


@dataclass
class SessionContext:
    db_session: Session
    market: MarketSnapshot
    indicators: dict
    fib: FibLevels
    structure: Structure
    jack: JackLevels | None = None


# ─────────────────────────────────────
# 智能默认验证时长（按 timeframe × N 根 K 线）
# ─────────────────────────────────────
# 经验值：6-12 根 K 线足够看清止损/止盈是否被触发，
# 既给市场留出演化时间，又不让反馈循环太慢。
_DEFAULT_VERIFY_HOURS = {
    "30m": 6,    # 12 根
    "1h": 12,    # 12 根
    "2h": 24,    # 12 根
    "4h": 24,    # 6 根 = 1 天（与 Jack Li 实践一致）
    "1d": 144,   # 6 天
}


def default_verify_hours(timeframe: str) -> int:
    """根据 K 线周期推荐合理的验证时长（小时）。"""
    return _DEFAULT_VERIFY_HOURS.get(timeframe, 72)


def create_session(
    symbol: str,
    timeframe: str | None = None,
    verify_after_hours: int | None = None,
    market: str = "spot",
) -> SessionContext:
    """创建一次新的训练会话。

    步骤：
    1. 拉数据（多周期）
    2. 算指标（每个周期）
    3. 算结构 + 斐波（主周期）
    4. 入库
    """
    settings = get_settings()
    timeframe = timeframe or settings.default_timeframe
    # 快照固定含 1d/4h/1h/30m；非法周期回退到 4h
    if timeframe not in ("1d", "4h", "1h", "30m"):
        timeframe = "4h"

    market_snap = build_snapshot(symbol, market=market)

    indicators_snap: dict[str, dict] = {}
    for tf, series in market_snap.timeframes.items():
        snap = ind.compute_all(series)
        ind_dict = _serialize_indicators(snap)
        # 量能分析（OBV / 量价关系 / 放量缩量）
        vol = analyze_volume(series)
        ind_dict["volume"] = {
            "ratio": round(vol.volume_ratio, 2),
            "obv_trend": vol.obv_trend,
            "signal": vol.price_volume_signal,
        }
        indicators_snap[tf] = ind_dict

    primary_series = market_snap.timeframes[timeframe]
    structure = detect_structure(primary_series)
    fib = compute_fib(structure.recent_high, structure.recent_low)

    btc_series = None
    if "BTC" not in symbol.upper().replace("/", ""):
        try:
            from analyst.data.fetcher import fetch_candles

            btc_series = fetch_candles(
                "BTC/USDT",
                timeframe="1d",
                limit=60,
                market=market,
            )
        except Exception:
            btc_series = None

    jack = compute_jack_levels(
        current_price=market_snap.current_price,
        structure=structure,
        fib=fib,
        daily_indicators=indicators_snap.get("1d"),
        primary_series=primary_series,
        btc_series=btc_series,
        symbol=symbol,
    )

    expire_at = datetime.utcnow() + timedelta(hours=settings.session_expire_hours)
    verify_hours = verify_after_hours or default_verify_hours(timeframe)

    snap_dict = market_snap.to_dict()
    snap_dict["jack_levels"] = jack.to_dict()
    snap_dict["primary_timeframe"] = timeframe

    db_session = repo.create_session(
        symbol=symbol,
        timeframe=timeframe,
        expire_at=expire_at,
        verify_after_hours=verify_hours,
        market_snapshot=snap_dict,
        indicators_snapshot=indicators_snap,
    )

    return SessionContext(
        db_session=db_session,
        market=market_snap,
        indicators=indicators_snap,
        fib=fib,
        structure=structure,
        jack=jack,
    )


def _ctx_to_market_extras(ctx: SessionContext, latency_ms: int | None = None) -> dict:
    """从内存中的 SessionContext 组装 session_to_dto 用的 market_extras。"""
    from dataclasses import asdict

    from analyst.compute.plan import generate_baseline_plan

    tf = ctx.db_session.timeframe
    baseline = generate_baseline_plan(
        ctx.market.current_price, ctx.fib, ctx.structure, jack=ctx.jack
    )
    out = {
        "current_price": ctx.market.current_price,
        "high_24h": ctx.market.high_24h,
        "low_24h": ctx.market.low_24h,
        "structure": {
            "trend": ctx.structure.trend,
            "supports": ctx.structure.supports,
            "resistances": ctx.structure.resistances,
            "key_pivot": ctx.structure.key_pivot,
            "recent_high": ctx.structure.recent_high,
            "recent_low": ctx.structure.recent_low,
        },
        "fib": {
            "high": ctx.fib.high,
            "low": ctx.fib.low,
            "range": ctx.fib.range,
            "retr_500": ctx.fib.retr_500,
            "retr_618": ctx.fib.retr_618,
            "retr_786": ctx.fib.retr_786,
            "ext_1272": ctx.fib.ext_1272,
            "rebound_382": ctx.fib.rebound_382,
            "rebound_618": ctx.fib.rebound_618,
        },
        "jack_levels": ctx.jack.to_dict() if ctx.jack else None,
        "indicators": ctx.indicators.get(tf) or next(iter(ctx.indicators.values()), None),
        "baseline_plan": asdict(baseline),
    }
    if latency_ms is not None:
        out["latency_ms"] = latency_ms
    return out


def run_ai_job(session_id: int) -> None:
    """后台任务：用已存快照调 LLM。由 FastAPI BackgroundTasks 调用。"""
    import logging

    from analyst.llm.analyst import analyze_market
    from analyst.storage.models import AIPlan

    log = logging.getLogger(__name__)

    s = repo.get_session(session_id)
    if not s:
        return
    if repo.get_ai_plan(session_id):
        return

    repo.update_session_status(session_id, "ai_running")
    repo.set_session_ai_error(session_id, None)

    market_dict = dict(s.market_snapshot or {})
    market_dict["primary_timeframe"] = s.timeframe
    try:
        ai_response = analyze_market(
            market_snapshot=market_dict,
            indicators_snapshot=s.indicators_snapshot or {},
        )
        repo.save_ai_plan(
            AIPlan(
                session_id=session_id,
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
        repo.update_session_status(session_id, "ai_planned")
        repo.set_session_ai_error(session_id, None)
    except Exception as e:
        log.exception("run_ai_job failed session=%s", session_id)
        repo.update_session_status(session_id, "ai_failed")
        repo.set_session_ai_error(session_id, str(e)[:2000])


def start_async_analysis(
    symbol: str,
    timeframe: str | None,
    verify_after_hours: int | None,
    market: str = "spot",
) -> dict:
    """Web：创建会话，立即返回 DTO（仅 AI 计划；后台 run_ai_job）。"""
    from analyst.web.dto import session_to_dto

    sym = symbol if "/" in symbol else f"{symbol.upper()}/USDT"
    ctx = create_session(
        sym, timeframe, verify_after_hours=verify_after_hours, market=market
    )

    refreshed = repo.get_session(ctx.db_session.id)
    return session_to_dto(refreshed, market_extras=_ctx_to_market_extras(ctx))


def run_practice_quick(
    symbol: str,
    timeframe: str | None = None,
    verify_after_hours: int | None = None,
) -> dict:
    """完整的 quick 模式训练流程（拉数据 → 算指标 → AI 分析 → 落库）。

    供 CLI 和 Web 复用。返回 `_session_to_dto` 形式的字典。

    Args:
        symbol: 币种（如 "BTC" 或 "BTC/USDT"）
        timeframe: K 线周期（如 "4h"），决定分析视角
        verify_after_hours: 多久后可以验证。None 时按 timeframe 智能默认。
    """
    from analyst.llm.analyst import analyze_market
    from analyst.storage import repo
    from analyst.storage.models import AIPlan
    from analyst.web.dto import session_to_dto

    sym = symbol if "/" in symbol else f"{symbol.upper()}/USDT"

    ctx = create_session(sym, timeframe, verify_after_hours=verify_after_hours)

    market_dict = ctx.market.to_dict()
    market_dict["primary_timeframe"] = ctx.db_session.timeframe

    try:
        ai_response = analyze_market(
            market_snapshot=market_dict,
            indicators_snapshot=ctx.indicators,
        )
    except Exception:
        repo.delete_session(ctx.db_session.id)
        raise

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

    refreshed = repo.get_session(ctx.db_session.id)
    extras = _ctx_to_market_extras(ctx, latency_ms=ai_response.latency_ms)
    return session_to_dto(refreshed, market_extras=extras)


def map_monitor_tf_to_ai_tf(timeframe: str) -> str:
    """盯盘周期 → AI 快照允许周期（create_session 仅 1d/4h/1h/30m）。"""
    tf = (timeframe or "").strip().lower()
    if tf == "15m":
        return "1h"
    if tf in ("1d", "4h", "1h", "30m"):
        return tf
    return "4h"


def run_monitor_ai_confirm(
    symbol: str,
    timeframe: str,
    *,
    market: str = "futures",
    trigger_rules: list[str] | None = None,
) -> dict:
    """盯盘候选确认：拉快照 → LLM → 落库。返回精简结果供告警用。

    Returns:
        {
          "direction": str,
          "plan": dict | None,
          "session_id": int,
          "model_id": str | None,
          "ai_timeframe": str,
          "price": float | None,
          "rationale": str,
        }
    """
    from analyst.config import get_settings
    from analyst.llm.analyst import analyze_market
    from analyst.storage import repo
    from analyst.storage.models import AIPlan

    settings = get_settings()
    free_only = bool(getattr(settings, "monitor_ai_free_only", True))

    sym = symbol if "/" in symbol else f"{symbol.upper()}/USDT"
    ai_tf = map_monitor_tf_to_ai_tf(timeframe)
    ctx = create_session(sym, ai_tf, verify_after_hours=None, market=market)

    market_dict = dict(ctx.db_session.market_snapshot or {})
    if not market_dict:
        market_dict = ctx.market.to_dict()
    market_dict["primary_timeframe"] = ctx.db_session.timeframe
    if trigger_rules:
        market_dict["trigger_rules"] = list(trigger_rules)[:8]
    # jack_levels 在 create_session 里写入 DB 快照
    if "jack_levels" not in market_dict and isinstance(ctx.db_session.market_snapshot, dict):
        jl = ctx.db_session.market_snapshot.get("jack_levels")
        if jl:
            market_dict["jack_levels"] = jl

    try:
        ai_response = analyze_market(
            market_snapshot=market_dict,
            indicators_snapshot=ctx.indicators,
            free_only=free_only,
        )
    except Exception:
        repo.delete_session(ctx.db_session.id)
        raise

    plan = ai_response.plan
    repo.save_ai_plan(
        AIPlan(
            session_id=ctx.db_session.id,
            direction=plan.direction,
            entry_low=plan.entry_low,
            entry_high=plan.entry_high,
            stop_loss=plan.stop_loss,
            take_profit_1=plan.take_profit_1,
            take_profit_2=plan.take_profit_2,
            confidence=3,
            rationale=plan.rationale,
            rr_ratio=plan.rr_ratio,
            raw_response=ai_response.raw_text,
            prompt_version=ai_response.prompt_version,
            model_id=ai_response.model,
            cost_usd=ai_response.cost_usd,
        )
    )
    repo.update_session_status(ctx.db_session.id, "ai_planned")

    plan_dict = {
        "direction": plan.direction,
        "entry_low": plan.entry_low,
        "entry_high": plan.entry_high,
        "stop_loss": plan.stop_loss,
        "take_profit_1": plan.take_profit_1,
        "take_profit_2": plan.take_profit_2,
        "rr_ratio": plan.rr_ratio,
        "rationale": plan.rationale,
    }
    return {
        "direction": plan.direction,
        "plan": plan_dict,
        "session_id": ctx.db_session.id,
        "model_id": ai_response.model,
        "ai_timeframe": ai_tf,
        "price": getattr(ctx.market, "current_price", None),
        "rationale": plan.rationale or "",
    }


def recompute_market_extras_from_db(s: Session, latency_ms: int | None = None) -> dict:
    """从 DB 里存的 market_snapshot 重组结构/斐波（补跑 AI 时用）。"""
    from dataclasses import asdict
    from datetime import datetime
    from typing import Any

    from analyst.compute.fibonacci import compute_fib
    from analyst.compute.jack_levels import JackLevels, compute_jack_levels
    from analyst.compute.plan import generate_baseline_plan
    from analyst.compute.structure import detect_structure
    from analyst.data.fetcher import Candle, CandleSeries

    ms = s.market_snapshot or {}
    tf = s.timeframe
    tfd = (ms.get("timeframes") or {}).get(tf)
    indicators_all = s.indicators_snapshot or {}
    base: dict[str, Any] = {
        "current_price": ms.get("current_price"),
        "high_24h": ms.get("high_24h"),
        "low_24h": ms.get("low_24h"),
        "indicators": indicators_all.get(tf) or next(iter(indicators_all.values()), None),
        "jack_levels": ms.get("jack_levels"),
    }
    if latency_ms is not None:
        base["latency_ms"] = latency_ms
    if not tfd or not tfd.get("candles"):
        return base

    candles: list[Candle] = []
    for c in tfd["candles"]:
        ts_str = str(c["timestamp"]).replace("Z", "").split("+")[0]
        ts = datetime.fromisoformat(ts_str)
        candles.append(
            Candle(
                timestamp=ts,
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                volume=float(c.get("volume") or 0),
            )
        )
    sym = tfd.get("symbol") or s.symbol
    series = CandleSeries(symbol=sym, timeframe=tf, candles=candles)
    structure = detect_structure(series)
    fib = compute_fib(structure.recent_high, structure.recent_low)
    price = float(base["current_price"] or candles[-1].close)

    jack = None
    raw_jack = ms.get("jack_levels")
    if isinstance(raw_jack, dict) and raw_jack.get("swing_high") is not None:
        try:
            jack = JackLevels(**{k: raw_jack[k] for k in JackLevels.__dataclass_fields__ if k in raw_jack})
        except TypeError:
            jack = None
    if jack is None:
        jack = compute_jack_levels(
            current_price=price,
            structure=structure,
            fib=fib,
            daily_indicators=indicators_all.get("1d"),
            primary_series=series,
            symbol=s.symbol or "",
        )
        base["jack_levels"] = jack.to_dict()

    baseline = generate_baseline_plan(price, fib, structure, jack=jack)
    base["structure"] = {
        "trend": structure.trend,
        "supports": structure.supports,
        "resistances": structure.resistances,
        "key_pivot": structure.key_pivot,
        "recent_high": structure.recent_high,
        "recent_low": structure.recent_low,
    }
    base["fib"] = {
        "high": fib.high,
        "low": fib.low,
        "range": fib.range,
        "retr_500": fib.retr_500,
        "retr_618": fib.retr_618,
        "retr_786": fib.retr_786,
        "ext_1272": fib.ext_1272,
        "rebound_382": fib.rebound_382,
        "rebound_618": fib.rebound_618,
    }
    base["baseline_plan"] = asdict(baseline)
    return base


def retry_quick_session_ai(session_id: int) -> dict:
    """对「尚无 AI 计划」的会话补跑 LLM（用当时快照，不重新拉盘）。

    已存在 ai_plan 时直接返回当前 DTO（幂等，避免重复扣费）。
    """
    from analyst.llm.analyst import analyze_market
    from analyst.storage import repo
    from analyst.storage.models import AIPlan
    from analyst.web.dto import session_to_dto

    s = repo.get_session(session_id)
    if not s:
        raise ValueError(f"会话 #{session_id} 不存在")

    if repo.get_ai_plan(session_id):
        refreshed = repo.get_session(session_id)
        extras = recompute_market_extras_from_db(refreshed)
        return session_to_dto(refreshed, market_extras=extras)

    if s.status == "ai_running":
        raise ValueError("AI 正在生成中，请稍候或刷新页面")

    if s.status not in ("created", "user_planned", "ai_failed", "ai_planned"):
        raise ValueError(
            f'该会话状态为「{s.status}」，无法补跑'
        )

    market_dict = dict(s.market_snapshot or {})
    market_dict["primary_timeframe"] = s.timeframe

    ai_response = analyze_market(
        market_snapshot=market_dict,
        indicators_snapshot=s.indicators_snapshot or {},
    )

    repo.save_ai_plan(
        AIPlan(
            session_id=session_id,
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
    repo.update_session_status(session_id, "ai_planned")

    refreshed = repo.get_session(session_id)
    repo.set_session_ai_error(session_id, None)
    extras = recompute_market_extras_from_db(refreshed, latency_ms=ai_response.latency_ms)
    return session_to_dto(refreshed, market_extras=extras)


def _serialize_indicators(snap: ind.IndicatorSnapshot) -> dict:
    """指标对象转 dict（去掉嵌套的 series 数据）。"""
    return {
        "timeframe": snap.timeframe,
        "macd": {
            "dif": snap.macd.dif,
            "dea": snap.macd.dea,
            "histogram": snap.macd.histogram,
            "above_zero": snap.macd.above_zero,
            "cross_signal": snap.macd.cross_signal,
        },
        "ema": {
            "ema7": snap.ema.ema7,
            "ema30": snap.ema.ema30,
            "ema52": snap.ema.ema52,
        },
        "boll": {
            "upper": snap.boll.upper,
            "middle": snap.boll.middle,
            "lower": snap.boll.lower,
            "width": snap.boll.width,
        },
    }
