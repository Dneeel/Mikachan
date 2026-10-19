"""Offline checks for opt-in MTProto routing and input boundaries."""

import sqlite3

import pytest

from mikachan.access import AccessRegistry
from mikachan.userbot import extract_download_url, is_currency_query


def test_file_link_parser_accepts_public_https_only():
    answer = ("Your Link Generated!\nDownload: "
              "https://files.example.org/stream/317555?hash=378ba2&d=true\n"
              "All links expire after 24 hours")
    assert extract_download_url(answer) == \
        "https://files.example.org/stream/317555?hash=378ba2&d=true"
    for bad in ("Download: http://127.0.0.1/file", "Download: http://localhost/file",
                "No link yet", "Download: javascript:alert(1)"):
        with pytest.raises(ValueError):
            extract_download_url(bad)


@pytest.mark.parametrize("query", ["usd", "101 usd", "2300 jpy to usd", "19 eur to usdt",
                                   "دلار به ین", "120000 دینار عراق به یورو", "12 سکه به دلار"])
def test_currency_query_matches_explicit_requests(query):
    assert is_currency_query(query)


@pytest.mark.parametrize("query", ["I have usd", "https://example.com", "btc to something",
                                   "send all messages", "usd\npassword"])
def test_currency_query_ignores_unrelated_text(query):
    assert not is_currency_query(query)


def test_owner_controls_registered_private_chat():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE access(user_id INTEGER PRIMARY KEY);"
                     "CREATE TABLE allowed_groups(chat_id INTEGER PRIMARY KEY);")
    registry = AccessRegistry(db, 1)
    registry.register(1, 2, "Second account")
    assert not registry.can_access(2, 2, "private")
    registry.set_private(1, 2, True)
    assert registry.can_access(2, 2, "private")
    assert not registry.can_access(2, 3, "private")
    registry.set_private(1, 2, False)
    assert not registry.can_access(2, 2, "private")
