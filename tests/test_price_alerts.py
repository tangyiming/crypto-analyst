"""实时价格告警：穿越只响一次，涨跌按窗口边沿触发。"""

from pathlib import Path

import pytest

from analyst.monitor.price_alerts import PriceAlertBook


def book(tmp_path: Path) -> PriceAlertBook:
    b = PriceAlertBook(tmp_path / "price_alerts.json")
    b.load()
    return b


def test_cross_up_fires_once_then_waits_for_retrace(tmp_path: Path):
    b = book(tmp_path)
    alert = b.add(
        {"symbol": "BTC", "kind": "cross", "direction": "up", "price": 100},
        now=1,
    )
    assert b.on_tick("BTC/USDT", 99, now=2) == []
    fired = b.on_tick("BTC/USDT", 101, now=3)
    assert len(fired) == 1
    assert fired[0][1]["direction"] == "long"
    assert b.alerts[alert.id].enabled is False
    assert b.on_tick("BTC/USDT", 102, now=4) == []


def test_repeat_cross_needs_retrace(tmp_path: Path):
    b = book(tmp_path)
    b.add(
        {
            "symbol": "ETH/USDT",
            "kind": "below",
            "price": 50,
            "repeat": True,
            "cooldown_sec": 0,
        },
        now=1,
    )
    b.on_tick("ETH/USDT", 51, now=2)
    assert len(b.on_tick("ETH/USDT", 49, now=3)) == 1
    assert b.on_tick("ETH/USDT", 48, now=4) == []
    assert b.on_tick("ETH/USDT", 52, now=5) == []
    assert len(b.on_tick("ETH/USDT", 49, now=6)) == 1


def test_pct_window_waits_until_tape_covers(tmp_path: Path):
    b = book(tmp_path)
    b.add(
        {
            "symbol": "SOL/USDT",
            "kind": "pct_change",
            "direction": "up",
            "threshold": 2,
            "window_sec": 60,
            "repeat": True,
            "cooldown_sec": 0,
        },
        now=1_000,
    )
    assert b.on_tick("SOL/USDT", 100, now=1_000) == []
    assert b.on_tick("SOL/USDT", 103, now=1_030) == []
    fired = b.on_tick("SOL/USDT", 103, now=1_060)
    assert len(fired) == 1
    assert "+3.00%" in fired[0][1]["reasons"][0]
    assert b.on_tick("SOL/USDT", 104, now=1_061) == []


def test_abs_since_anchor_then_resets_on_repeat(tmp_path: Path):
    b = book(tmp_path)
    b.add(
        {
            "symbol": "DOGE/USDT",
            "kind": "abs_change",
            "direction": "down",
            "threshold": 0.01,
            "window_sec": 0,
            "repeat": True,
            "cooldown_sec": 0,
        },
        now=10,
    )
    b.on_tick("DOGE/USDT", 0.10, now=11)
    fired = b.on_tick("DOGE/USDT", 0.08, now=12)
    assert len(fired) == 1
    assert b.on_tick("DOGE/USDT", 0.079, now=13) == []
    assert len(b.on_tick("DOGE/USDT", 0.06, now=14)) == 1


def test_rejects_bad_spec(tmp_path: Path):
    b = book(tmp_path)
    with pytest.raises(ValueError):
        b.add({"symbol": "BTC", "kind": "above"}, now=1)


def test_persists_without_replaying_last_tick(tmp_path: Path):
    b = book(tmp_path)
    alert = b.add({"symbol": "BTC", "kind": "above", "price": 10}, now=1)
    b.on_tick("BTC/USDT", 9, now=2)
    b.on_tick("BTC/USDT", 11, now=3)
    again = PriceAlertBook(b.path)
    again.load()
    assert again.alerts[alert.id].enabled is False
    assert again.alerts[alert.id].last_price is None
