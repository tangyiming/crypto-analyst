"""币安广场 OpenAPI：发短文（contentType=1）。

协议对齐官方 square-post skill：
POST https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add
Header: X-Square-OpenAPI-Key
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL_V1 = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi"


class SquareApiError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"Square API [{code}]: {message}")
        self.code = code
        self.message = message


def mask_key(api_key: str) -> str:
    k = (api_key or "").strip()
    if len(k) <= 9:
        return (k[:2] + "…") if k else ""
    return f"{k[:5]}…{k[-4:]}"


def post_text(
    api_key: str,
    text: str,
    *,
    title: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """发短文（无 title）或长文（有 title）。返回 {id, shareLink, ...}。"""
    key = (api_key or "").strip()
    body_text = (text or "").strip()
    if not key:
        raise ValueError("缺少 BINANCE_SQUARE_OPENAPI_KEY")
    if not body_text:
        raise ValueError("帖文不能为空")

    content_type = 2 if (title or "").strip() else 1
    payload: dict[str, Any] = {
        "contentType": content_type,
        "bodyTextOnly": body_text,
    }
    if content_type == 2:
        payload["title"] = title.strip()  # type: ignore[union-attr]

    url = f"{BASE_URL_V1}/content/add"
    headers = {
        "X-Square-OpenAPI-Key": key,
        "Content-Type": "application/json",
        "clienttype": "binanceSkill",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException as e:
        raise RuntimeError(f"Square 发帖超时（key={mask_key(key)}）") from e
    except httpx.HTTPError as e:
        raise RuntimeError(f"Square 发帖网络错误: {e}") from e

    # 官方脚本：504 也视为已提交成功，但无 id
    if resp.status_code == 504:
        logger.warning("Square /content/add 504，视为已提交但无帖子 ID")
        return {"id": None, "shareLink": None, "publishStatus": "success_without_post_id"}

    raw = resp.text or ""
    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError(
            f"Square 非 JSON 响应 HTTP {resp.status_code}: {raw[:300]}"
        ) from e

    code = str(data.get("code") or "")
    if code != "000000":
        raise SquareApiError(code, str(data.get("message") or raw[:200]))

    out = data.get("data") or {}
    if not isinstance(out, dict):
        out = {"raw": out}
    return out
