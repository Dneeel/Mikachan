"""The compact help card should describe only connected optional features."""

from mikachan.bot import help_text


def test_help_card_keeps_audio_and_ai_conditional():
    base = help_text(relay_enabled=False, group=False, closing="Ready when you are.")
    assert "<b>MIKACHAN</b>" in base
    assert "<code>/music</code>" in base and "<code>/pod</code>" in base
    assert "/mika" not in base and "/tts" not in base
    assert "GROUP MANAGEMENT" not in base
    assert len(base) < 4096

    connected = help_text(relay_enabled=True, group=True, closing="Ready when you are.")
    assert "<code>/mika</code>" in connected
    assert "Reply to continue" in connected
    assert "<code>/groupstatus</code>" in connected
    assert len(connected) < 4096
