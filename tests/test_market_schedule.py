"""市场日程：时段 / 时钟 / 资金费 / 宏观提醒。"""

from datetime import datetime, timezone

from analyst.compute.market_schedule import (
    SESSION_WINDOWS,
    build_clocks,
    build_sessions,
    funding_lead_candidates,
    funding_snapshot,
    session_lead_candidates,
)


def test_clocks_have_zones():
    rows = build_clocks(local_tz="Asia/Dubai")
    assert rows[0]["id"] == "local"
    assert rows[0]["tz"] == "Asia/Dubai"
    ids = {r["id"] for r in rows}
    assert "beijing" in ids
    assert "newyork" in ids
    assert all(r["time"] and ":" in r["time"] for r in rows)


def test_sessions_structure():
    data = build_sessions()
    assert "windows" in data
    assert "upcoming" in data or data["active"]
    assert len(SESSION_WINDOWS) >= 3


def test_session_lead_near_window(monkeypatch):
    # 构造「距下一亚盘开始恰好 30 分钟」——亚盘 00:00 UTC
    # 用固定 now = 前一天 23:30 UTC
    now = datetime(2026, 7, 15, 23, 30, 0, tzinfo=timezone.utc)
    cands = session_lead_candidates([30, 15], now=now)
    keys = [c["key"] for c in cands]
    assert any(k.startswith("session|asia_am|") and k.endswith("|30") for k in keys)


def test_funding_snapshot_and_lead():
    now = datetime(2026, 7, 16, 7, 30, 0, tzinfo=timezone.utc)
    # 8:00 UTC = 30 分钟后
    nft = int(datetime(2026, 7, 16, 8, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    prem = {"next_funding_time": nft, "funding_rate": 0.0001}
    snap = funding_snapshot(prem, symbol="BTC/USDT", now=now)
    assert snap is not None
    assert abs(snap["seconds_to_funding"] - 1800) < 2
    cands = funding_lead_candidates(prem, [30], symbol="BTC/USDT", now=now)
    assert len(cands) == 1
    assert cands[0]["kind"] == "funding"
