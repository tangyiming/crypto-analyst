#!/usr/bin/env python3
"""把第三方导出的 @jackli727 推文 JSON（列表，字段 id/created_at/full_text/media[].original …）
合并进 .cache/jack_tweets/（与官方 API 缓存同一 schema），按推文 id 去重、配图按 pbs 媒体 id 去重后下载。

用法（项目根）：
  .venv/bin/python scripts/import_jack_export.py ~/Downloads/twitter-用户推文-xxx.json [more.json ...]
  .venv/bin/python scripts/import_jack_export.py export.json --skip-media
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".cache" / "jack_tweets"
MEDIA = OUT / "media"
USERNAME = "jackli727"

_PBS_ID = re.compile(r"pbs\.twimg\.com/media/([A-Za-z0-9_-]+)")


def _pbs_id(url: str) -> str | None:
    m = _PBS_ID.search(url or "")
    return m.group(1) if m else None


def _to_utc_iso(s: str) -> str:
    """'2026-07-30 23:45:45 +04:00' → '2026-07-30T19:45:45.000Z'；已是 ISO 的原样返回。"""
    if not s:
        return s
    if s.endswith("Z") and "T" in s:
        return s
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return s
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _load_cache() -> dict:
    p = OUT / "tweets.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"fetched_at": None, "user": {"username": USERNAME}, "count": 0, "tweets": []}


def _existing_media_index() -> dict[str, str]:
    """pbs 媒体 id → 本地相对路径（含官方 API 缓存里按 url 推出的 id）。"""
    idx: dict[str, str] = {}
    if MEDIA.is_dir():
        for p in MEDIA.iterdir():
            # 文件名形如 {tweet}_{i}_{key}.jpg；key 可能是数字 media_key 或 pbs id
            stem = p.stem
            parts = stem.split("_", 2)
            if len(parts) == 3:
                idx[parts[2]] = str(p.relative_to(ROOT))
    return idx


def _normalize(item: dict) -> dict | None:
    if item.get("retweeted_status"):
        return None  # 转发别人的内容不算他的分析
    if str(item.get("screen_name") or "").lower() not in ("", USERNAME):
        return None
    text = item.get("full_text") or item.get("text") or ""
    media = []
    for i, m in enumerate(item.get("media") or []):
        if (m.get("type") or "photo") != "photo":
            continue
        url = m.get("original") or m.get("media_url_https") or m.get("thumbnail") or ""
        pid = _pbs_id(url)
        if not pid:
            continue
        media.append({"media_key": pid, "type": "photo", "url": url, "local_path": None})
    return {
        "id": str(item["id"]),
        "created_at": _to_utc_iso(item.get("created_at") or ""),
        "text": text,
        "note_tweet": {"text": text},
        "media": media,
        "attachments": {"media_keys": [m["media_key"] for m in media]} if media else {},
        "public_metrics": {
            "like_count": item.get("favorite_count"),
            "retweet_count": item.get("retweet_count"),
            "reply_count": item.get("reply_count"),
            "impression_count": item.get("views_count"),
        },
        "in_reply_to": item.get("in_reply_to"),
        "source": "export",
    }


def _download(tweets: list[dict], existing: dict[str, str]) -> tuple[int, int, int]:
    MEDIA.mkdir(parents=True, exist_ok=True)
    saved = reused = failed = 0
    with httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for t in tweets:
            for i, m in enumerate(t.get("media") or []):
                if m.get("local_path") and (ROOT / m["local_path"]).is_file():
                    continue
                pid = m["media_key"]
                if pid in existing:
                    m["local_path"] = existing[pid]
                    reused += 1
                    continue
                url = m["url"]
                ext = ".png" if "format=png" in url else ".jpg"
                path = MEDIA / f"{t['id']}_{i}_{pid}{ext}"
                try:
                    r = client.get(url)
                    r.raise_for_status()
                    path.write_bytes(r.content)
                    m["local_path"] = str(path.relative_to(ROOT))
                    existing[pid] = m["local_path"]
                    saved += 1
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    print(f"  下载失败 {url}: {e}", file=sys.stderr)
    return saved, reused, failed


def _write_md(tweets: list[dict]) -> None:
    lines = [f"# @{USERNAME} 推文缓存（官方 API + 导出合并）", "", f"- 条数：{len(tweets)}"]
    if tweets:
        lines.append(f"- 区间：{tweets[-1]['created_at']} → {tweets[0]['created_at']}")
    lines += ["", ""]
    for t in tweets:
        text = ((t.get("note_tweet") or {}).get("text") or t.get("text") or "").strip()
        lines.append(f"## {t['created_at']}")
        lines.append("")
        lines.append(text)
        for m in t.get("media") or []:
            if m.get("local_path"):
                lines.append(f"- 图：{m['local_path']}")
        lines += ["", "---", ""]
    (OUT / "tweets.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--skip-media", action="store_true")
    a = ap.parse_args()

    cache = _load_cache()
    by_id: dict[str, dict] = {str(t["id"]): t for t in cache.get("tweets") or []}
    before = len(by_id)
    added = dup = skipped = 0
    for f in a.files:
        items = json.loads(Path(f).expanduser().read_text(encoding="utf-8"))
        if isinstance(items, dict):
            items = items.get("data") or items.get("tweets") or []
        for it in items:
            norm = _normalize(it)
            if norm is None:
                skipped += 1
                continue
            if norm["id"] in by_id:
                dup += 1
                # 补齐缓存里缺的配图
                have = {m.get("media_key") for m in by_id[norm["id"]].get("media") or []}
                for m in norm["media"]:
                    if m["media_key"] not in have:
                        by_id[norm["id"]].setdefault("media", []).append(m)
                continue
            by_id[norm["id"]] = norm
            added += 1
    tweets = sorted(by_id.values(), key=lambda t: t["created_at"], reverse=True)
    print(f"合并：原缓存 {before} 条，新增 {added} 条，重复 {dup} 条，跳过转发/他人 {skipped} 条 → 共 {len(tweets)} 条")

    if not a.skip_media:
        existing = _existing_media_index()
        saved, reused, failed = _download(tweets, existing)
        print(f"配图：新下载 {saved} 张，复用已有 {reused} 张，失败 {failed} 张；目录 {MEDIA}")

    cache.update(
        fetched_at=datetime.now(timezone.utc).isoformat(),
        user=cache.get("user") or {"username": USERNAME},
        count=len(tweets),
        tweets=tweets,
    )
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tweets.json").write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    _write_md(tweets)
    print(f"已写入 {OUT / 'tweets.json'} 与 tweets.md")


if __name__ == "__main__":
    main()
