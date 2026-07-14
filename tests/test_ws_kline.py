"""WebSocket kline 解析测试。"""

from analyst.data.ws_kline import _parse_kline_msg, kline_url, symbol_to_stream


def test_symbol_to_stream():
    assert symbol_to_stream("BTC/USDT") == "btcusdt"


def test_kline_url_spot():
    url = kline_url("BTC/USDT", "15m", market="spot")
    assert url.endswith("/btcusdt@kline_15m")
    assert "stream.binance.com" in url


def test_mark_price_url():
    from analyst.data.ws_kline import mark_price_url

    url = mark_price_url("BTC/USDT", speed="1s")
    assert url.endswith("/btcusdt@markPrice@1s")
    assert "fstream.binance.com" in url


def test_parse_mark_price():
    from analyst.data.ws_kline import _parse_mark_price_msg

    payload = {
        "e": "markPriceUpdate",
        "E": 1_700_000_000_000,
        "s": "BTCUSDT",
        "p": "100.5",
        "i": "100.0",
        "P": "100.2",
        "r": "0.0001",
        "T": 1_700_000_100_000,
    }
    parsed = _parse_mark_price_msg(payload)
    assert parsed is not None
    assert parsed["mark_price"] == 100.5
    assert parsed["index_price"] == 100.0
    assert abs(parsed["premium_pct"] - 0.5) < 1e-9
    assert parsed["funding_rate"] == 0.0001


def test_futures_intervals_include_daily_weekly_monthly():
    from analyst.data.ws_kline import BINANCE_FUTURES_INTERVALS

    for tf in ("1d", "1w", "1M", "3m", "12h"):
        assert tf in BINANCE_FUTURES_INTERVALS


def test_parse_closed_kline():
    payload = {
        "e": "kline",
        "k": {
            "t": 1_700_000_000_000,
            "o": "100",
            "h": "110",
            "l": "90",
            "c": "105",
            "v": "12.5",
            "x": True,
        },
    }
    parsed = _parse_kline_msg(payload)
    assert parsed is not None
    candle, closed = parsed
    assert closed is True
    assert candle.close == 105.0
    assert candle.volume == 12.5
