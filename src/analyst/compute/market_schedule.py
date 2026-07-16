"""市场日程：交易时段窗口 · 多时区时钟 · 宏观日历（Forex Factory）。

时段按加密常见流动性切换定义（UTC），面向「谁刚起床/谁刚下班」而非传统股市开盘铃。
宏观事件默认只保留 USD High 影响（FOMC / CPI / NFP 等）。
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

FF_WEEK_JSON = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# 固定时钟（展示用）
CLOCK_ZONES: tuple[tuple[str, str, str], ...] = (
    ("beijing", "北京", "Asia/Shanghai"),
    ("dubai", "迪拜", "Asia/Dubai"),
    ("london", "伦敦", "Europe/London"),
    ("newyork", "纽约", "America/New_York"),
)

# 流动性窗口（UTC 时:分）；周末仍显示，但提醒可跳过周末
SESSION_WINDOWS: tuple[dict[str, Any], ...] = (
    {
        "id": "asia_am",
        "name": "亚盘活跃",
        "hint": "中韩早盘，常有扫流动性 / 假突破",
        "start_utc": (0, 0),
        "end_utc": (3, 0),
        "skip_weekend": False,
    },
    {
        "id": "eu_open",
        "name": "欧盘开盘",
        "hint": "伦敦接棒，波动常抬升",
        "start_utc": (7, 0),
        "end_utc": (9, 0),
        "skip_weekend": True,
    },
    {
        "id": "us_open",
        "name": "美盘开盘",
        "hint": "纽约接棒，加密常跟风险资产共振",
        "start_utc": (13, 0),
        "end_utc": (15, 0),
        "skip_weekend": True,
    },
)


@dataclass(frozen=True)
class SessionOccurrence:
    id: str
    name: str
    hint: str
    start: datetime
    end: datetime

    @property
    def active(self) -> bool:
        now = datetime.now(timezone.utc)
        return self.start <= now < self.end


def _parse_hhmm(pair: tuple[int, int]) -> tuple[int, int]:
    return int(pair[0]), int(pair[1])


def _at_utc(day: date, hm: tuple[int, int]) -> datetime:
    h, m = _parse_hhmm(hm)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)


def _iter_session_occurrences(
    now: datetime | None = None,
    *,
    days_ahead: int = 3,
) -> list[SessionOccurrence]:
    """生成 now 附近的时段实例（含正在进行 + 未来几天）。"""
    now = now or datetime.now(timezone.utc)
    start_day = (now - timedelta(days=1)).date()
    out: list[SessionOccurrence] = []
    for i in range(days_ahead + 2):
        day = start_day + timedelta(days=i)
        weekday = day.weekday()  # 0=Mon
        is_weekend = weekday >= 5
        for win in SESSION_WINDOWS:
            if win.get("skip_weekend") and is_weekend:
                continue
            start = _at_utc(day, win["start_utc"])
            end = _at_utc(day, win["end_utc"])
            if end <= start:
                end += timedelta(days=1)
            out.append(
                SessionOccurrence(
                    id=str(win["id"]),
                    name=str(win["name"]),
                    hint=str(win.get("hint") or ""),
                    start=start,
                    end=end,
                )
            )
    out.sort(key=lambda x: x.start)
    return out


def resolve_tz(name: str | None, fallback: str = "Asia/Dubai") -> ZoneInfo:
    """解析 IANA 时区；非法则回退。"""
    for cand in ((name or "").strip(), fallback, "UTC"):
        if not cand:
            continue
        try:
            return ZoneInfo(cand)
        except Exception:
            continue
    return ZoneInfo("UTC")


def format_in_tz(dt: datetime, tz: ZoneInfo, fmt: str = "%m-%d %H:%M") -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime(fmt)


def build_clocks(
    now: datetime | None = None,
    *,
    local_tz: str | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 本地时区置顶（浏览器传入，如 Asia/Dubai）
    if local_tz:
        try:
            tz = ZoneInfo(local_tz)
            local = now.astimezone(tz)
            off = local.utcoffset() or timedelta(0)
            total_min = int(off.total_seconds() // 60)
            sign = "+" if total_min >= 0 else "-"
            abs_m = abs(total_min)
            rows.append(
                {
                    "id": "local",
                    "label": "本地",
                    "tz": local_tz,
                    "time": local.strftime("%H:%M:%S"),
                    "date": local.strftime("%m-%d"),
                    "offset": f"UTC{sign}{abs_m // 60:02d}:{abs_m % 60:02d}",
                    "weekday": "一二三四五六日"[local.weekday()],
                    "is_local": True,
                }
            )
            seen.add(local_tz)
        except Exception:
            pass

    for cid, label, tz_name in CLOCK_ZONES:
        if tz_name in seen:
            continue
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            continue
        local = now.astimezone(tz)
        off = local.utcoffset() or timedelta(0)
        total_min = int(off.total_seconds() // 60)
        sign = "+" if total_min >= 0 else "-"
        abs_m = abs(total_min)
        rows.append(
            {
                "id": cid,
                "label": label,
                "tz": tz_name,
                "time": local.strftime("%H:%M:%S"),
                "date": local.strftime("%m-%d"),
                "offset": f"UTC{sign}{abs_m // 60:02d}:{abs_m % 60:02d}",
                "weekday": "一二三四五六日"[local.weekday()],
                "is_local": False,
            }
        )
        seen.add(tz_name)
        if len(rows) >= 5:
            break
    return rows


def build_sessions(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    occs = _iter_session_occurrences(now)
    active = [o for o in occs if o.start <= now < o.end]
    upcoming = next((o for o in occs if o.start > now), None)
    windows: list[dict[str, Any]] = []
    for o in occs:
        if o.end < now - timedelta(hours=1):
            continue
        if o.start > now + timedelta(days=2):
            break
        windows.append(
            {
                "id": o.id,
                "name": o.name,
                "hint": o.hint,
                "start": o.start.isoformat(),
                "end": o.end.isoformat(),
                "active": o.start <= now < o.end,
                "starts_in_sec": max(0, int((o.start - now).total_seconds())),
                "ends_in_sec": max(0, int((o.end - now).total_seconds()))
                if o.start <= now < o.end
                else None,
            }
        )
    return {
        "active": [
            {
                "id": o.id,
                "name": o.name,
                "hint": o.hint,
                "start": o.start.isoformat(),
                "end": o.end.isoformat(),
                "ends_in_sec": max(0, int((o.end - now).total_seconds())),
            }
            for o in active
        ],
        "upcoming": (
            {
                "id": upcoming.id,
                "name": upcoming.name,
                "hint": upcoming.hint,
                "start": upcoming.start.isoformat(),
                "end": upcoming.end.isoformat(),
                "starts_in_sec": max(0, int((upcoming.start - now).total_seconds())),
            }
            if upcoming
            else None
        ),
        "windows": windows,
    }


def session_lead_candidates(
    lead_minutes: list[int],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """返回此刻应触发的「时段前提醒」候选（调用方做去重）。"""
    now = now or datetime.now(timezone.utc)
    leads = sorted({int(x) for x in lead_minutes if int(x) > 0}, reverse=True)
    out: list[dict[str, Any]] = []
    for o in _iter_session_occurrences(now, days_ahead=2):
        if o.start <= now:
            continue
        secs = (o.start - now).total_seconds()
        for lead in leads:
            # 落在 lead±45s 窗口内触发（配合 30–60s 轮询）
            target = lead * 60
            if abs(secs - target) <= 45:
                out.append(
                    {
                        "key": f"session|{o.id}|{o.start.date().isoformat()}|{lead}",
                        "kind": "session",
                        "lead_min": lead,
                        "title": o.name,
                        "hint": o.hint,
                        "at": o.start.isoformat(),
                        "text": (
                            f"⏱ 还有 {lead} 分钟进入「{o.name}」\n"
                            f"{o.hint}\n"
                            f"开始 {o.start.astimezone(ZoneInfo('Asia/Dubai')):%H:%M} 迪拜"
                            f" / {o.start.astimezone(ZoneInfo('Asia/Shanghai')):%H:%M} 北京"
                        ),
                    }
                )
    return out


_macro_cache: dict[str, Any] = {"fetched_at": 0.0, "events": []}


def _parse_ff_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def fetch_macro_events(
    *,
    currencies: set[str] | None = None,
    impacts: set[str] | None = None,
    force: bool = False,
    cache_ttl_sec: float = 1800.0,
) -> list[dict[str, Any]]:
    """拉取本周 FF 日历并过滤。结果带短缓存。"""
    import time

    currencies = {c.upper() for c in (currencies or {"USD"})}
    impacts = {i.capitalize() for i in (impacts or {"High"})}
    now_ts = time.time()
    if (
        not force
        and _macro_cache["events"]
        and now_ts - float(_macro_cache["fetched_at"]) < cache_ttl_sec
    ):
        return list(_macro_cache["events"])

    try:
        req = urllib.request.Request(
            FF_WEEK_JSON,
            headers={"User-Agent": "crypto-analyst/1.0"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = json.loads(resp.read().decode())
    except Exception as e:
        logger.warning("FF calendar fetch failed: %s", e)
        return list(_macro_cache["events"] or [])

    if not isinstance(raw, list):
        return []

    events: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        country = str(item.get("country") or "").upper()
        impact = str(item.get("impact") or "").capitalize()
        if currencies and country not in currencies:
            continue
        if impacts and impact not in impacts:
            continue
        when = _parse_ff_date(str(item.get("date") or ""))
        if when is None:
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        events.append(
            {
                "title": title,
                "country": country,
                "impact": impact,
                "at": when.isoformat(),
                "forecast": str(item.get("forecast") or ""),
                "previous": str(item.get("previous") or ""),
                "actual": str(item.get("actual") or ""),
            }
        )
    events.sort(key=lambda e: e["at"])
    _macro_cache["fetched_at"] = now_ts
    _macro_cache["events"] = events
    return list(events)


def build_macro(
    *,
    currencies: set[str] | None = None,
    impacts: set[str] | None = None,
    now: datetime | None = None,
    force: bool = False,
    local_tz: str | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    tz = resolve_tz(local_tz, "Asia/Dubai")
    events = fetch_macro_events(
        currencies=currencies, impacts=impacts, force=force
    )
    upcoming = []
    past = []
    for e in events:
        at = _parse_ff_date(e["at"])
        if at is None:
            continue
        row = {
            **e,
            "starts_in_sec": int((at - now).total_seconds()),
            "local_time": format_in_tz(at, tz, "%m-%d %H:%M"),
            "beijing": format_in_tz(at, ZoneInfo("Asia/Shanghai"), "%m-%d %H:%M"),
        }
        if at >= now - timedelta(minutes=5):
            upcoming.append(row)
        else:
            past.append(row)
    next_high = upcoming[0] if upcoming else None
    return {
        "source": "forexfactory",
        "local_tz": str(getattr(tz, "key", local_tz) or "Asia/Dubai"),
        "updated_at": datetime.fromtimestamp(
            float(_macro_cache["fetched_at"] or 0), tz=timezone.utc
        ).isoformat()
        if _macro_cache["fetched_at"]
        else None,
        "next": next_high,
        "upcoming": upcoming[:20],
        "recent": past[-5:][::-1],
        "count": len(upcoming),
    }


def macro_lead_candidates(
    lead_minutes: list[int],
    *,
    currencies: set[str] | None = None,
    impacts: set[str] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    leads = sorted({int(x) for x in lead_minutes if int(x) > 0}, reverse=True)
    events = fetch_macro_events(currencies=currencies, impacts=impacts)
    out: list[dict[str, Any]] = []
    for e in events:
        at = _parse_ff_date(e["at"])
        if at is None or at <= now:
            continue
        secs = (at - now).total_seconds()
        for lead in leads:
            if abs(secs - lead * 60) <= 45:
                bj = at.astimezone(ZoneInfo("Asia/Shanghai"))
                dxb = at.astimezone(ZoneInfo("Asia/Dubai"))
                out.append(
                    {
                        "key": f"macro|{e['country']}|{e['title']}|{at.date().isoformat()}|{lead}",
                        "kind": "macro",
                        "lead_min": lead,
                        "title": e["title"],
                        "at": at.isoformat(),
                        "text": (
                            f"📰 还有 {lead} 分钟 · {e['impact']} 影响\n"
                            f"{e['country']} {e['title']}\n"
                            f"{dxb:%m-%d %H:%M} 迪拜 / {bj:%m-%d %H:%M} 北京"
                            + (
                                f"\n预期 {e['forecast']} · 前值 {e['previous']}"
                                if e.get("forecast") or e.get("previous")
                                else ""
                            )
                        ),
                    }
                )
    return out


def funding_snapshot(
    premium: dict[str, Any] | None,
    *,
    symbol: str = "BTC/USDT",
    now: datetime | None = None,
    local_tz: str | None = None,
) -> dict[str, Any] | None:
    """从 premiumIndex 字段构造资金费倒计时。"""
    if not premium:
        return None
    nft = premium.get("next_funding_time")
    if nft is None:
        return None
    now = now or datetime.now(timezone.utc)
    tz = resolve_tz(local_tz, "Asia/Dubai")
    try:
        ms = int(nft)
    except (TypeError, ValueError):
        return None
    at = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    rate = premium.get("funding_rate")
    rate_pct = float(rate) * 100.0 if rate is not None else None
    return {
        "symbol": symbol,
        "next_funding_time": ms,
        "at": at.isoformat(),
        "local_time": format_in_tz(at, tz, "%H:%M:%S"),
        "beijing": format_in_tz(at, ZoneInfo("Asia/Shanghai"), "%H:%M:%S"),
        "seconds_to_funding": int((at - now).total_seconds()),
        "funding_rate": rate,
        "funding_rate_pct": round(rate_pct, 4) if rate_pct is not None else None,
    }


def funding_lead_candidates(
    premium: dict[str, Any] | None,
    lead_minutes: list[int],
    *,
    symbol: str = "BTC/USDT",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    snap = funding_snapshot(premium, symbol=symbol, now=now)
    if not snap:
        return []
    now = now or datetime.now(timezone.utc)
    secs = int(snap["seconds_to_funding"])
    if secs < 0:
        return []
    leads = sorted({int(x) for x in lead_minutes if int(x) > 0}, reverse=True)
    at = datetime.fromisoformat(snap["at"])
    out: list[dict[str, Any]] = []
    for lead in leads:
        if abs(secs - lead * 60) <= 45:
            rate_txt = (
                f"{snap['funding_rate_pct']:+.4f}%/8h"
                if snap.get("funding_rate_pct") is not None
                else "—"
            )
            local_t = snap.get("local_time") or snap.get("beijing") or "—"
            out.append(
                {
                    "key": f"funding|{symbol}|{at.isoformat()}|{lead}",
                    "kind": "funding",
                    "lead_min": lead,
                    "title": "资金费结算",
                    "at": snap["at"],
                    "text": (
                        f"💸 还有 {lead} 分钟资金费结算 · {symbol}\n"
                        f"费率 {rate_txt} · 本地 {local_t}"
                    ),
                }
            )
    return out


def build_schedule_payload(
    *,
    premium: dict[str, Any] | None = None,
    funding_symbol: str = "BTC/USDT",
    currencies: set[str] | None = None,
    impacts: set[str] | None = None,
    force_macro: bool = False,
    local_tz: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    tz = resolve_tz(local_tz, "Asia/Dubai")
    tz_key = str(getattr(tz, "key", None) or local_tz or "Asia/Dubai")
    return {
        "now_utc": now.isoformat(),
        "now_local": format_in_tz(now, tz, "%Y-%m-%d %H:%M:%S"),
        "now_beijing": format_in_tz(now, ZoneInfo("Asia/Shanghai"), "%Y-%m-%d %H:%M:%S"),
        "local_tz": tz_key,
        "clocks": build_clocks(now, local_tz=tz_key),
        "sessions": build_sessions(now),
        "funding": funding_snapshot(
            premium, symbol=funding_symbol, now=now, local_tz=tz_key
        ),
        "macro": build_macro(
            currencies=currencies,
            impacts=impacts,
            now=now,
            force=force_macro,
            local_tz=tz_key,
        ),
    }
