"""Eric 周线超卖波段计划状态机：观察 → 拐头开仓 → 第一半止盈 → 余仓回撤离场 / 止损。"""

from datetime import datetime, timedelta

from analyst.compute.band_filter import band_filter_series, hysteresis_states
from analyst.compute.eric_swing import STOP_PAD, TP2_TRAIL, advance
from analyst.data.fetcher import Candle, CandleSeries


def _wk(i: int, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(timestamp=datetime(2024, 1, 1) + timedelta(weeks=i), open=o, high=h, low=l, close=c, volume=1.0)


def _daily_from_week(week_idx: int, days: list[tuple[float, float, float, float]]) -> list[Candle]:
    base = datetime(2024, 1, 1) + timedelta(weeks=week_idx)
    return [Candle(timestamp=base + timedelta(days=k), open=o, high=h, low=l, close=c, volume=1.0) for k, (o, h, l, c) in enumerate(days)]


def _weekly_oversold_then_turn() -> tuple[list[Candle], int]:
    """先涨 60 周，再连续下跌直到周线过滤器进入超卖；最后一根拐头。返回 (K 线, 拐头周索引)。"""
    closes = [100.0 + i * 0.8 for i in range(60)]
    candles = [_wk(i, c - 0.5, c + 1, c - 1, c) for i, c in enumerate(closes)]
    i = 60
    while True:
        c = closes[-1] - 2.5
        closes.append(c)
        candles.append(_wk(i, c + 1, c + 1.5, c - 1.5, c))
        X = band_filter_series([x.high for x in candles], [x.low for x in candles], [x.close for x in candles])
        st = hysteresis_states(X, os_level=-40, os_exit=-25, ob_level=40, ob_exit=25)
        if st[-1] == -1 and st[-2] == -1:
            break
        i += 1
    # 再跌两根（继续超卖），然后一根明显反弹 → 读数拐头
    for _ in range(2):
        i += 1
        c = closes[-1] - 2.0
        closes.append(c)
        candles.append(_wk(i, c + 1, c + 1.2, c - 1.2, c))
    i += 1
    c = closes[-1] + 6.0
    closes.append(c)
    candles.append(_wk(i, c - 5, c + 0.5, c - 5.5, c))
    return candles, i


def test_state_machine_watch_entry_tp1_tp2():
    candles, turn_i = _weekly_oversold_then_turn()
    # 逐周推进到拐头前一周：应先出现 watch，且不开仓
    state: dict = {}
    events_all = []
    for n in range(61, turn_i):
        ev, state = advance(state, CandleSeries("BTC/USDT", "1w", candles[:n]), None)
        events_all += ev
    kinds = [e.kind for e in events_all]
    assert "watch" in kinds and "entry" not in kinds
    assert state["phase"] == "armed"
    ep_low = state["episode_low"]
    # 拐头周收盘 → 开仓
    ev, state = advance(state, CandleSeries("BTC/USDT", "1w", candles[: turn_i + 1]), None)
    assert [e.kind for e in ev] == ["entry"]
    assert state["phase"] == "long"
    entry = state["entry"]
    assert abs(state["stop"] - min(ep_low, candles[turn_i].low) * (1 - STOP_PAD)) < 1e-6 or state["stop"] <= entry * 0.995
    plan = ev[0].plan
    assert plan["stop"] < entry < plan["tp1"]
    # 日线：先一根平淡 → 无事件；再一根冲到 +25% → tp1
    weekly = CandleSeries("BTC/USDT", "1w", candles[: turn_i + 1])
    d1 = _daily_from_week(turn_i + 1, [(entry, entry * 1.02, entry * 0.99, entry * 1.01)])
    ev, state = advance(state, weekly, CandleSeries("BTC/USDT", "1d", d1 * 30))
    # 30 根重复日 K 只为满足长度；同一时间戳会被 last_daily_ts 去重，这里取最后一根
    assert all(e.kind != "tp1" for e in ev)
    d2 = d1 * 29 + _daily_from_week(turn_i + 2, [(entry * 1.1, entry * 1.30, entry * 1.05, entry * 1.28)])
    ev, state = advance(state, weekly, CandleSeries("BTC/USDT", "1d", d2))
    assert [e.kind for e in ev if e.kind not in ("status", "near")] == ["tp1"]
    assert state["phase"] == "runner" and state["tp1_done"] and state["stop"] >= entry
    peak = state["peak"]
    # 再一根回撤 > 20% → tp2，状态清空
    # 回撤 K 的最低价须高于保本止损（entry），否则先触发 stop
    lvl = peak * (1 - TP2_TRAIL - 0.01)
    assert lvl > entry
    d3 = d2 + _daily_from_week(turn_i + 3, [(peak * 0.9, peak * 0.9, lvl, lvl)])
    ev, state = advance(state, weekly, CandleSeries("BTC/USDT", "1d", d3))
    assert [e.kind for e in ev] == ["tp2"]
    assert state["phase"] == "flat"


def test_stop_out_path_and_no_repeat_on_same_bar():
    candles, turn_i = _weekly_oversold_then_turn()
    state: dict = {}
    for n in range(61, turn_i + 2):  # 含拐头周（candles[:turn_i+1]）
        _, state = advance(state, CandleSeries("BTC/USDT", "1w", candles[:n]), None)
    assert state["phase"] == "long"
    entry, stop = state["entry"], state["stop"]
    weekly = CandleSeries("BTC/USDT", "1w", candles[: turn_i + 1])
    days = _daily_from_week(turn_i + 1, [(entry, entry * 1.01, entry * 0.98, entry)] * 29 + [(entry * 0.98, entry * 0.99, stop * 0.99, stop * 0.995)])
    ev, state = advance(state, weekly, CandleSeries("BTC/USDT", "1d", days))
    assert [e.kind for e in ev] == ["stop"]
    assert state["phase"] == "flat"
    # 同一根日线再评估一次：不重复出事件
    ev2, state2 = advance(state, weekly, CandleSeries("BTC/USDT", "1d", days))
    assert ev2 == [] and state2["phase"] == "flat"


def test_replay_matches_stepwise_state():
    from analyst.compute.eric_swing import replay

    candles, turn_i = _weekly_oversold_then_turn()
    weekly = CandleSeries("BTC/USDT", "1w", candles[: turn_i + 1])
    stepwise: dict = {}
    for n in range(61, turn_i + 2):
        _, stepwise = advance(stepwise, CandleSeries("BTC/USDT", "1w", candles[:n]), None)
    entry = stepwise["entry"]
    # 拐头周之后两根日线：第二根打到 +25% → 逐步推进应到 runner
    # 前置 28 根入场前的日线只为满足 ≥30 根的长度要求（时间早于入场周，会被忽略）
    pad = _daily_from_week(turn_i - 5, [(entry, entry, entry, entry)] * 28)
    days = pad + _daily_from_week(turn_i + 1, [(entry, entry * 1.02, entry * 0.99, entry * 1.01), (entry * 1.1, entry * 1.30, entry * 1.05, entry * 1.28)])
    daily = CandleSeries("BTC/USDT", "1d", days)
    for j in range(len(pad), len(days)):
        _, stepwise = advance(stepwise, weekly, CandleSeries("BTC/USDT", "1d", days[: j + 1]))
    assert stepwise["phase"] == "runner"
    rp = replay(weekly, daily)
    assert rp["phase"] == stepwise["phase"]
    assert abs(rp["entry"] - stepwise["entry"]) < 1e-9 and rp["tp1_done"] and rp["stop"] >= entry


def test_mark_stop_and_status_events():
    from analyst.compute.eric_swing import NEAR_PCT, check_mark_stop

    candles, turn_i = _weekly_oversold_then_turn()
    state: dict = {}
    for n in range(61, turn_i + 2):
        _, state = advance(state, CandleSeries("BTC/USDT", "1w", candles[:n]), None)
    assert state["phase"] == "long"
    entry, stop = state["entry"], state["stop"]
    weekly = CandleSeries("BTC/USDT", "1w", candles[: turn_i + 1])
    # 日线收盘仍持仓 → 心跳 status；收盘价距止损 <3% → near
    pad = _daily_from_week(turn_i - 5, [(entry, entry, entry, entry)] * 29)  # 凑够 ≥30 根日线
    calm = pad + _daily_from_week(turn_i + 1, [(entry, entry * 1.02, entry * 0.99, entry * 1.01)])
    ev, st1 = advance(state, weekly, CandleSeries("BTC/USDT", "1d", calm))
    assert [e.kind for e in ev] == ["status"]
    near_px = stop * (1 + NEAR_PCT / 2)
    near = pad + _daily_from_week(turn_i + 1, [(entry, entry, near_px * 0.999, near_px), (near_px, near_px, near_px * 0.999, near_px)])
    ev, _ = advance(dict(state), weekly, CandleSeries("BTC/USDT", "1d", near))
    assert [e.kind for e in ev] == ["near"]
    # 实时标记价打穿止损 → 立即 stop；再次检查不重复
    ev, st2 = check_mark_stop(st1, stop * 0.999, 1_700_000_000)
    assert [e.kind for e in ev] == ["stop"] and st2["phase"] == "flat"
    assert check_mark_stop(st2, stop * 0.9, 1_700_000_100)[0] == []
    assert check_mark_stop(st1, stop * 1.01, 1_700_000_000)[0] == []


def test_buff_half_entry_then_fill(monkeypatch):
    import analyst.compute.eric_swing as es

    candles, turn_i = _weekly_oversold_then_turn()
    monkeypatch.setattr(es, "ENABLE_BUFF_HALF", True)  # 默认关闭（回测证伪），这里只测路径正确
    monkeypatch.setattr(es, "weekly_buff", lambda weekly, monthly, stop=None: (6, ("超卖", "前低/支撑", "底背离")))
    state: dict = {}
    kinds = []
    for n in range(61, turn_i + 2):
        ev, state = advance(state, CandleSeries("BTC/USDT", "1w", candles[:n]), None)
        kinds += [e.kind for e in ev]
    assert kinds.count("entry_half") == 1 and kinds.count("entry") == 1
    assert kinds.index("watch") < kinds.index("entry_half") < kinds.index("entry")
    assert state["phase"] == "long" and state["size"] == 1.0
    # 均价 = (半仓价 + 拐头周收盘)/2
    assert abs(state["entry"] - (state["entry1"] + candles[turn_i].close) / 2) < 1e-9
    assert state["stop"] < state["entry1"]


def test_eric_square_post_text():
    from analyst.monitor.square_posts import DISCLAIMER, compose_eric_square_post

    t = compose_eric_square_post(
        symbol="BTC/USDT",
        kind="weekly_entry",
        price=63750.0,
        bf_value=-42.1,
        plan={"stop": 60000.0, "tp1": 79687.5},
    )
    assert "$BTC" in t.splitlines()[0]
    assert "止损 60000" in t and "79687" in t
    assert DISCLAIMER in t and "$BTC" in t.splitlines()[-1]
    assert len(t) <= 900
    t2 = compose_eric_square_post(symbol="ETH/USDT", kind="daily_oversold", price=1800.0, bf_value=-45.0)
    assert "日线" in t2 and "$ETH" in t2
    t3 = compose_eric_square_post(
        symbol="BTC/USDT", kind="weekly_tp1", price=72998.7, bf_value=None,
        plan={"stop": 63750.0, "pnl_pct": 14.5},
    )
    assert "止盈一半" in t3 and "+14.5%" in t3 and "63750" in t3
    t4 = compose_eric_square_post(
        symbol="BTC/USDT", kind="weekly_tp2", price=65000.0, bf_value=None,
        plan={"pnl_pct": 2.0, "tp1_pnl_pct": 14.5},
    )
    assert "第一半 +14.5%" in t4 and "余仓 +2.0%" in t4
    t5 = compose_eric_square_post(symbol="ETH/USDT", kind="weekly_stop", price=1700.0, bf_value=None, plan={"pnl_pct": -12.3})
    assert "止损" in t5 and "-12.3%" in t5 and DISCLAIMER in t5
