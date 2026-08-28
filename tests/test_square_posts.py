"""币安广场短评：文案与冷却。"""

from analyst.compute.jack_levels import JackLevels
from analyst.compute.jack_regime import JackRegime
from analyst.monitor.square_posts import compose_jack_square_post, square_symbols_set


def _sample():
    jack = JackLevels(
        swing_high=70000,
        swing_low=60000,
        rebound_382=63820,
        rebound_500=65000,
        rebound_618=66180,
        retr_382=66180,
        retr_618=63820,
        boll_mid=64000,
        confluence_382=False,
        confluence_618=False,
        daily_bias="up",
        defense_level=62000,
        htf_ready=True,
        horizon="swing",
        touch_level=67000,
        touch_count=2,
        rs_note="—",
        summary_line="测",
    )
    reg = JackRegime(
        regime="strong_trend",
        regime_zh="强势盘",
        trade_side="long",
        seed_style="market",
        add_mode="breakout",
        tp_style="new_high",
        defense_broken=False,
        continuation_intact=True,
        nearest_support=62000,
        nearest_resistance=67000,
        prev_day_high=None,
        prev_day_low=None,
        intraday_high=None,
        intraday_low=None,
        tp_intraday_50=None,
        tp_intraday_618=None,
        ema12h_6=None,
        spike_stop_recent=False,
        playbook_line="市价小头仓 + 突破加仓",
        summary_line="测",
    )
    return jack, reg


def test_compose_jack_square_post_has_direction_and_levels():
    jack, reg = _sample()
    text = compose_jack_square_post(
        symbol="BTC/USDT",
        timeframe="4h",
        price=65000.0,
        jack=jack,
        regime=reg,
        compact=False,
    )
    assert "$BTC" in text
    assert "$BTC" in text  # 标签行已按用户要求去掉，只保留首行 cashtag
    assert "#crypto" not in text
    assert "#合约" not in text
    assert "看涨" in text
    assert "预测" in text
    assert "强势盘" in text
    assert "偏多" in text
    assert "62000" in text or "62000.00" in text
    assert "非投资建议" not in text  # 已按用户要求去掉免责声明


def test_compose_short_prediction_and_tags():
    jack, reg = _sample()
    reg.trade_side = "short"
    reg.regime = "weak_trend"
    reg.regime_zh = "弱势盘"
    text = compose_jack_square_post(
        symbol="ETH/USDT",
        timeframe="1h",
        price=2400.0,
        jack=jack,
        regime=reg,
        compact=False,
    )
    assert "看跌" in text
    assert "$ETH" in text
    assert "$ETH" in text
    assert "#crypto" not in text
    assert "预测" in text


def test_square_default_symbols_include_sol_aave():
    symbols = square_symbols_set()
    assert "SOL/USDT" in symbols
    assert "AAVE/USDT" in symbols
    assert "BTC/USDT" in symbols


def test_round_barrier_far_from_price_is_skipped():
    """SOL 102 时下一个整数关口是 150，离价 47%，不该被写成首压。"""
    from types import SimpleNamespace
    from analyst.monitor.square_posts import _round_near

    reg = SimpleNamespace(round_level=150.0)
    assert not _round_near(reg, 102.0, 0.08)
    assert not _round_near(reg, 102.0, 0.15)
    assert _round_near(SimpleNamespace(round_level=100.0), 97.0, 0.08)
    assert not _round_near(SimpleNamespace(round_level=None), 97.0, 0.08)


def test_polish_number_check_tolerates_rounding():
    from analyst.monitor.square_posts import _fmt_price, _missing_numbers, _numbers

    assert _fmt_price(106.7532) == "106.75"
    assert _fmt_price(2420.0) == "2420.00"
    assert _fmt_price(1.23456) == "1.235"
    assert _fmt_price(0.123456) == "0.123456"
    want = _numbers("上轨 101.5747；近阻力 106.7532；现价 102.05")
    got = _numbers("上轨 101.57，近阻力 106.75，现价 102.05")
    assert _missing_numbers(want, got) == set()
    got_bad = _numbers("上轨 101.57，现价 102.05")
    assert _missing_numbers(want, got_bad) == {"106.753"}


def test_post_levels_filtered_by_price_side():
    from types import SimpleNamespace
    from analyst.monitor.square_posts import _clip_sentence, _numbers, _post_levels

    jack = SimpleNamespace(defense_level=77704.0, rebound_382=69660.0, rebound_618=74094.0, touch_level=None)
    reg = SimpleNamespace(trade_side="long", nearest_support=77704.0, nearest_resistance=80499.9, ext_150=81949.85, ext_1618=82292.0)
    defense, near, target = _post_levels(reg, jack, 79054.0)
    assert defense == 77704.0 and near == 80499.9 and target == 81949.85  # 锁点反弹位在现价下方，不能当近压/目标
    reg_s = SimpleNamespace(trade_side="short", nearest_support=2431.0, nearest_resistance=2566.0, ext_150=None, ext_1618=None)
    d, n, t = _post_levels(reg_s, SimpleNamespace(defense_level=2566.0, rebound_382=2400.0, rebound_618=2350.0, touch_level=None), 2505.0)
    assert d == 2566.0 and n == 2431.0 and t == 2350.0
    assert _clip_sentence("第一句。第二句；第三句没完", 12) == "第一句。第二句；"
    assert _numbers("卖出 1/2，第2次，0.50–0.618，止损 2004.53，+25.0%") == {"2004.53", "25"}


def test_waist_note_distinguishes_below_and_near():
    from types import SimpleNamespace
    from analyst.monitor.square_posts import _waist_note

    reg = SimpleNamespace(waist_line=2478.84, below_waist=True)
    assert "贴着" in _waist_note(reg, 2505.66)
    assert "在其下" in _waist_note(reg, 2400.0)
    assert _waist_note(SimpleNamespace(waist_line=2478.84, below_waist=False), 2600.0) == ""


def test_indicator_block_filters_far_levels_by_timeframe():
    from types import SimpleNamespace
    from analyst.monitor.square_posts import indicator_block

    reg = SimpleNamespace(
        trade_side="long", macd_8h_decel=False, macd_12h_decel=False, weekly_macd_zero=False, accel_2d=False,
        golden_3d=False, golden_5d=False, hollow_daily=False,
        boll_4h_lower=77260.0, boll_4h_mid=78755.0, boll_4h_upper=80250.0, boll_12h_mid=74428.0,
        ema12h_6=78431.0, boll_mid_3d=65734.0, boll_mid_5d=66276.0, ema5d_6=70980.0,
        pullback_50=79050.0, pullback_618=78708.0, nearest_support=77704.0, nearest_resistance=80500.0,
        ext_150=81950.0, ext_1618=82292.0, round_level=80000.0, barrier_below=(76800.0, 78400.0), barrier_above=(83200.0, 84800.0),
        waist_line=63104.0, below_waist=False, cycle_low=57758.6, cycle_high=126208.5, cycle_382=83906.0, cycle_500=91984.0, cycle_618=100061.0,
        monthly_boll_mid=88259.0, weekly_boll_upper=84398.0,
    )
    h4 = "\n".join(indicator_block(reg, None, price=79054.0, timeframe="4h"))
    assert "65734" not in h4 and "66276" not in h4 and "70980" not in h4  # −17%/−10%：4h 帖不写
    assert "91984" not in h4 and "100061" not in h4 and "83906" in h4  # 梯子只留最近一档
    assert "88259" not in h4 and "84398" in h4  # 月线中轨 +12% 超门槛，周线上轨 +7% 保留
    assert "上方 20%" in h4 and "牛市结构没坏" in h4  # 腰斩线远 → 定性
    assert "78431" in h4 and "74428" in h4 and "80500" in h4
    d1 = "\n".join(indicator_block(reg, None, price=79054.0, timeframe="1d"))
    assert "65734" in d1 and "88259" in d1 and "91984" in d1  # 日线门槛 25%：这些都回来了
    assert "100061" not in d1  # +27% 仍超


def test_enforce_square_anchors_restores_cashtag_and_cta():
    from analyst.monitor.square_posts import _enforce_square_anchors

    original = (
        "📈 看涨 $BTC\n现价 65000.00\n点 $BTC 看永续，站稳 62000.00 可跟，上看 67000.00。"
    )
    polished = "BTC现在65000，4h多头，防守62000，上看67000。"
    out = _enforce_square_anchors(polished, original)
    assert "$BTC" in out
    assert "点 $BTC 看永续" in out
    assert "62000" in out


def test_polish_enforces_anchors_when_llm_drops_dollar(monkeypatch):
    import analyst.llm.chat as chat
    import analyst.monitor.square_posts as sp
    from analyst.monitor.square_posts import compose_jack_square_post, polish_square_text
    from tests.test_square_posts import _sample

    jack, reg = _sample()
    raw = compose_jack_square_post(
        symbol="BTC/USDT", timeframe="4h", price=65000.0, jack=jack, regime=reg, compact=True
    )

    class _Msg:
        def __init__(self, c):
            self.content = c

    class _Choice:
        def __init__(self, c):
            self.message = _Msg(c)

    class _Resp:
        def __init__(self, c):
            self.choices = [_Choice(c)]

    class _Client:
        def __init__(self, reply):
            self._reply = reply

        class _Chat:
            def __init__(self, outer):
                self._o = outer

            class _Comp:
                def __init__(self, outer):
                    self._o = outer

                def create(self, **kw):
                    return _Resp(self._o._reply)

            @property
            def completions(self):
                return _Client._Chat._Comp(self._o)

        @property
        def chat(self):
            return _Client._Chat(self)

    bad = (
        "BTC现在65000，4h还是多头。防守62000，上看67000。"
        "小仓试，突破再加，别追尾巴。"
    )
    monkeypatch.setattr(chat, "_iter_chat_clients", lambda s: iter([(_Client(bad), "m", "fake")]))

    class _S:
        square_post_ai_polish = True

    out, src = polish_square_text(raw, settings=_S(), compact=True)
    assert src.startswith("llm:fake")
    assert "$BTC" in out
    assert "点 $BTC" in out


def test_compose_jack_compact_has_cta():
    jack, reg = _sample()
    text = compose_jack_square_post(
        symbol="BTC/USDT",
        timeframe="4h",
        price=65000.0,
        jack=jack,
        regime=reg,
        compact=True,
    )
    assert "$BTC" in text
    assert "看涨" in text
    assert "点 $BTC" in text
    assert "预测" not in text
    assert "指标怎么看" not in text


def test_compose_jack_setup_post():
    from analyst.monitor.square_posts import compose_jack_setup_post

    jack, reg = _sample()
    text = compose_jack_setup_post(
        symbol="SOL/USDT",
        timeframe="4h",
        price=101.0,
        jack=jack,
        regime=reg,
        flag_labels=["3日金叉", "大小周期共振，可市价冲"],
    )
    assert "$SOL" in text
    assert "打法" in text
    assert "点 $SOL" in text


def test_compose_level_touch_post():
    from analyst.monitor.square_posts import compose_level_touch_post

    jack, reg = _sample()
    text = compose_level_touch_post(
        symbol="ETH/USDT",
        timeframe="4h",
        price=2410.0,
        level=2400.0,
        kind="support",
        jack=jack,
        regime=reg,
    )
    assert "$ETH" in text
    assert "2400" in text
    assert "点 $ETH" in text


def test_compose_daily_recap_post():
    from analyst.monitor.square_posts import compose_daily_recap_post

    text = compose_daily_recap_post(
        facts={"market": {"regime": "bull", "btc_price": 65000, "btc_vs_ema200d_pct": 5.2}},
        movers=[("SOL/USDT", 101.0, 6.5), ("BTC/USDT", 65000.0, 1.2)],
    )
    assert "$BTC" in text
    assert "$ETH" in text
    assert "SOL" in text
