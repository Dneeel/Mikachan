import sqlite3
from types import SimpleNamespace

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
