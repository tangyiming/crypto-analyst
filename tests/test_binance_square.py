"""币安广场 OpenAPI：配图上传。"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from analyst.integrations.binance_square import upload_image


def test_upload_image_happy_path(tmp_path):
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    presigned_resp = MagicMock()
    presigned_resp.status_code = 200
    presigned_resp.text = ""
    presigned_resp.json.return_value = {
        "code": "000000",
        "data": {
            "presignedUrl": "https://s3.example/put",
            "fileTicket": "ticket-1",
        },
    }

    put_resp = MagicMock()
    put_resp.raise_for_status = MagicMock()

    status_resp = MagicMock()
    status_resp.status_code = 200
    status_resp.text = ""
    status_resp.json.return_value = {
        "code": "000000",
        "data": {"status": 1, "imageUrl": "https://cdn.example/img.png"},
    }

    def _post(url, **kw):
        if url.endswith("/image/presignedUrl"):
            return presigned_resp
        if url.endswith("/image/imageStatus"):
            return status_resp
        raise AssertionError(url)

    with patch("analyst.integrations.binance_square.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.side_effect = _post
        client.put.return_value = put_resp
        client_cls.return_value = client

        url = upload_image("sk-test", img)

    assert url == "https://cdn.example/img.png"
    client.put.assert_called_once()
    put_args, put_kw = client.put.call_args
    assert put_args[0] == "https://s3.example/put"
    assert put_kw["headers"]["Content-Type"] == "image/png"
