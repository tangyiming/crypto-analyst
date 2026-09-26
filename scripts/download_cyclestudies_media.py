#!/usr/bin/env python3
"""下载 @CycleStudies 推文配图到 .cache/cyclestudies_tweets/media/（不碰参考推文缓存）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".cache" / "cyclestudies_tweets"
MEDIA = OUT / "media"


def _orig(url: str) -> str:
    if "pbs.twimg.com/media/" not in url:
        return url
    if "name=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "name=orig"


def _text(t: dict) -> str:
    note = t.get("note_tweet") or {}
    if isinstance(note, dict) and note.get("text"):
        return str(note["text"])
    return str(t.get("text") or "")


def main() -> None:
    MEDIA.mkdir(parents=True, exist_ok=True)
    blob = json.loads((OUT / "tweets.json").read_text(encoding="utf-8"))
    user = (blob.get("user") or {}).get("username") or ""
    if user.lower() != "cyclestudies":
        raise SystemExit(f"拒绝：缓存用户是 @{user}，不是 CycleStudies")
    tweets = blob["tweets"]
    existing = {p.name: p for p in MEDIA.glob("*")}
    saved = skipped = linked = failed = 0
    headers = {"User-Agent": "Mozilla/5.0 (compatible; crypto-analyst/1.0)"}
    with httpx.Client(timeout=40.0, headers=headers, follow_redirects=True) as client:
        for t in tweets:
            tid = t.get("id") or "unknown"
            for i, m in enumerate(t.get("media") or []):
                url = m.get("url") or m.get("preview_image_url")
                if not url:
                    continue
                url = _orig(str(url))
                key = (m.get("media_key") or f"{tid}_{i}").replace("/", "_")
                ext = ".jpg"
                low = url.lower()
                if ".png" in low:
                    ext = ".png"
                elif ".webp" in low:
                    ext = ".webp"
                path = MEDIA / f"{tid}_{i}_{key}{ext}"
                rel = str(path.relative_to(ROOT))
                if path.exists() and path.stat().st_size > 0:
                    m["local_path"] = rel
                    skipped += 1
                    continue
                matched = None
                for name, p in existing.items():
                    if name.startswith(f"{tid}_{i}_"):
                        matched = p
                        break
                if matched is not None and matched.stat().st_size > 0:
                    m["local_path"] = str(matched.relative_to(ROOT))
                    linked += 1
                    continue
                r = client.get(url)
                if r.status_code != 200 or not r.content:
                    failed += 1
                    continue
                path.write_bytes(r.content)
                m["local_path"] = rel
                saved += 1
                if saved % 40 == 0:
                    print(f"…saved {saved}")

    (OUT / "tweets.json").write_text(
        json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    disk = len(list(MEDIA.glob("*")))
    print(
        f"DONE saved={saved} skipped={skipped} linked={linked} "
        f"failed={failed} disk={disk}"
    )

    rows = []
    for t in tweets:
        s = _text(t)
        if not re.search(r"超卖|超买|背离", s):
            continue
        paths = [m["local_path"] for m in (t.get("media") or []) if m.get("local_path")]
        if not paths:
            continue
        rows.append(
            {
                "date": str(t.get("created_at", ""))[:10],
                "id": t.get("id"),
                "text": s[:600],
                "images": paths,
                "url": f"https://x.com/CycleStudies/status/{t.get('id')}",
            }
        )
    (OUT / "signal_images_index.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"signal+img={len(rows)} index={OUT / 'signal_images_index.json'}")


if __name__ == "__main__":
    main()
