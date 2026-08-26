#!/usr/bin/env python3
"""拉取 @CycleStudies（百萬Eric）推文到 .cache/cyclestudies_tweets/。

与 Jack（jackli727）缓存完全隔离，不会写入 .cache/jack_tweets/。

用法（项目根）：
  .venv/bin/python scripts/fetch_cyclestudies_tweets.py --refresh --max 800
  .venv/bin/python scripts/fetch_cyclestudies_tweets.py --refresh --years 2 --max 1200
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
# 复用官方 API / Nitter 工具，但覆盖输出目录
import fetch_jack_tweets as fj  # noqa: E402
OUT = ROOT / ".cache" / "cyclestudies_tweets"
USERNAME = "CycleStudies"


def main() -> None:
    parser = argparse.ArgumentParser(description="拉取 @CycleStudies 推文（独立缓存）")
    parser.add_argument("--max", type=int, default=600, help="最多条数")
    parser.add_argument("--years", type=float, default=2.0, help="回溯年数（默认 2）")
    parser.add_argument("--include-replies", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="强制打官方 API")
    parser.add_argument("--skip-media", action="store_true", help="不下载配图")
    args = parser.parse_args()

    fj.OUT_DIR = OUT
    start = (datetime.now(timezone.utc) - timedelta(days=int(365 * args.years))).replace(
        microsecond=0
    )
    start_time = start.strftime("%Y-%m-%dT%H:%M:%SZ")

    if not args.refresh:
        cached = fj.load_cache()
        if cached is not None:
            user, tweets = cached
            print(
                f"使用本地缓存 {OUT / 'tweets.json'}：{len(tweets)} 条 "
                f"(@{user.get('username')})，未请求官方 API。"
            )
            print("更新：加 --refresh")
            return

    token = fj._env_token()
    user: dict
    tweets: list[dict]

    if not token:
        print("未配置 X_BEARER_TOKEN，改用 Nitter RSS。", file=sys.stderr)
        user, tweets = fj.fetch_via_nitter(USERNAME, max_tweets=max(1, args.max))
    else:
        try:
            with httpx.Client(
                timeout=45.0, headers=fj._headers(token), follow_redirects=True
            ) as client:
                fj.fetch_usage(client)
                user = fj.lookup_user(client, USERNAME)
                uname = user.get("username") or USERNAME
                if uname.lower() != USERNAME.lower():
                    raise SystemExit(f"用户名不匹配：期望 {USERNAME}，得到 {uname}")
                print(
                    f"用户 @{uname} id={user['id']} "
                    f"粉丝={user.get('public_metrics', {}).get('followers_count')}"
                )
                print(f"时间窗 start_time={start_time}（近 {args.years:g} 年）")
                # 给 fetch_tweets 打补丁：支持 start_time
                tweets = _fetch_with_start(
                    client,
                    user["id"],
                    max_tweets=max(1, args.max),
                    include_replies=args.include_replies,
                    start_time=start_time,
                )
        except httpx.HTTPStatusError as exc:
            body = (exc.response.text or "")[:200]
            print(f"官方 API {exc.response.status_code}：{body}", file=sys.stderr)
            user, tweets = fj.fetch_via_nitter(USERNAME, max_tweets=max(1, args.max))
        except SystemExit as exc:
            print(str(exc), file=sys.stderr)
            user, tweets = fj.fetch_via_nitter(USERNAME, max_tweets=max(1, args.max))

    # 二次校验：绝不能写成 jack
    if (user.get("username") or "").lower() not in (USERNAME.lower(), "cyclestudies"):
        print(f"警告：用户字段={user.get('username')}", file=sys.stderr)

    n_media = 0 if args.skip_media else fj.download_media(tweets)
    fj.write_outputs(user, tweets)
    oldest = tweets[-1].get("created_at") if tweets else None
    newest = tweets[0].get("created_at") if tweets else None
    print(
        f"已拉取 @{user.get('username')} {len(tweets)} 条，配图 {n_media} 张。"
        f" 区间 {oldest} → {newest}"
    )
    print(f"输出目录：{OUT}（与 .cache/jack_tweets 隔离）")


def _fetch_with_start(
    client: httpx.Client,
    user_id: str,
    *,
    max_tweets: int,
    include_replies: bool,
    start_time: str,
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
            "tweet.fields": fj.TWEET_FIELDS,
            "expansions": "attachments.media_keys",
            "media.fields": fj.MEDIA_FIELDS,
            "exclude": ",".join(exclude),
            "start_time": start_time,
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        r = client.get(f"{fj.API_BASE}/users/{user_id}/tweets", params=params)
        fj._print_rate_headers(r, "GET /2/users/:id/tweets")
        if r.status_code == 402:
            raise httpx.HTTPStatusError(
                "credits depleted", request=r.request, response=r
            )
        if r.status_code == 429:
            raise SystemExit("429：X API 限速窗口用尽，等 15 分钟再试。")
        r.raise_for_status()
        payload = r.json()
        batch = payload.get("data") or []
        fj._attach_media(batch, payload.get("includes") or {})
        collected.extend(batch)
        print(f"  …已收 {len(collected)} 条（本页 {len(batch)}）")
        pagination_token = (payload.get("meta") or {}).get("next_token")
        if not pagination_token or not batch:
            break
    return collected[:max_tweets]


if __name__ == "__main__":
    main()
