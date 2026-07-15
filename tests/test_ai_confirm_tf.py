"""盯盘周期 → AI 分析周期映射。"""

from analyst.config import Settings
from analyst.training.session import map_monitor_tf_to_ai_tf


def test_map_15m_to_1h():
    assert map_monitor_tf_to_ai_tf("15m") == "1h"


def test_map_allowed_passthrough():
    for tf in ("1d", "4h", "1h", "30m"):
        assert map_monitor_tf_to_ai_tf(tf) == tf


def test_map_unknown_to_4h():
    assert map_monitor_tf_to_ai_tf("5m") == "4h"
    assert map_monitor_tf_to_ai_tf("") == "4h"


def test_tg_trade_rules_parsing():
    s = Settings.model_construct(monitor_tg_trade_rules="ai_plan,cycle_switch")
    rules = s.tg_trade_rules_set
    assert rules == {"ai_plan", "cycle_switch"}
