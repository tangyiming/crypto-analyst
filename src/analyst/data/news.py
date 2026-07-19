"""新闻源抓取：RSS（stdlib 解析）+ 币安公告接口。

只做「取回 + 归一化」，分级判断在 monitor/news_sentinel。
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

BINANCE_ANN_URL = (
    "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query"
)
_UA = {"User-Agent": "Mozilla/5.0 (crypto-analyst news sentinel)"}


@dataclass(frozen=True)
class NewsItem:
    id: str          # 去重键（链接或公告 code）
    title: str
    source: str
    url: str
    published: str   # ISO 或原始字符串（尽力而为）


def _parse_rss(text: str, source: str) -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        logger.warning("RSS 解析失败 %s：%s", source, e)
        return items
    # RSS 2.0: channel/item；Atom: entry
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = link = guid = pub = ""
        for child in node:
            ctag = child.tag.rsplit("}", 1)[-1]
            if ctag == "title":
                title = (child.text or "").strip()
            elif ctag == "link":
                link = (child.text or child.get("href") or "").strip()
            elif ctag in ("guid", "id"):
                guid = (child.text or "").strip()
            elif ctag in ("pubDate", "published", "updated"):
                pub = (child.text or "").strip()
        if not title:
            continue
        items.append(
            NewsItem(
                id=guid or link or title,
                title=title,
                source=source,
                url=link,
                published=pub,
            )
        )
    return items


def fetch_rss(url: str, timeout: int = 10) -> list[NewsItem]:
    source = url.split("/")[2] if "://" in url else url
    try:
        r = requests.get(url, headers=_UA, timeout=timeout)
        r.raise_for_status()
        return _parse_rss(r.text, source)
    except Exception as e:
        logger.warning("RSS 拉取失败 %s：%s", url, e)
        return []


def fetch_binance_announcements(timeout: int = 10) -> list[NewsItem]:
    """币安官方公告（下架/维护/风险提示等，对持仓风险最直接）。"""
    try:
        r = requests.get(
            BINANCE_ANN_URL,
            params={"type": 1, "pageNo": 1, "pageSize": 20},
            headers=_UA,
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        arts = (
            (data.get("data") or {}).get("catalogs") or []
        )
        items: list[NewsItem] = []
        for cat in arts:
            for a in cat.get("articles") or []:
                code = str(a.get("code") or "")
                title = str(a.get("title") or "").strip()
                if not title:
                    continue
                ts = a.get("releaseDate")
                pub = (
                    datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
                    if isinstance(ts, (int, float))
                    else ""
                )
                items.append(
                    NewsItem(
                        id=f"binance:{code}",
                        title=title,
                        source="binance",
                        url=f"https://www.binance.com/en/support/announcement/{code}",
                        published=pub,
                    )
                )
        return items
    except Exception as e:
        logger.warning("币安公告拉取失败：%s", e)
        return []


def fetch_news(feeds: list[str]) -> list[NewsItem]:
    """按配置源抓全部条目（去重交给调用方）。"""
    out: list[NewsItem] = []
    for f in feeds:
        f = f.strip()
        if not f:
            continue
        if f.lower() == "binance":
            out.extend(fetch_binance_announcements())
        else:
            out.extend(fetch_rss(f))
    return out
