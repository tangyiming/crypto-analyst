"""市场日程 API。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from analyst.compute.market_schedule import build_schedule_payload
from analyst.config import get_settings
from analyst.monitor.hub import _fetch_premium_index, get_monitor_hub

router = APIRouter(tags=["schedule"])


def _macro_filters() -> tuple[set[str], set[str]]:
    s = get_settings()
    cur_raw = (getattr(s, "monitor_schedule_macro_currencies", "USD") or "USD").strip()
    imp_raw = (getattr(s, "monitor_schedule_macro_impacts", "High") or "High").strip()
    currencies = {x.strip().upper() for x in cur_raw.split(",") if x.strip()} or {"USD"}
    impacts = {
        x.strip().capitalize() for x in imp_raw.split(",") if x.strip()
    } or {"High"}
    return currencies, impacts


@router.get("/api/schedule")
def schedule_status(
    symbol: str = Query("BTC/USDT", description="资金费参考品种"),
    tz: str = Query("", description="本地 IANA 时区，如 Asia/Dubai"),
    refresh: bool = Query(False, description="强制刷新宏观日历缓存"),
):
    """时段 · 时钟 · 资金费 · 宏观日历一览。"""
    hub = get_monitor_hub()
    sym = (symbol or "BTC/USDT").upper().replace("-", "/")
    if "/" not in sym:
        sym = f"{sym}/USDT" if not sym.endswith("USDT") else f"{sym[:-4]}/USDT"

    premium = None
    for w in hub._workers.values():
        if w.key.symbol.upper() == sym and w.last_premium:
            premium = w.last_premium
            break
    if premium is None:
        premium = _fetch_premium_index(sym)

    currencies, impacts = _macro_filters()
    local_tz = (tz or "").strip() or "Asia/Dubai"
    payload = build_schedule_payload(
        premium=premium,
        funding_symbol=sym,
        currencies=currencies,
        impacts=impacts,
        force_macro=refresh,
        local_tz=local_tz,
    )
    s = get_settings()
    payload["settings"] = {
        "enabled": bool(getattr(s, "monitor_schedule_enabled", True)),
        "tg": bool(getattr(s, "monitor_schedule_tg", True)),
        "session_leads": getattr(s, "monitor_schedule_session_leads", "30,15"),
        "funding_leads": getattr(s, "monitor_schedule_funding_leads", "30"),
        "macro_leads": getattr(s, "monitor_schedule_macro_leads", "60,30,15"),
        "macro_currencies": getattr(s, "monitor_schedule_macro_currencies", "USD"),
        "macro_impacts": getattr(s, "monitor_schedule_macro_impacts", "High"),
    }
    return payload
