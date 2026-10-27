"""Navigation and arithmetic are deterministic and do not call external services."""

from mikachan.command_guide import commands, navigation, page
from mikachan.quick_math import calculate


def test_command_order_matches_directory_and_optional_ai():
    names = [command.command for command in commands(relay_enabled=True,
                                                      tts_enabled=True, stt_enabled=False)]
    assert names[:5] == ["start", "help", "music", "pod", "lyric"]
    assert "mika" in names and "tts" in names and "stt" in names
    disconnected = [command.command for command in commands(
        relay_enabled=False, tts_enabled=False, stt_enabled=False)]
    assert "tts" in disconnected and "stt" in disconnected and "edit" in disconnected
    assert "<code>/music</code>" in page("music", relay_enabled=False)
    assert "<code>/pod</code>" in page("music", relay_enabled=False)
    assert "<code>/mika</code>" not in page("home", relay_enabled=False)
    assert "<code>/mika</code>" in page("ai", relay_enabled=True)
    assert "guide:admin" not in str(navigation("home", relay_enabled=False,
                                                group=True, admin=False))


def test_quick_math_accepts_arithmetic_but_not_code_or_chat():
    assert calculate("12*7") == "84"
    assert calculate("99+11") == "110"
    assert calculate("11-9") == "2"
    assert calculate("(12+3)*4") == "60"
    assert calculate("5/2") == "2.5"
    assert calculate("2**1000000") is None
    assert calculate("__import__('os').system('id')") is None
    assert calculate("میکا 12*7") is None
    assert calculate("1/0") is None
