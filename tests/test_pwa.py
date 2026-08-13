"""PWA 静态资源存在，供手机安装 / APK WebView 加载。"""

from pathlib import Path

from analyst.web.server import STATIC_DIR, create_app


def test_pwa_assets_exist():
    for name in (
        "manifest.webmanifest",
        "sw.js",
        "icon-192.png",
        "icon-512.png",
        "apple-touch-icon.png",
        "index.html",
    ):
        assert (STATIC_DIR / name).is_file(), name


def test_pwa_routes_registered():
    app = create_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    for path in ("/manifest.webmanifest", "/sw.js", "/icon-192.png"):
        assert path in paths


def test_lan_ipv4_helper_runs():
    from analyst.cli import _lan_ipv4

    ips = _lan_ipv4()
    assert isinstance(ips, list)
