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
