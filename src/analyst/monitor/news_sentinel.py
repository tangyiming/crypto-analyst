"""新闻事件风控哨兵：抓取 → 去重 → LLM 分级 → 高危推 TG。

分工：
  · LLM 只做「非结构化标题 → 结构化风险分级」的翻译，不决定任何交易动作
  · LLM 不可用时退回关键词兜底（宁可漏报不误报静默）
  · 高危事件的提示里会点名 carry 的交易所对手风险（delta 中性 ≠ 交易所免疫）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from analyst.config import get_settings
from analyst.data.news import NewsItem, fetch_news

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# 关键词兜底（LLM 线路全挂时仍能报最危险的事）
_CRITICAL_PAT = re.compile(
    r"hack|exploit|stolen|breach|insolven|bankrupt|halt(s|ed)? withdraw|"
    r"depeg|de-peg|emergency|SEC (sues|charges)|delist|暂停提现|被盗|破产|脱锚",
    re.IGNORECASE,
)

CLASSIFY_SYSTEM = (
    "你是加密交易系统的风险分级器。输入是一批新闻标题（JSON 数组，含 id/title/source）。"
    "对每条输出风险分级，只返回 JSON 数组，元素形如："
    '{"id": "...", "severity": "low|medium|high|critical", '
    '"category": "exchange|regulation|hack|depeg|macro|market|other", '
    '"affected": "受影响资产或交易所，无则空串", "reason": "≤20字理由"}。'
    "分级标准：critical=交易所暴雷/提现暂停/主流币被盗/稳定币脱锚；"
    "high=重大监管行动/主流资产被起诉/大所下架主流币；"
    "medium=行业负面但不直接影响持仓；low=常规新闻。"
    "宁可保守（降级），不要把普通新闻抬成 high。只输出 JSON。"
)


def _state_path() -> Path:
    s = get_settings()
    return Path(s.data_cache_dir) / "news_sentinel_seen.json"


def _load_seen() -> set[str]:
    try:
        p = _state_path()
        if p.is_file():
            return set(json.loads(p.read_text()) or [])
    except Exception:
        pass
    return set()


def _save_seen(seen: set[str]) -> None:
    try:
        _state_path().write_text(json.dumps(sorted(seen)[-500:]))
    except Exception:
        logger.exception("news seen save failed")


def _events_path() -> Path:
    s = get_settings()
    return Path(s.data_cache_dir) / "news_events.json"


def load_news_events(limit: int = 100) -> list[dict]:
    """读最近的分级事件（新的在前），供 Web 页面展示。"""
    try:
        p = _events_path()
        if p.is_file():
            rows = json.loads(p.read_text()) or []
            return list(reversed(rows))[: max(1, limit)]
    except Exception:
        logger.warning("news events load failed", exc_info=True)
    return []


def _append_events(rows: list[dict], keep: int = 200) -> None:
    """滚动追加事件记录（时间序，尾部最新），失败静默。"""
    if not rows:
        return
    try:
        p = _events_path()
        old = []
        if p.is_file():
            old = json.loads(p.read_text()) or []
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps((old + rows)[-keep:], ensure_ascii=False, default=str)
        )
    except Exception:
        logger.exception("news events save failed")


def keyword_fallback_classify(items: list[NewsItem]) -> list[dict]:
    """无 LLM 时的正则兜底：只报 critical 级关键词。"""
    out = []
    for it in items:
        if _CRITICAL_PAT.search(it.title):
            out.append({
                "id": it.id,
                "severity": "critical",
                "category": "keyword",
                "affected": "",
                "reason": "关键词命中（LLM 不可用）",
            })
    return out


def classify_news(items: list[NewsItem]) -> list[dict]:
    """LLM 批量分级；失败退回关键词兜底。"""
    if not items:
        return []
    settings = get_settings()
    payload = json.dumps(
        [{"id": i.id, "title": i.title, "source": i.source} for i in items],
        ensure_ascii=False,
    )
    from analyst.llm.chat import _iter_chat_clients

    for client, model, prov in _iter_chat_clients(settings):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": CLASSIFY_SYSTEM},
                    {"role": "user", "content": payload},
                ],
                temperature=0.1,
                max_tokens=1500,
            )
            text = (resp.choices[0].message.content or "").strip()
            m = re.search(r"\[.*\]", text, re.DOTALL)
            if not m:
                continue
            rows = json.loads(m.group(0))
            out = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                sev = str(r.get("severity", "low")).lower()
                if sev not in SEVERITY_ORDER:
                    continue
                out.append({
                    "id": str(r.get("id", "")),
                    "severity": sev,
                    "category": str(r.get("category", "other")),
                    "affected": str(r.get("affected", "")),
                    "reason": str(r.get("reason", "")),
                })
            logger.info("新闻分级 %d 条 via %s", len(out), prov)
            return out
        except Exception as e:
            logger.warning("新闻分级 %s 失败：%s", prov, e)
    return keyword_fallback_classify(items)


def _format_alert(item: NewsItem, cls: dict, has_carry: bool) -> str:
    icon = "🚨" if cls["severity"] == "critical" else "⚠️"
    lines = [
        f"{icon} 风险事件（{cls['severity'].upper()} · {cls.get('category')}）",
        item.title,
        f"来源 {item.source}" + (f" · 影响 {cls['affected']}" if cls.get("affected") else ""),
    ]
    if cls.get("reason"):
        lines.append(f"判定：{cls['reason']}")
    if has_carry and cls["severity"] == "critical":
        lines.append("💡 提示：carry 两腿在同一交易所，交易所级风险请考虑手动平 carry 降敞口")
    if item.url:
        lines.append(item.url)
    lines.append("（AI 分级仅供参考，不构成自动交易动作）")
    return "\n".join(lines)


async def run_news_sentinel_loop(
    notify: Callable[[str], Awaitable[None]],
) -> None:
    """轮询循环：新条目 → 分级 → ≥门槛推 TG。"""
    seen = _load_seen()
    first_pass = True
    while True:
        try:
            settings = get_settings()
            if not getattr(settings, "monitor_news_enabled", False):
                await asyncio.sleep(300)
                continue
            feeds = [
                f.strip()
                for f in (settings.monitor_news_feeds or "").split(",")
                if f.strip()
            ]
            min_sev = SEVERITY_ORDER.get(
                (settings.monitor_news_min_severity or "high").lower(), 2
            )
            items = await asyncio.to_thread(fetch_news, feeds)
            fresh = [i for i in items if i.id not in seen]
            for i in items:
                seen.add(i.id)
            _save_seen(seen)
            if first_pass:
                # 启动首轮只建立基线，不回放旧新闻（防重启刷屏）
                logger.info("news sentinel 基线 %d 条（首轮不推送）", len(items))
                first_pass = False
            elif fresh:
                cls_rows = await asyncio.to_thread(classify_news, fresh)
                by_id = {c["id"]: c for c in cls_rows}
                from analyst.trading.paper import get_paper_broker

                has_carry = bool(get_paper_broker().state.carry_book)
                pushed = 0
                now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
                events: list[dict] = []
                for item in fresh:
                    c = by_id.get(item.id)
                    hit = bool(
                        c and SEVERITY_ORDER.get(c["severity"], 0) >= min_sev
                    )
                    events.append({
                        "ts_utc": now_utc,
                        "title": item.title,
                        "source": item.source,
                        "url": item.url,
                        "severity": (c or {}).get("severity", "low"),
                        "category": (c or {}).get("category", "unrated"),
                        "affected": (c or {}).get("affected", ""),
                        "reason": (c or {}).get("reason", ""),
                        "pushed": hit,
                    })
                    if not hit:
                        continue
                    await notify(_format_alert(item, c, has_carry))
                    pushed += 1
                _append_events(events)
                logger.info(
                    "news sentinel 新 %d 条 · 推送 %d 条", len(fresh), pushed
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("news sentinel loop error")
        interval = max(5, int(get_settings().monitor_news_interval_min or 30))
        await asyncio.sleep(interval * 60)
