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
    s = Settings.model_construct(monitor_tg_trade_rules="ai_plan")
    rules = s.tg_trade_rules_set
    assert rules == {"ai_plan"}


def test_analyze_market_free_only_requires_groq(monkeypatch):
    from analyst.config import Settings
    import analyst.llm.analyst as mod

    s = Settings.model_construct(
        groq_api_key="",
        llm_try_groq_first=True,
        llm_provider="deepseek",
        deepseek_api_key="sk-paid",
    )
    monkeypatch.setattr(mod, "get_settings", lambda: s)
    try:
        mod.analyze_market({}, {}, free_only=True)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "GROQ" in str(e) or "免费" in str(e)

