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


def test_analyze_market_free_only_requires_any_free_key(monkeypatch):
    from analyst.config import Settings
    import analyst.llm.analyst as mod

    s = Settings.model_construct(
        groq_api_key="",
        cerebras_api_key="",
        gemini_api_key="",
        openrouter_api_key="",
        sambanova_api_key="",
        llm_try_groq_first=True,
        llm_free_order="nvidia,groq,cerebras,openrouter,sambanova,gemini",
        nvidia_api_key="",
        llm_provider="deepseek",
        deepseek_api_key="sk-paid",
    )
    monkeypatch.setattr(mod, "get_settings", lambda: s)
    try:
        mod.analyze_market({}, {}, free_only=True)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "免费" in str(e)


def test_list_free_endpoints_order(monkeypatch):
    from analyst.config import Settings
    import analyst.llm.analyst as mod

    s = Settings.model_construct(
        groq_api_key="gsk-x",
        cerebras_api_key="csk-x",
        gemini_api_key="",
        openrouter_api_key="or-x",
        sambanova_api_key="",
        llm_try_groq_first=True,
        llm_free_order="cerebras,groq,openrouter",
        groq_model="llama-3.3-70b-versatile",
        groq_base_url="https://api.groq.com/openai/v1",
        cerebras_model="llama-3.3-70b",
        cerebras_base_url="https://api.cerebras.ai/v1",
        openrouter_model="meta-llama/llama-3.3-70b-instruct:free",
        openrouter_base_url="https://openrouter.ai/api/v1",
    )
    names = [e["name"] for e in mod.list_free_endpoints(s)]
    assert names == ["cerebras", "groq", "openrouter"]

