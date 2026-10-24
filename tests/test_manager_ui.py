"""Regression checks for the arrow/Enter-only console front-end (no server calls)."""
from __future__ import annotations

import curses

from deploy.manager_ui import (BANNER, Entry, MikachanMenu, Page, banner_lines,
                               clip, is_secret, safe_setting_value)


class FakeManager:
    def __init__(self, *, on_event=None):
        self.on_event = on_event

    def status(self):
        return {"installed": True, "service": "active", "config": False,
                "branch": "v2", "commit": "a" * 40, "consistent": True}

    def config(self):
        return {"OWNER_TELEGRAM_ID": "42", "BOT_TOKEN": "(set)",
                "MAX_ACTIVE_DOWNLOADS": "2", "HTTP_PROXY": "(set)"}


class FakeWin:
    def __init__(self, width=80, height=33):
        self.width, self.height = width, height
        self.drawn = []

    def getmaxyx(self):
        return (self.height, self.width)

    def erase(self):
        self.drawn = []

    def addnstr(self, y, x, data, n, style=0):
        assert 0 <= y < self.height
        assert 0 <= x < self.width
        assert len(data) <= n
        self.drawn.append((y, x, data))

    def refresh(self):
        pass


def test_default_banner_and_custom_art_are_responsive():
    assert BANNER == "M I K A C H A N"
    custom = ("   /\\   ", "  /__\\  ")
    assert banner_lines(160, 40, custom) == custom
    assert banner_lines(80, 24, custom) == ("MIKACHAN",)
    assert banner_lines(36, 15) == ("MIKACHAN",)


def test_top_level_arrow_enter_and_back_navigation():
    ui = MikachanMenu(FakeManager)
    assert ui.page.title == "MAIN MENU"
    ui.handle_key(curses.KEY_DOWN)
    ui.handle_key(curses.KEY_DOWN)
    ui.handle_key(10)
    assert ui.page.title == "DEPLOYMENT"
    ui.handle_key(27)
    assert ui.page.title == "MAIN MENU"
    ui.handle_key(curses.KEY_UP)
    assert ui.page.selected == 1
    ui.handle_key(10)
    assert ui.page.title == "SERVICES"


def test_confirmation_cannot_run_without_exact_phrase(tmp_path, monkeypatch):
    import deploy.manager_ui as panel

    monkeypatch.setattr(panel, "N8N_MARKER", tmp_path / "unmanaged-n8n-marker")
    ui = MikachanMenu(FakeManager)
    received = []
    ui.execute = lambda *a: received.append(a)
    ui._confirm("purge")
    ui.form_value = "PURGE"
    ui.submit_form()
    assert received == []
    ui._confirm("purge")
    ui.form_value = "PURGE MIKACHAN"
    ui.submit_form()
    assert received == [("purge", None)]


def test_mikachan_uninstall_offers_independent_n8n_keep_by_default(tmp_path, monkeypatch):
    import deploy.manager_ui as panel

    marker = tmp_path / ".managed-by-n8n-installer"
    marker.write_text("owned")
    monkeypatch.setattr(panel, "N8N_MARKER", marker)
    ui = MikachanMenu(FakeManager)
    received = []
    ui.execute = lambda *args: received.append(args)
    ui._confirm("purge")
    assert ui.page.title == "n8n IS INSTALLED SEPARATELY"
    assert "Keep n8n Running" in ui.page.entries[0].label
    ui.select()
    ui.form_value = "PURGE MIKACHAN"
    ui.submit_form()
    assert received == [("purge", "keep")]


def test_removing_both_apps_needs_two_exact_confirmations(tmp_path, monkeypatch):
    import deploy.manager_ui as panel

    marker = tmp_path / ".managed-by-n8n-installer"
    marker.write_text("owned")
    monkeypatch.setattr(panel, "N8N_MARKER", marker)
    ui = MikachanMenu(FakeManager)
    received = []
    ui.execute = lambda *args: received.append(args)
    ui._confirm("remove")
    ui.page.selected = 1
    ui.select()
    ui.page.selected = 1  # permanent n8n data removal
    ui.select()
    ui.form_value = "REMOVE MIKACHAN"
    ui.submit_form()
    assert received == []
    ui.form_value = "PURGE N8N DATA"
    ui.submit_form()
    assert received == [("remove-n8n", ("remove", True))]


def test_snapshot_version_and_owner_requests_use_structured_payload():
    ui = MikachanMenu(FakeManager)
    received = []
    ui.execute = lambda *a: received.append(a)
    ui._choose_snapshot("20261019T010101Z-1234abcd")
    ui.form_value = "RESTORE"
    ui.submit_form()
    assert received == [("restore", "20261019T010101Z-1234abcd")]
    ui._choose_branch({"branch": "v3", "commit": "a" * 40})
    ui.select()
    ui.form_value = "SWITCH"
    ui.submit_form()
    assert received[-1] == ("switch", ("v3", None))
    ui._choose_owner_request({"token": "A" * 28, "target_id": 24})
    ui.form_value = "APPROVE OWNER"
    ui.submit_form()
    assert received[-1] == ("owner-approve", ("A" * 28, 24))


def test_secret_values_masked_and_nonsecret_visible():
    assert is_secret("BOT_TOKEN")
    assert is_secret("FREESTYLE_API_KEY")
    assert is_secret("HTTPS_PROXY")
    assert safe_setting_value("BOT_TOKEN", "(set)") == "configured"
    assert safe_setting_value("OWNER_TELEGRAM_ID", "42") == "42"
    ui = MikachanMenu(FakeManager)
    ui._open_categories()
    ui._open_category("Identity & Access")
    assert any("BOT_TOKEN  :  configured" in e.label for e in ui.page.entries)
    assert not any("(set)" in e.label for e in ui.page.entries)
    ui._choose_setting("BOT_TOKEN")
    assert ui.form_secret
    assert ui.form_value == ""


def test_existing_setup_requires_explicit_confirm_before_any_secrets():
    class Existing(FakeManager):
        def status(self):
            return {**super().status(), "config": True}
    ui = MikachanMenu(Existing)
    ui.start_setup()
    assert ui.form_label == "Type SETUP to reconfigure"
    ui.form_value = "wrong"
    ui.submit_form()
    assert ui.form_label is None
    ui.start_setup()
    ui.form_value = "SETUP"
    ui.submit_form()
    assert "SETUP 1/6" in ui.form_label
    assert ui.form_secret


def test_form_enter_submission_backspace_and_escape():
    ui = MikachanMenu(FakeManager)
    received = []
    ui.open_form("Edit", lambda value: received.append(value))
    for k in map(ord, "abc"):
        ui.handle_key(k)
    ui.handle_key(curses.KEY_BACKSPACE)
    ui.handle_key(10)
    assert received == ["ab"]
    ui.open_form("Edit", lambda value: received.append(value))
    ui.handle_key(27)
    assert received == ["ab"]


def test_small_wide_and_shallow_screens_do_not_overflow():
    ui = MikachanMenu(FakeManager)
    for width, height in [(31, 11), (80, 24), (100, 30), (160, 38)]:
        w = FakeWin(width, height)
        ui.draw(w)
        assert all(0 <= y < height and 0 <= x < width for y, x, data in w.drawn)
        assert any("MAIN MENU" in data for _, _, data in w.drawn)


def test_result_view_scrolling_back_and_menu_state():
    ui = MikachanMenu(FakeManager)
    ui.push(Page("RESULT", [Entry("Back", "back")], kind="report",
                 text=[str(x) for x in range(100)]))
    ui.handle_key(curses.KEY_DOWN)
    assert ui.page.offset == 1
    ui.handle_key(10)
    assert ui.page.title == "MAIN MENU"


def test_clip_removes_control_codes():
    assert "\x1b" not in clip("\x1b[31mERROR\x1b[0m", 30)
    assert clip("a" * 100, 10).endswith("…")


def test_async_version_picker_builds_selection_from_backend_not_hardcoded():
    class WithVersions(FakeManager):
        def versions(self, *, refresh=False):
            assert refresh
            return [{"branch": "v2", "commit": "b" * 40},
                    {"branch": "v3", "commit": "c" * 40}]

    ui = MikachanMenu(WithVersions)
    ui.execute("versions")
    assert ui.worker is not None
    ui.worker.join(timeout=5)
    ui.drain_events()
    assert ui.page.title == "AVAILABLE VERSIONS"
    assert [e.action for e in ui.page.entries] == ["branch", "branch", "back"]
    assert ui.page.entries[1].payload["branch"] == "v3"


def test_asynchronous_errors_become_clear_result_without_crashing_menu():
    class BadManager(FakeManager):
        def versions(self, *, refresh=False):
            raise ValueError("Unavailable branch list")

    ui = MikachanMenu(BadManager)
    ui.execute("versions")
    assert ui.worker is not None
    ui.worker.join(timeout=5)
    ui.drain_events()
    assert ui.page.title == "OPERATION FAILED"
    assert any("ValueError" in line for line in ui.page.text)
    ui.handle_key(10)
    assert ui.page.title == "MAIN MENU"


def test_noninteractive_use_exits_safely_without_terminal(monkeypatch):
    from deploy import manager_ui

    monkeypatch.setattr(manager_ui.os, "isatty", lambda fd: False)
    import pytest
    with pytest.raises(SystemExit, match="Interactive terminal required"):
        manager_ui.main()
