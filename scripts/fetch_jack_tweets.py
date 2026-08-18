#!/usr/bin/env python3
"""从 X/Twitter 官方 API 拉取 @jackli727 近期推文，落到本地缓存。

网页 https://x.com/jackli727 会 403 拦爬虫，不能当数据源。
需要的是 **X Developer Bearer Token**（不是 LLM key）。

申请：https://developer.x.com/en/portal/dashboard
  1. 建 Project + App
  2. Keys and tokens → Bearer Token
  3. 写入项目根 .env：X_BEARER_TOKEN=AAAA...

用法（项目根目录）：
  .venv/bin/python scripts/fetch_jack_tweets.py              # 默认读本地，不扣费
  .venv/bin/python scripts/fetch_jack_tweets.py --usage
  .venv/bin/python scripts/fetch_jack_tweets.py --refresh --max 150
  .venv/bin/python scripts/fetch_jack_tweets.py --include-replies

官方 API 有两层额度（不是「每天 N 条」）：
  1) Developer Console 预付 credits（没钱会 402 credits depleted）
  2) 项目月度 post-read cap（GET /2/usage/tweets，常见 300 万/月，按 cap_reset_day 重置）
读一条推文按 pay-per-use 计费；限速另算（用户时间线约 1 万次请求 / 15 分钟）。
Nitter RSS 没有官方日限额，但通常一页只有十几条近期帖。

产物（已 gitignore，只在本机 `.cache/`，不进 git）：
  .cache/jack_tweets/tweets.json   # 全文 + 配图 URL
  .cache/jack_tweets/tweets.md
  .cache/jack_tweets/media/        # K 线截图

默认读本地缓存，不打官方接口。要更新或往前多拉才加 --refresh（按条计费）：
  .venv/bin/python scripts/fetch_jack_tweets.py
  .venv/bin/python scripts/fetch_jack_tweets.py --refresh --max 150
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / ".cache" / "jack_tweets"
API_BASE = "https://api.x.com/2"
NITTER_RSS = [
    "https://nitter.net/{username}/rss",
    "https://nitter.poast.org/{username}/rss",
]
TWEET_FIELDS = ",".join(
    [
        "created_at",
        "public_metrics",
        "entities",
        "lang",
        "referenced_tweets",
        "note_tweet",
        "conversation_id",
        "attachments",
    ]
)
MEDIA_FIELDS = "media_key,type,url,preview_image_url,width,height,alt_text"


def _env_token() -> str:
    load_dotenv(ROOT / ".env")
    token = (os.getenv("X_BEARER_TOKEN") or os.getenv("TWITTER_BEARER_TOKEN") or "").strip()
    return unquote(token)


def fetch_via_nitter(username: str, *, max_tweets: int) -> tuple[dict, list[dict]]:
    """官方 API 额度用尽时，走公开 Nitter RSS。"""
    import re
    import xml.etree.ElementTree as ET
    from html import unescape
    from email.utils import parsedate_to_datetime

    last_err = ""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; crypto-analyst/1.0)"}
    with httpx.Client(timeout=40.0, headers=headers, follow_redirects=True) as client:
        for tmpl in NITTER_RSS:
            url = tmpl.format(username=username)
            r = client.get(url)
            if r.status_code != 200 or "<item>" not in r.text:
                last_err = f"{url} -> {r.status_code}"
                continue
            root = ET.fromstring(r.content)
            channel = root.find("channel")
            if channel is None:
                continue
            title = (channel.findtext("title") or username).split("/")[0].strip()
            user = {"id": "", "name": title, "username": username, "source": "nitter-rss"}
            tweets: list[dict] = []
            for item in channel.findall("item"):
                link = (item.findtext("link") or "").strip()
                m = re.search(r"/status(?:es)?/(\d+)", link)
                tid = m.group(1) if m else ""
                desc = unescape(item.findtext("description") or "")
                desc = re.sub(r"<br\s*/?>", "\n", desc, flags=re.I)
                desc = re.sub(r"<[^>]+>", "", desc).strip()
                pub = item.findtext("pubDate") or ""
                created = pub
                try:
                    created = parsedate_to_datetime(pub).astimezone(timezone.utc).isoformat()
                except (TypeError, ValueError):
                    pass
                tweets.append(
                    {
                        "id": tid,
                        "created_at": created,
                        "text": desc,
                        "public_metrics": {},
                    }
                )
                if len(tweets) >= max_tweets:
                    break
            if tweets:
                print(f"备用源：{url}（官方 API 额度不足）")
                return user, tweets
    raise SystemExit(f"Nitter RSS 也失败：{last_err}")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": "crypto-analyst-jack-tweets/1.0",
    }


def _print_rate_headers(r: httpx.Response, label: str) -> None:
    limit = r.headers.get("x-rate-limit-limit")
    remaining = r.headers.get("x-rate-limit-remaining")
    reset = r.headers.get("x-rate-limit-reset")
    if limit or remaining:
        print(f"限速 {label}：{remaining}/{limit} remaining，reset={reset}")


def fetch_usage(client: httpx.Client) -> dict:
    """月度 post-read cap。这不是 credits 余额；没充 credits 时这里经常是 0。"""
    r = client.get(
        f"{API_BASE}/usage/tweets",
        params={
            "days": 7,
            "usage.fields": "daily_project_usage,project_usage,project_cap,cap_reset_day",
        },
    )
    _print_rate_headers(r, "GET /2/usage/tweets")
    if r.status_code == 401:
        raise SystemExit("401：Bearer Token 无效或已过期，请到 X Developer Portal 重新生成。")
    r.raise_for_status()
    data = r.json().get("data") or {}
    cap = data.get("project_cap")
    used = data.get("project_usage")
    reset_day = data.get("cap_reset_day")
    print(
        f"Usage（本月 post-read cap）：{used} / {cap}，"
        f"每月 {reset_day} 日重置。"
        " 这只是月度条数上限，不是账户 credits。"
    )
    if str(used) == "0":
        print("usage=0 通常表示读推文接口还没成功扣过量（常见原因：credits depleted 先挡住了）。")
    return data


def lookup_user(client: httpx.Client, username: str) -> dict:
    r = client.get(
        f"{API_BASE}/users/by/username/{username}",
        params={"user.fields": "id,name,username,description,public_metrics"},
    )
    _print_rate_headers(r, "GET /2/users/by/username")
    if r.status_code == 402:
        raise httpx.HTTPStatusError(
            "credits depleted", request=r.request, response=r
        )
    if r.status_code == 401:
        raise SystemExit("401：Bearer Token 无效或已过期，请到 X Developer Portal 重新生成。")
    if r.status_code == 403:
        raise SystemExit(
            "403：当前 App 没有读推文权限。在 Developer Portal 把 App 权限改成 Read，"
            "并确认套餐包含 User Tweet timeline。"
        )
    r.raise_for_status()
    data = r.json()
    if "data" not in data:
        raise SystemExit(f"查用户失败：{json.dumps(data, ensure_ascii=False)[:400]}")
    return data["data"]


def _attach_media(batch: list[dict], includes: dict) -> None:
    media_by_key = {
        m["media_key"]: m
        for m in (includes.get("media") or [])
        if m.get("media_key")
    }
    for tweet in batch:
        keys = (tweet.get("attachments") or {}).get("media_keys") or []
        tweet["media"] = [media_by_key[k] for k in keys if k in media_by_key]


def _orig_media_url(url: str) -> str:
    if "pbs.twimg.com/media/" not in url:
        return url
    if "name=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "name=orig"


def download_media(tweets: list[dict]) -> int:
    """把配图下到 .cache/jack_tweets/media，路径写回 tweet.media[].local_path。"""
    media_dir = OUT_DIR / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    headers = {"User-Agent": "Mozilla/5.0 (compatible; crypto-analyst/1.0)"}
    with httpx.Client(timeout=40.0, headers=headers, follow_redirects=True) as client:
        for tweet in tweets:
            tid = tweet.get("id") or "unknown"
            for i, m in enumerate(tweet.get("media") or []):
                url = m.get("url") or m.get("preview_image_url")
                if not url:
                    continue
                url = _orig_media_url(url)
                key = (m.get("media_key") or f"{tid}_{i}").replace("/", "_")
                ext = ".jpg"
                if ".png" in url.lower():
                    ext = ".png"
                elif ".webp" in url.lower():
                    ext = ".webp"
                path = media_dir / f"{tid}_{i}_{key}{ext}"
                if not path.exists():
                    r = client.get(url)
                    if r.status_code != 200 or not r.content:
                        print(f"配图下载失败 {url} -> {r.status_code}", file=sys.stderr)
                        continue
                    path.write_bytes(r.content)
                m["local_path"] = str(path.relative_to(ROOT))
                saved += 1
    return saved


def fetch_tweets(
    client: httpx.Client,
    user_id: str,
    *,
    max_tweets: int,
    include_replies: bool,
) -> list[dict]:
    exclude: list[str] = ["retweets"]
    if not include_replies:
        exclude.append("replies")
    collected: list[dict] = []
    pagination_token: str | None = None
    while len(collected) < max_tweets:
        page_size = min(100, max_tweets - len(collected))
        params: dict[str, str | int] = {
            "max_results": max(5, page_size),
            "tweet.fields": TWEET_FIELDS,
            "expansions": "attachments.media_keys",
            "media.fields": MEDIA_FIELDS,
            "exclude": ",".join(exclude),
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        r = client.get(f"{API_BASE}/users/{user_id}/tweets", params=params)
        _print_rate_headers(r, "GET /2/users/:id/tweets")
        if r.status_code == 402:
            raise httpx.HTTPStatusError(
                "credits depleted", request=r.request, response=r
            )
        if r.status_code == 429:
            raise SystemExit("429：X API 限速窗口用尽，等 15 分钟再试。")
        r.raise_for_status()
        payload = r.json()
        batch = payload.get("data") or []
        _attach_media(batch, payload.get("includes") or {})
        collected.extend(batch)
        pagination_token = (payload.get("meta") or {}).get("next_token")
        if not pagination_token or not batch:
            break
    return collected[:max_tweets]


def _text(tweet: dict) -> str:
    note = tweet.get("note_tweet") or {}
    if isinstance(note, dict) and note.get("text"):
        return str(note["text"])
    return str(tweet.get("text") or "")


def write_outputs(user: dict, tweets: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    blob = {
        "fetched_at": fetched_at,
        "user": user,
        "count": len(tweets),
        "tweets": tweets,
    }
    json_path = OUT_DIR / "tweets.json"
    json_path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"# @{user.get('username')} 近期推文",
        "",
        f"- 拉取时间（UTC）：{fetched_at}",
        f"- 条数：{len(tweets)}",
        f"- 主页：https://x.com/{user.get('username')}",
        "",
    ]
    for t in tweets:
        tid = t.get("id", "")
        created = t.get("created_at", "")
        metrics = t.get("public_metrics") or {}
        url = f"https://x.com/{user.get('username')}/status/{tid}"
        lines.append(f"## {created}")
        lines.append("")
        lines.append(f"- 链接：{url}")
        lines.append(
            f"- 互动：like={metrics.get('like_count')} "
            f"rt={metrics.get('retweet_count')} "
            f"reply={metrics.get('reply_count')} "
            f"view={metrics.get('impression_count')}"
        )
        lines.append("")
        lines.append(_text(t).strip())
        lines.append("")
        media = t.get("media") or []
        if media:
            lines.append("配图：")
            for m in media:
                local = m.get("local_path") or m.get("url") or m.get("preview_image_url")
                lines.append(f"- {m.get('type')} {m.get('width')}x{m.get('height')} `{local}`")
            lines.append("")
        lines.append("---")
        lines.append("")
    md_path = OUT_DIR / "tweets.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")


def load_cache() -> tuple[dict, list[dict]] | None:
    path = OUT_DIR / "tweets.json"
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    user = blob.get("user")
    tweets = blob.get("tweets")
    if not isinstance(user, dict) or not isinstance(tweets, list) or not tweets:
        return None
    return user, tweets


def main() -> None:
    parser = argparse.ArgumentParser(description="拉取 jackli727 近期推文")
    parser.add_argument("--username", default="jackli727")
    parser.add_argument("--max", type=int, default=40, help="最多条数（默认 40）")
    parser.add_argument("--include-replies", action="store_true")
    parser.add_argument(
        "--usage",
        action="store_true",
        help="只查官方 API 月度 usage，不拉推文",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="强制打官方 API 覆盖本地缓存（会按条计费）",
    )
    args = parser.parse_args()
    token = _env_token()
    user = None
    tweets: list[dict] = []
    if args.usage:
        if not token:
            raise SystemExit("查 usage 需要 .env 里的 X_BEARER_TOKEN。")
        with httpx.Client(timeout=30.0, headers=_headers(token), follow_redirects=True) as client:
            fetch_usage(client)
        return

    cached = None if args.refresh else load_cache()
    if cached is not None:
        user, tweets = cached
        json_path = OUT_DIR / "tweets.json"
        print(
            f"使用本地缓存 {json_path}：{len(tweets)} 条，"
            "未请求官方 API（不扣 credits）。"
        )
        print(
            "要更新或往前多拉：.venv/bin/python scripts/fetch_jack_tweets.py "
            f"--refresh --max {max(args.max, len(tweets) + 50)}"
        )
        n_media = sum(len(t.get("media") or []) for t in tweets)
        print(f"配图记录 {n_media} 张，目录 {OUT_DIR / 'media'}")
        return

    if token:
        try:
            with httpx.Client(timeout=30.0, headers=_headers(token), follow_redirects=True) as client:
                fetch_usage(client)
                user = lookup_user(client, args.username)
                print(
                    f"用户 @{user['username']} id={user['id']} "
                    f"粉丝={user.get('public_metrics', {}).get('followers_count')}"
                )
                tweets = fetch_tweets(
                    client,
                    user["id"],
                    max_tweets=max(1, args.max),
                    include_replies=args.include_replies,
                )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (401, 402, 403, 429):
                raise
            body = (exc.response.text or "")[:180]
            if exc.response.status_code == 402:
                print(
                    "官方 API 402 credits depleted：Developer Console 预付积分已用尽，"
                    "读推文会失败。月度 usage cap 仍可能显示 0/3000000。"
                    "充 credits 后才按条计费（约 $0.005/条），并没有「每天固定 N 条」。"
                    f" {body}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"官方 API {exc.response.status_code}：{body}",
                    file=sys.stderr,
                )
            user, tweets = fetch_via_nitter(args.username, max_tweets=max(1, args.max))
        except SystemExit as exc:
            print(str(exc), file=sys.stderr)
            user, tweets = fetch_via_nitter(args.username, max_tweets=max(1, args.max))
    else:
        print("未配置 X_BEARER_TOKEN，改用 Nitter RSS。", file=sys.stderr)
        user, tweets = fetch_via_nitter(args.username, max_tweets=max(1, args.max))

    n_media = download_media(tweets)
    write_outputs(user, tweets)
    print(
        f"已拉取 {len(tweets)} 条，配图 {n_media} 张。"
        " 文字+K线截图都在 .cache/jack_tweets/"
    )


if __name__ == "__main__":
    main()
