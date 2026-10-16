import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mika_nexus.security import allowed, clean_url, collection, links, source, validate_dns
from mika_nexus.store import Store


class SecurityTests(unittest.TestCase):
    def test_url_policy(self):
        for url in ("http://127.0.0.1/x", "http://169.254.169.254/x", "http://[::1]/", "http://localhost/x", "https://a.com:8080/x", "https://x:y@a.com/x", "file:///tmp/x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                clean_url(url)
        self.assertEqual(source(clean_url("https://youtube.com.evil.example/x")), "direct")
        self.assertEqual(source(clean_url("https://x.com/user/status/1")), "twitter")
        self.assertFalse(collection("https://youtu.be/abc"))
        self.assertTrue(collection("https://youtube.com/playlist?list=abc"))

    def test_dns_mixed(self):
        answers = [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("10.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=answers), self.assertRaises(ValueError):
            validate_dns("https://example.com/file")

    def test_links_and_auth(self):
        urls, extra = links("https://a.com/x https://b.com/y https://c.com/z", 2)
        self.assertEqual((len(urls), extra), (2, 1))
        self.assertFalse(allowed(2, -100, "supergroup", {1, 2}, True))
        self.assertTrue(allowed(2, 2, "private", {1, 2}, True))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(Path(self.tmp.name) / "n.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_confirmation_atomic_and_idempotent(self):
        cid = self.db.confirm_create(10, 20, 30, 0, "https://a.com/x", 900)
        self.assertEqual(cid, self.db.confirm_create(10, 20, 30, 0, "https://a.com/x", 900))
        self.db.claim_prompt(cid)
        self.db.set_prompt(cid, 40)
        self.assertEqual(self.db.decision(cid, 21, 10, 40, True)[0], "FORBIDDEN")
        state, jid = self.db.decision(cid, 20, 10, 40, True)
        self.assertEqual(state, "ACCEPTED")
        self.assertEqual(self.db.decision(cid, 20, 10, 40, True), ("ALREADY", None))
        self.assertEqual(len(self.db.jobs(20)), 1)
        self.assertEqual(self.db.job(jid)["state"], "QUEUED")

    def test_reject_expire_and_recovery(self):
        cid = self.db.confirm_create(10, 20, 30, 0, "https://a.com/x", 900)
        self.db.claim_prompt(cid)
        self.db.set_prompt(cid, 40)
        self.assertEqual(self.db.decision(cid, 20, 10, 40, False), ("REJECTED", None))
        self.assertEqual(self.db.jobs(20), [])
        cid2 = self.db.confirm_create(10, 20, 31, 0, "https://a.com/x", 1)
        self.db.claim_prompt(cid2)
        self.db.set_prompt(cid2, 41)
        self.db.db.execute("UPDATE confirmations SET expires=0 WHERE id=?", (cid2,))
        self.assertEqual(self.db.decision(cid2, 20, 10, 41, True), ("EXPIRED", None))
        cid3 = self.db.confirm_create(10, 20, 32, 0, "https://a.com/x", 900)
        self.db.claim_prompt(cid3)
        self.db.set_prompt(cid3, 42)
        _, jid = self.db.decision(cid3, 20, 10, 42, True)
        self.db.transition(jid, "QUEUED", "DELIVERING")
        self.db.recover()
        self.assertEqual(self.db.job(jid)["state"], "DELIVERY_UNCERTAIN")

    def test_search_owner_generation(self):
        sid = self.db.create_search(20, 10, "Radiohead", {"track"}, {"apple"})
        self.assertIsNone(self.db.search(sid, 21, 10))
        self.db.save_results(sid, 0, [{"kind": "track", "title": "Creep"}])
        self.assertEqual(self.db.result(sid, 0, 0)["title"], "Creep")
        self.assertEqual(self.db.update_search(sid, {"track", "album"}, {"apple"}), 1)
        self.assertIsNone(self.db.result(sid, 1, 0))


if __name__ == "__main__":
    unittest.main()

class AdminTests(unittest.TestCase):
    def test_owner_managed_allowlist(self):
        with tempfile.TemporaryDirectory() as d:
            db = Store(Path(d) / "n.sqlite")
            db.seed_access({1, 2})
            self.assertEqual(db.allowlist(), {1, 2})
            token = db.propose_access(1, 10, 3, "add")
            db.bind_admin_prompt(token, 50)
            self.assertEqual(db.apply_access(token, 2, 1, 10, 50), "expired or unauthorized")
            self.assertEqual(db.allowlist(), {1, 2})
            token = db.propose_access(1, 10, 3, "add")
            db.bind_admin_prompt(token, 51)
            self.assertIn("complete", db.apply_access(token, 1, 1, 10, 51))
            self.assertEqual(db.allowlist(), {1, 2, 3})
            token = db.propose_access(1, 10, 1, "remove")
            db.bind_admin_prompt(token, 52)
            self.assertEqual(db.apply_access(token, 1, 1, 10, 52), "cannot remove owner")
            self.assertIn(1, db.allowlist())

class CollectionTests(unittest.TestCase):
    def test_selection_is_idempotent_and_owned(self):
        with tempfile.TemporaryDirectory() as d:
            db = Store(Path(d) / "n.sqlite")
            cid = db.create_collection(20, 10, "A", [{"title": "One", "url": "https://a.com/1"}])
            self.assertEqual(db.select_collection(cid, 21, 10, 0), (None, False))
            jid, fresh = db.select_collection(cid, 20, 10, 0)
            self.assertTrue(fresh)
            self.assertEqual(db.select_collection(cid, 20, 10, 0), (jid, False))
            self.assertEqual(len(db.jobs(20)), 1)

class QueueTests(unittest.TestCase):
    def test_one_oldest_job_per_user(self):
        with tempfile.TemporaryDirectory() as d:
            db = Store(Path(d) / "n.sqlite")
            for i in range(35):
                db.create_job(1, 1, f"https://a.com/{i}")
            db.create_job(2, 2, "https://a.com/other")
            rows = db.next_jobs()
            self.assertEqual({r["user_id"] for r in rows}, {1, 2})
            self.assertEqual(len(rows), 2)

class RateTests(unittest.TestCase):
    def test_rate_limit(self):
        with tempfile.TemporaryDirectory() as d:
            db = Store(Path(d) / "n.sqlite")
            self.assertTrue(db.rate(1, "links", 2))
            self.assertTrue(db.rate(1, "links", 2))
            self.assertFalse(db.rate(1, "links", 2))
            self.assertTrue(db.rate(2, "links", 2))
