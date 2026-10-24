"""The compact help card should describe only connected optional features."""

from mikachan.bot import help_text


def test_help_card_keeps_audio_and_ai_conditional():
    base = help_text(relay_enabled=False, group=False, closing="Ready when you are.")
    assert "<b>MIKACHAN</b>" in base
    assert "choose <b>Yes</b> or <b>No</b>" in base
    assert "/mika" not in base and "/tts" not in base
    assert "Admins:" not in base
    assert len(base) < 4096

    connected = help_text(relay_enabled=True, group=True, closing="Ready when you are.")
    assert "/mika QUESTION" in connected
    assert "Reply to an AI answer" in connected
    assert "Admins:" in connected
    assert len(connected) < 4096
