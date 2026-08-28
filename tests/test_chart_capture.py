"""K 线截图 payload 与广场配图。"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from analyst.data.fetcher import Candle
from analyst.integrations.chart_capture import SquareChartRequest, build_chart_payload
from analyst.monitor.square_posts import _chart_for_post, _publish_square_post


def _fake_candles():
    return SimpleNamespace(
        candles=[
            Candle(
                timestamp=datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000.0,
            )
        ]
    )


def test_chart_levels_resistance_must_be_above_price():
    from types import SimpleNamespace
    from analyst.integrations.chart_capture import _chart_levels

    regime = SimpleNamespace(
        trade_side="long",
        nearest_support=62000.0,
        nearest_resistance=67000.0,  # 低于现价，不应画成阻力/近压
        ext_150=82000.0,
        ext_1618=83500.0,
    )
    jack = SimpleNamespace(
        defense_level=77000.0,
        rebound_382=70000.0,
        rebound_618=75000.0,
        touch_level=80500.0,
    )
    levels = _chart_levels(regime, jack, 79000.0)
    labels = {lv["label"] for lv in levels}
    prices = {round(lv["price"]) for lv in levels}
    assert 67000 not in prices
    assert "近压" in labels or "阻力" in labels
    for lv in levels:
        if lv["label"] in ("近压", "目标", "阻力"):
            assert lv["price"] > 79000
        if lv["label"] in ("防守", "支撑"):
            assert lv["price"] < 79000


def test_build_eric_chart_payload_has_filter_panel():
    from analyst.integrations.chart_capture import EricChartRequest, build_eric_chart_payload

    req = EricChartRequest(
        symbol="BTC/USDT",
        timeframe="1w",
        price=79000.0,
        bf_value=-42.0,
        kind="weekly_watch",
        extra_levels=[{"price": 75000.0, "color": "#f6465d", "title": "止损"}],
    )
    with patch("analyst.data.fetcher.fetch_candles", return_value=_fake_candles()):
        payload = build_eric_chart_payload(req)
    assert payload["mode"] == "eric"
    assert payload["filter"]["bars"]
    assert payload["filter"]["current"] == -42.0
    assert len(payload["filter"]["bars"]) == len(payload["candles"])
    assert payload["title"].find("Eric") >= 0


def test_build_chart_payload_levels():
    regime = SimpleNamespace(
        regime_zh="强势盘",
        trade_side="long",
        nearest_support=98.0,
        nearest_resistance=103.0,
        ext_150=108.0,
        ext_1618=110.0,
    )
    jack = SimpleNamespace(
        defense_level=95.0,
        rebound_382=98.0,
        rebound_618=102.0,
        touch_level=105.0,
    )
    req = SquareChartRequest(
        symbol="BTC/USDT",
        timeframe="4h",
        price=100.0,
        regime=regime,
        jack=jack,
    )
    with patch("analyst.data.fetcher.fetch_candles", return_value=_fake_candles()):
        payload = build_chart_payload(req)
    assert payload["title"].startswith("$BTC")
    assert payload["candles"]
    # 标注价跟 K 线收盘价，不跟 req.price
    assert payload.get("price") == 100.5
    assert payload["levels"]
    labels = {lv.get("label") for lv in payload["levels"]}
    assert "防守" in labels
    for lv in payload["levels"]:
        assert "price" in lv
        assert lv.get("label") or lv.get("title")
        p = float(lv["price"])
        if lv.get("label") in ("近压", "目标", "阻力"):
            assert p > 100.5
        if lv.get("label") in ("防守", "支撑"):
            assert p < 100.5


def test_chart_for_post_returns_request():
    chart = _chart_for_post(
        symbol="ETH/USDT",
        timeframe="1d",
        price=2400.0,
    )
    assert chart.symbol == "ETH/USDT"
    assert chart.timeframe == "1d"


def test_publish_square_post_attaches_chart(monkeypatch, tmp_path):
    import analyst.monitor.square_posts as sp

    monkeypatch.setattr(sp, "get_settings", lambda: SimpleNamespace(
        square_post_enabled=True,
        binance_square_openapi_key="sk-test-key",
        square_post_compact=True,
        square_post_ai_polish=False,
        square_post_chart_enabled=True,
        data_cache_dir=str(tmp_path),
    ))
    monkeypatch.setattr(sp, "_load_cooldown", lambda: {})
    monkeypatch.setattr(sp, "_save_cooldown", lambda d: None)
    monkeypatch.setattr(
        sp,
        "_square_image_urls",
        lambda chart, key, settings=None: ["https://img.example/chart.png"],
    )
    posted = {}

    def _post_content(key, text, image_urls=None, **kw):
        posted["key"] = key
        posted["text"] = text
        posted["images"] = image_urls
        return {"id": "123", "shareLink": "https://square.example/p/123"}

    monkeypatch.setattr(sp, "post_content", _post_content)
    chart = _chart_for_post(symbol="BTC/USDT", timeframe="4h", price=65000.0)
    out = _publish_square_post(
        "测试帖 $BTC",
        cool_key="test|BTC/USDT|4h",
        cooldown_hours=0,
        chart=chart,
    )
    assert out is not None
    assert posted["images"] == ["https://img.example/chart.png"]
