import sqlite3
import time
from types import SimpleNamespace

import pytest

from mikachan.intelligence import Intelligence


class Access:
    def groups(self):
        return {-100}

    def role(self, user):
        return "OWNER" if user == 1 else None


def test_observed_words_are_exact_group_scoped_and_export_escaped():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    intel = Intelligence(db, Access())
    category = intel.add_category("Good words")
    term = intel.add_term(category, "کره جنوبی")
    message = SimpleNamespace(chat=SimpleNamespace(id=-100),
        from_user=SimpleNamespace(id=5), message_id=11,
        text="کره جنوبی و کره جنوبی <script>", caption=None)
    intel.observe(message)
    intel.observe(message)
    assert db.execute("SELECT COUNT(*) FROM intel_hits WHERE term_id=?", (term,)).fetchone()[0] == 2
    message.chat.id = -200
    intel.observe(message)
    assert db.execute("SELECT COUNT(*) FROM intel_hits").fetchone()[0] == 2
    content, ext = intel.export(term, "h")
    assert ext == "html" and b"&lt;script&gt;" in content and b"<script>" not in content
    assert b"MIKACHAN / INTELLIGENCE DESK" in content
    assert b"backdrop-filter:blur" in content


@pytest.mark.asyncio
async def test_category_accepts_multiple_words_without_reply():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    intel = Intelligence(db, Access())
    intel.pending[1] = ("category", "secret", time.time() + 600)
    replies = []

    async def reply(text, **kwargs):
        replies.append((text, kwargs))

    message = SimpleNamespace(from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(type="private"), text="History", reply=reply)
    assert await intel.reply(message)
    assert intel.pending[1][0].startswith("word:")
    for word in ("first word", "second word"):
        message.text = word
        assert await intel.reply(message)
    assert [row[0] for row in db.execute("SELECT phrase FROM intel_terms ORDER BY id")] == [
        "first word", "second word"]
    assert len(replies) == 3
    assert all(item[1]["reply_markup"].inline_keyboard[0][0].text == "✅ Done"
               for item in replies)
