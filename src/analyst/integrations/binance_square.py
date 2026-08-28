"""币安广场 OpenAPI：发文 / 配图。

协议对齐官方 square-post skill：
POST https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add
POST https://www.binance.com/bapi/composite/v2/public/pgc/openApi/image/*
Header: X-Square-OpenAPI-Key
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL_V1 = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi"
BASE_URL_V2 = "https://www.binance.com/bapi/composite/v2/public/pgc/openApi"

_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_POLL_INTERVAL_S = 3.0
_POLL_MAX = 10


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


def _headers(api_key: str) -> dict[str, str]:
    return {
        "X-Square-OpenAPI-Key": api_key.strip(),
        "Content-Type": "application/json",
        "clienttype": "binanceSkill",
    }


def _parse_api_response(resp: httpx.Response, *, endpoint: str) -> dict[str, Any]:
    if endpoint == "/content/add" and resp.status_code == 504:
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
    return out if isinstance(out, dict) else {"raw": out}


def _api_post(
    api_key: str,
    endpoint: str,
    body: dict[str, Any],
    *,
    base_url: str = BASE_URL_V2,
    timeout: float = 30.0,
) -> dict[str, Any]:
    key = (api_key or "").strip()
    if not key:
        raise ValueError("缺少 BINANCE_SQUARE_OPENAPI_KEY")
    url = f"{base_url}{endpoint}"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=_headers(key), json=body)
    except httpx.TimeoutException as e:
        raise RuntimeError(f"Square API 超时 {endpoint}（key={mask_key(key)}）") from e
    except httpx.HTTPError as e:
        raise RuntimeError(f"Square API 网络错误 {endpoint}: {e}") from e
    return _parse_api_response(resp, endpoint=endpoint)


def _content_type(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def upload_image(api_key: str, img_path: str | Path, *, timeout: float = 60.0) -> str:
    """上传本地图片，轮询处理后返回 imageUrl。"""
    path = Path(img_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    image_name = path.name
    presigned = _api_post(
        api_key,
        "/image/presignedUrl",
        {"imageName": image_name},
        timeout=timeout,
    )
    presigned_url = str(presigned.get("presignedUrl") or "")
    file_ticket = presigned.get("fileTicket")
    if not presigned_url or not file_ticket:
        raise RuntimeError(f"Square presignedUrl 响应异常: {presigned}")
    ct = _content_type(path)
    data = path.read_bytes()
    try:
        with httpx.Client(timeout=timeout) as client:
            put = client.put(presigned_url, content=data, headers={"Content-Type": ct})
            put.raise_for_status()
    except httpx.HTTPError as e:
        raise RuntimeError(f"Square S3 上传失败: {e}") from e
    for i in range(_POLL_MAX):
        status = _api_post(api_key, "/image/imageStatus", {"fileTicket": file_ticket}, timeout=timeout)
        st = status.get("status")
        if st == 1:
            url = status.get("imageUrl")
            if not url:
                raise RuntimeError(f"Square imageStatus 无 imageUrl: {status}")
            logger.info("Square 图片就绪 %s", image_name)
            return str(url)
        if st == 2:
            raise RuntimeError(f"Square 图片处理失败: {status.get('failedReason')}")
        logger.debug("Square 图片处理中 %s (%d/%d)", image_name, i + 1, _POLL_MAX)
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(f"Square 图片处理超时 ticket={file_ticket}")


def post_content(
    api_key: str,
    text: str,
    *,
    title: str | None = None,
    image_urls: list[str] | None = None,
    cover: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """发短文/图文/长文。imageList 仅用于 contentType=1；cover 用于 contentType=2。"""
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
        if cover:
            payload["cover"] = cover
    elif image_urls:
        payload["imageList"] = list(image_urls)

    return _api_post(key, "/content/add", payload, base_url=BASE_URL_V1, timeout=timeout)


def post_text(
    api_key: str,
    text: str,
    *,
    title: str | None = None,
    image_urls: list[str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """发短文（无 title）或长文（有 title）；可附 imageList。"""
    return post_content(
        api_key,
        text,
        title=title,
        image_urls=image_urls,
        timeout=timeout,
    )
