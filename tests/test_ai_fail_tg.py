"""AI 调用失败的 Telegram 只推一次。"""

from analyst.monitor.notifier import (
    claim_ai_fail_tg_alert,
    note_ai_call_ok,
    reset_ai_fail_tg_alert,
)


def setup_function() -> None:
    reset_ai_fail_tg_alert()


def test_claim_ai_fail_tg_only_once_until_ok():
    assert claim_ai_fail_tg_alert() is True
    assert claim_ai_fail_tg_alert() is False
    assert claim_ai_fail_tg_alert() is False
    note_ai_call_ok()
    assert claim_ai_fail_tg_alert() is True
    assert claim_ai_fail_tg_alert() is False
