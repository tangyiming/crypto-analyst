"""品种列表：DEFAULT_SYMBOLS 为唯一源，其它空则跟随。"""

from analyst.config import Settings


def test_empty_overlays_follow_default_symbols():
    s = Settings.model_construct(
        default_symbols="BTC/USDT,ETH/USDT,BNB/USDT,UNI/USDT",
        monitor_daemon_symbols="",
        monitor_cycle_symbols="",
        monitor_xs_symbols="",
        monitor_carry_symbols="",
    )
    assert s.symbols_list == ["BTC/USDT", "ETH/USDT", "BNB/USDT", "UNI/USDT"]
    assert s.daemon_symbols_list == s.symbols_list
    assert s.cycle_symbols_set is None
    assert s.carry_symbols_list == s.symbols_list


def test_explicit_carry_and_daemon_override():
    s = Settings.model_construct(
        default_symbols="BTC/USDT,ETH/USDT",
        monitor_daemon_symbols="BTC/USDT,UNI/USDT",
        monitor_carry_symbols="ETH/USDT",
    )
    assert s.daemon_symbols_list == ["BTC/USDT", "UNI/USDT"]
    assert s.carry_symbols_list == ["ETH/USDT"]
