"""Actor-bound Telegram navigation for source-backed content."""

import datetime as dt
import html
import json
import random
import secrets
import sqlite3
import time
from dataclasses import asdict
from typing import Any, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .content_providers import ContentError, ContentProviders, Item

COMMANDS = frozenset({"/pishi", "/phishi", "/earth", "/art", "/xotd", "/quote", "/wiki"})
DAILY = {"picture": "🖼 Picture of the Day", "media": "🎬 Media of the Day",
         "word": "📖 Word of the Day", "featured": "⭐ Featured Article",
         "dyk": "💡 Did You Know?", "events": "🕰 On This Day",
         "births": "🎂 Notable Births", "deaths": "🕯 Notable Deaths",
         "news": "📰 In the News", "mostread": "📈 Most Read Today"}


def _markup(token: str, rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, url=data) if data.startswith("https://") else
        InlineKeyboardButton(text=label, callback_data=f"ct:{token}:{data}")
        for label, data in row] for row in rows])


class ContentUI:
    def __init__(self, db: sqlite3.Connection, bot: Bot,
                 providers: ContentProviders | None = None, *, enabled: bool = True,
                 authorize: Callable[[int, int], bool] | None = None):
        self.db, self.bot = db, bot
        self.providers = providers or ContentProviders()
        self.enabled = enabled
        self.authorize = authorize or (lambda actor, chat: True)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS content_sessions (
          token TEXT PRIMARY KEY, actor_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          message_id INTEGER, state_json TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS content_api_credentials (
          provider TEXT PRIMARY KEY, api_key TEXT NOT NULL, updated REAL NOT NULL);
        """)

    def _session(self, token: str, actor: int, chat: int, message: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM content_sessions WHERE token=? AND actor_id=? "
            "AND chat_id=? AND message_id=? AND expires>?",
            (token, actor, chat, message, time.time())).fetchone()

    def source_api_key(self, provider: str) -> str:
        if provider not in {"thecatapi", "nasa", "navasan"}:
            raise ContentError("Unknown source API")
        row = self.db.execute("SELECT api_key FROM content_api_credentials "
                              "WHERE provider=?", (provider,)).fetchone()
        return str(row[0]) if row else ""

    def set_source_api_key(self, provider: str, key: str) -> None:
        self.source_api_key(provider)
        if not 8 <= len(key) <= 256 or not key.isascii() or any(c.isspace() for c in key):
            raise ContentError("API key must be 8–256 non-space ASCII characters")
        self.db.execute("INSERT INTO content_api_credentials VALUES (?,?,?) "
                        "ON CONFLICT(provider) DO UPDATE SET api_key=excluded.api_key,"
                        "updated=excluded.updated", (provider, key, time.time()))

    def remove_source_api_key(self, provider: str) -> None:
        self.source_api_key(provider)
        self.db.execute("DELETE FROM content_api_credentials WHERE provider=?", (provider,))

    def cat_api_key(self) -> str:
        return self.source_api_key("thecatapi")

    def set_cat_api_key(self, key: str) -> None:
        self.set_source_api_key("thecatapi", key)

    def remove_cat_api_key(self) -> None:
        self.remove_source_api_key("thecatapi")

    async def _cat(self, state: dict[str, Any]) -> None:
        key = self.cat_api_key()
        sources = [("thecatapi", key), ("cataas", "")] if key else [("cataas", "")]
        prior = (state.get("item") or {}).get("identifier")
        failure = "Cat photo sources are unavailable right now. Try again shortly."
        for provider, credential in sources:
            try:
                for _ in range(2):
                    item = await self.providers.cat_candidate(provider, credential)
                    if item.identifier != prior:
                        state.update(view="item", item=asdict(item))
                        return
                state.update(view="item", item=asdict(item))
                return
            except ContentError:
                continue
        raise ContentError(failure)

    async def command(self, message: Message, command: str, arg: str) -> bool:
        if command not in COMMANDS:
            return False
        if not self.enabled or not message.from_user:
            return True
        self.db.execute("DELETE FROM content_sessions WHERE expires<?", (time.time(),))
        state: dict[str, Any] = {"cmd": command, "view": "home",
                                 "lang": "fa" if command == "/quote" else "en",
                                 "history": [], "day": dt.datetime.now(dt.UTC).date().isoformat()}
        token = secrets.token_urlsafe(9)
        self.db.execute("INSERT INTO content_sessions VALUES (?,?,?,NULL,?,?)",
            (token, message.from_user.id, message.chat.id, json.dumps(state), time.time() + 900))
        waiting = None
        if arg and command not in {"/pishi", "/phishi"}:
            waiting = await message.reply("⏳ Checking source records…")
        try:
            if command in {"/pishi", "/phishi"}:
                state["cmd"] = "/pishi"
                await self._cat(state)
            elif arg:
                await self._query(state, arg)
            await self._display(token, message.chat.id, state, previous=
                waiting.message_id if waiting else None, reply_to=message.message_id)
        except ContentError as exc:
            await self._display(token, message.chat.id, state, error=str(exc),
                previous=waiting.message_id if waiting else None,
                reply_to=message.message_id)
        except (KeyError, TypeError, ValueError):
            await self._display(token, message.chat.id, state,
                error="Content source returned incomplete data",
                previous=waiting.message_id if waiting else None,
                reply_to=message.message_id)
        return True

    async def _query(self, state: dict[str, Any], query: str) -> None:
        cmd = state["cmd"]
        state["query"] = query[:100]
        if cmd == "/earth":
            if query.casefold() in {"volcano", "aurora", "glacier", "waterfall"} or \
                    state.get("intent") == "nature":
                item = await self.providers.nature(query)
                state["intent"] = "nature"
            else:
                state["intent"] = "species"
                candidates = await self.providers.species_candidates(query)
                if not candidates:
                    raise ContentError("No matching species was found")
                exact = [c for c in candidates if query.casefold() in {
                    c["name"].casefold(), c["common"].casefold()}]
                if len(candidates) > 1 and len(exact) != 1 and \
                        query.casefold() not in {"cat", "domestic cat"}:
                    state.update(view="taxa", candidates=candidates)
                    return
                taxon_id = int((exact or candidates)[0]["id"])
                state["taxon_id"] = taxon_id
                item = await self.providers.species(taxon_id=taxon_id)
            state.update(view="item", item=asdict(item))
        elif cmd == "/art":
            is_artist = state.get("intent") == "artist"
            items, total = await self.providers.art_search(query, 0, artist=is_artist)
            if not items:
                raise ContentError("No rights-clear works by this artist were found" if is_artist
                                   else "No rights-clear artwork matched this search")
            state.update(view="artist" if is_artist else "list",
                         items=[asdict(i) for i in items], page=0,
                         total=total, intent=state.get("intent") or "search")
        elif cmd == "/wiki":
            if state.get("intent") == "images":
                images = await self.providers.wiki_images(query, state["lang"])
                state.update(view="list", items=[asdict(i) for i in images],
                             page=0, total=len(images))
            else:
                titles, total = await self.providers.wiki_search(query, state["lang"])
                if not titles:
                    raise ContentError("Wikipedia found no matching articles")
                state.update(view="list", items=titles, page=0, total=total)
        elif cmd == "/quote":
            titles = await self.providers.quote_search(query, state["lang"])
            if not titles:
                raise ContentError("Wikiquote found no matching pages")
            state.update(view="list", items=titles, page=0, total=len(titles))

    async def _navigate(self, state: dict[str, Any], action: str) -> None:
        cmd = state["cmd"]
        if action == "back":
            history = state.get("history") or []
            if history:
                previous = history.pop()
                state.clear()
                state.update(previous, history=history)
            else:
                state["view"] = "home"
            return
        if action == "lang":
            state["lang"] = "fa" if state.get("lang") == "en" else "en"
            state["view"] = "home"
            return
        if action in {"yesterday", "tomorrow"}:
            day = dt.date.fromisoformat(state["day"])
            day += dt.timedelta(days=-1 if action == "yesterday" else 1)
            if day > dt.datetime.now(dt.UTC).date():
                raise ContentError("That date has not been published yet")
            state["day"] = day.isoformat()
            action = "daily:" + str(state.get("intent", "picture"))
        if action == "random_date" and cmd == "/xotd":
            topic = str(state.get("intent"))
            if topic not in {"picture", "media"}:
                raise ContentError("Random dates are unavailable for this topic")
            for _ in range(4):
                day = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=random.randint(1, 1095))
                try:
                    entries = await self.providers.daily(topic, day)
                    state.update(day=day.isoformat(), view="daily", item=asdict(entries[0]),
                                 items=[asdict(i) for i in entries])
                    return
                except ContentError:
                    continue
            raise ContentError("No archived daily media was found after bounded attempts")
        if action == "another":
            if cmd == "/pishi":
                await self._cat(state)
            elif cmd == "/art":
                previous_id = (state.get("item") or {}).get("identifier")
                if state.get("intent") == "artist" and state.get("query"):
                    artist = str(state["query"])
                    total = max(1, int(state.get("total", 1)))
                    pages = max(1, min(20, (total + 7) // 8))
                    for _ in range(3):
                        page = random.randrange(pages)
                        works, _ = await self.providers.art_search(artist, page, artist=True)
                        alternatives = [work for work in works
                                        if work.identifier != str(previous_id or "")]
                        if alternatives:
                            state.update(view="item", item=asdict(random.choice(alternatives)))
                            break
                    else:
                        raise ContentError("No different work by this artist was available")
                else:
                    state.update(view="item", item=asdict(await self.providers.art_random(
                        str(previous_id or ""))))
            elif cmd == "/earth":
                if state.get("intent") == "nature":
                    previous_id = (state.get("item") or {}).get("identifier")
                    state.update(view="item", item=asdict(await self.providers.nature(
                        str(state.get("query", "")), str(previous_id or ""))))
                else:
                    prior = (state.get("item") or {}).get("identifier")
                    for _ in range(3):
                        state.update(view="item", item=asdict(await self.providers.species(
                            taxon_id=int(state["taxon_id"]) if state.get("taxon_id") else None)))
                        if state["item"].get("identifier") != prior:
                            break
            elif cmd == "/quote":
                current = state.get("item") or {}
                author = str(current.get("title") or "").strip()
                if author:
                    items, _ = await self.providers.quote_page(author, state["lang"])
                    alternatives = [item for item in items if item.description !=
                                    current.get("description")]
                    if not alternatives:
                        raise ContentError("No other sourced quote by this author was found")
                    state.update(view="item", item=asdict(random.choice(alternatives)))
                else:
                    items = await self.providers.quote_random(state["lang"])
                    state.update(view="item", item=asdict(random.choice(items)))
            elif cmd == "/wiki":
                previous_id = (state.get("item") or {}).get("identifier")
                for _ in range(3):
                    item = await self.providers.wiki_random(state["lang"])
                    if item.identifier != previous_id:
                        break
                state.update(view="item", item=asdict(item))
            return
        snapshot = {k: v for k, v in state.items() if k != "history"}
        state["history"] = (state.get("history") or [])[-3:] + [snapshot]
        if action == "images" and cmd == "/wiki" and state.get("item"):
            state["intent"] = "images"
            await self._query(state, str(state["item"]["identifier"]))
        elif action in {"species", "nature", "search", "artist", "images"}:
            state.update(view="prompt", intent=action)
        elif action == "artist_works" and cmd == "/art":
            state["view"] = "list"
        elif action == "creator" and cmd == "/art" and state.get("item"):
            creator = str(state["item"].get("credit") or "").split(" (", 1)[0].strip()
            if not creator:
                raise ContentError("This artwork has no listed artist")
            state["intent"] = "artist"
            await self._query(state, creator)
        elif action == "author" and cmd == "/quote" and state.get("item"):
            quoted, _ = await self.providers.quote_page(str(state["item"]["title"]), state["lang"])
            state.update(view="list", items=[asdict(i) for i in quoted], page=0,
                         total=len(quoted), query=str(state["item"]["title"]))
        elif action == "readmore" and cmd == "/wiki" and state.get("item"):
            title = str(state["item"]["identifier"])
            sections = await self.providers.wiki_sections(title, state["lang"])
            body = await self.providers.wiki_section(title, sections[0]["index"], state["lang"])
            state.update(view="readmore", sections=sections, section=0, section_text=body)
        elif action == "nextsection" and cmd == "/wiki" and state.get("view") == "readmore":
            index = int(state.get("section", 0)) + 1
            sections = state["sections"]
            if index >= len(sections):
                raise ContentError("No further section is available")
            title = str(state["item"]["identifier"])
            body = await self.providers.wiki_section(title, sections[index]["index"], state["lang"])
            state.update(section=index, section_text=body)
        elif action.startswith("taxon:") and cmd == "/earth":
            index = int(action.split(":", 1)[1])
            candidates = state.get("candidates") or []
            if not 0 <= index < len(candidates):
                raise ContentError("Taxon choice expired")
            state["taxon_id"] = int(candidates[index]["id"])
            state.update(view="item", item=asdict(
                await self.providers.species(taxon_id=state["taxon_id"])))
        elif action == "random":
            if cmd == "/art":
                state.update(view="item", item=asdict(await self.providers.art_random()))
            elif cmd == "/earth":
                state.update(view="item", item=asdict(
                    await self.providers.nature() if state.get("intent") == "nature" else
                    await self.providers.species()))
            elif cmd == "/wiki":
                state.update(view="item", item=asdict(await self.providers.wiki_random(state["lang"])))
            else:
                items = await self.providers.quote_random(state["lang"])
                state.update(view="item", item=asdict(random.choice(items)))
        elif action.startswith("menu:"):
            menu = action.split(":", 1)[1]
            if menu not in {"media", "knowledge", "history", "current",
                            "discover", "featured"}:
                raise ContentError("Unknown content menu")
            state.update(view=menu)
        elif action.startswith("daily:"):
            topic = action.split(":", 1)[1]
            if topic not in DAILY:
                raise ContentError("Unknown daily topic")
            items = await self.providers.daily(topic, dt.date.fromisoformat(state["day"]))
            if not items:
                raise ContentError("No verified item was published for this date")
            state.update(view="daily_list" if len(items) > 1 else "daily", intent=topic,
                         items=[asdict(i) for i in items],
                         page=0, item=asdict(items[0]))
        elif action.startswith("select:"):
            index = int(action.split(":", 1)[1])
            items = state.get("items") or []
            if index < 0 or index >= len(items):
                raise ContentError("That result is no longer available")
            entry = items[index]
            if isinstance(entry, dict):
                item = Item(**entry)
            elif cmd == "/wiki":
                item = await self.providers.wiki_article(str(entry), state["lang"])
            else:
                quote_items, _ = await self.providers.quote_page(str(entry), state["lang"])
                state["items"] = [asdict(i) for i in quote_items]
                state["total"] = len(quote_items)
                state["page"] = 0
                state["view"] = "list"
                return
            state.update(view="item", item=asdict(item))
        elif action.startswith("page:"):
            page = int(action.split(":", 1)[1])
            if not 0 <= page <= 100:
                raise ContentError("Page is out of range")
            state["page"] = page
            if cmd == "/art":
                items, _ = await self.providers.art_search(state["query"], page,
                    artist=state.get("intent") == "artist")
                state["items"] = [asdict(i) for i in items]
            elif cmd == "/wiki":
                titles, _ = await self.providers.wiki_search(state["query"], state["lang"], page)
                state["items"] = titles
            state["view"] = "daily_list" if cmd == "/xotd" else "list"
        elif action == "featured_random" and cmd == "/wiki":
            for _ in range(3):
                day = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=random.randint(30, 730))
                try:
                    items = await self.providers.daily("picture", day)
                    state.update(view="item", item=asdict(items[0]))
                    break
                except ContentError:
                    continue
            else:
                raise ContentError("No archived Commons picture was available")
        else:
            raise ContentError("Unknown content action")

    def _screen(self, state: dict[str, Any]) -> tuple[str, list[list[tuple[str, str]]], str]:
        cmd, view = state["cmd"], state["view"]
        done = [("🙏 Done", "done")]
        back = [("◀ Back", "back"), ("🙏 Done", "done")]
        if cmd == "/pishi":
            item = state.get("item")
            if item:
                return ("🐈 <b>Mikachan · Cat</b>\nSource · " +
                    html.escape(item["provider"]), [[("🎲 Another Cat", "another")],
                    [("🔗 Original Image", item["source_url"])], done], item["image_url"])
            return "Cat photos are unavailable right now.", \
                [[("🎲 Try Again", "another")], done], ""
        if view == "home":
            if cmd == "/earth":
                return "🌍 <b>Earth · Explore Our Planet</b>", \
                    [[("🐾 Species", "species")], [("🌋 Nature", "nature")], done], ""
            if cmd == "/art":
                return "🖼 <b>Art · Open Collections</b>", \
                    [[("🎲 Random Artwork", "random")], [("🔎 Search Artwork", "search")],
                     [("👨‍🎨 Choose Artist", "artist")], done], ""
            if cmd == "/xotd":
                return (f"📅 <b>X of the Day</b> · {state['day']}",
                    [[("🖼 Media", "menu:media"), ("📚 Knowledge", "menu:knowledge")],
                     [("🕰 History", "menu:history"), ("📰 Current", "menu:current")], done], "")
            if cmd == "/quote":
                return "💬 <b>Wikiquote</b> · " + state["lang"].upper(), \
                    [[("🎲 Random Quote", "random")], [("👤 Choose Author", "artist")],
                     [("🔎 Search Quotes", "search")],
                     [("🌐 Language: " + state["lang"].upper(), "lang")], done], ""
            return "📖 <b>Wikipedia</b> · " + state["lang"].upper(), \
                [[("🔎 Search Article", "search"), ("🎲 Discover", "menu:discover")],
                 [("🖼 Media & Featured", "menu:featured")], done], ""
        if view in {"media", "knowledge", "history", "current"}:
            topics = {"media": ("picture", "media"),
                      "knowledge": ("word", "featured", "dyk"),
                      "history": ("events", "births", "deaths"),
                      "current": ("news", "mostread")}[view]
            return f"📅 <b>{html.escape(view.title())}</b> · {state['day']}", \
                [[(DAILY[t], f"daily:{t}")] for t in topics] + [back], ""
        if view == "discover":
            return "🎲 <b>Discover Wikipedia</b>", \
                [[("🎲 Random Article", "random")], back], ""
        if view == "featured":
            return "🖼 <b>Wikipedia Media</b>", \
                [[("🌟 Archived Featured Picture", "featured_random")],
                 [("🗂 Images from an Article", "images")], back], ""
        if view == "prompt":
            label = {"species": "species name", "nature": "nature topic", "search": "search text",
                     "artist": "artist or author name", "images": "article title"}.get(
                         str(state.get("intent")), "query")
            rows = ([[(("🎲 Surprise Me" if state.get("intent") == "nature" else
                        "🎲 Random Species"), "random")]] if
                    state.get("intent") in {"species", "nature"} else [])
            return f"🔎 Reply to this message with a {label}.", rows + [back], ""
        if view == "taxa":
            rows = [[(f"{str(c.get('common') or c.get('name'))[:40]} · "
                      f"{str(c.get('name'))[:20]}", f"taxon:{n}")]
                    for n, c in enumerate((state.get("candidates") or [])[:5])]
            return "🐾 <b>Choose a real taxon</b>", rows + [back], ""
        if view == "artist" and cmd == "/art":
            return (f"👨‍🎨 <b>{html.escape(str(state.get('query', 'Artist')))}</b>\n"
                    f"{len(state.get('items') or [])} available works on this page · "
                    f"{int(state.get('total', 0))} catalog records",
                    [[("🖼 View works", "artist_works")],
                     [("🔎 Another artist", "artist")], back], "")
        if view == "list":
            items = state.get("items") or []
            page = int(state.get("page", 0))
            body = (f"👨‍🎨 <b>{html.escape(str(state.get('query', '')))}</b> · works · page {page + 1}"
                    if cmd == "/art" and state.get("intent") == "artist" else
                    f"🔎 <b>Results</b> · page {page + 1}\n{html.escape(str(state.get('query', '')))}")
            local_paging = cmd == "/quote" and bool(items) and isinstance(items[0], dict)
            visible = items[page * 8:(page + 1) * 8] if local_paging else items
            rows = [[(f"{page * 8 + n + 1}. {str(i.get('title') if isinstance(i, dict) else i)[:45]}",
                      f"select:{page * 8 + n if local_paging else n}")]
                    for n, i in enumerate(visible)]
            nav = []
            if page:
                nav.append(("◀ Prev", f"page:{page - 1}"))
            if cmd in {"/art", "/wiki"} and (page + 1) * 8 < int(state.get("total", 0)):
                nav.append(("Next ▶", f"page:{page + 1}"))
            if local_paging and (page + 1) * 8 < len(items):
                nav.append(("Next ▶", f"page:{page + 1}"))
            if nav:
                rows.append(nav)
            return body, rows + [back], ""
        if view == "daily_list":
            items = state.get("items") or []
            page = int(state.get("page", 0))
            rows = [[(f"{n + 1}. {str(item.get('title', ''))[:48]}", f"select:{n}")]
                    for n, item in enumerate(items[page * 8:(page + 1) * 8], page * 8)]
            nav = []
            if page:
                nav.append(("◀ Prev", f"page:{page - 1}"))
            if (page + 1) * 8 < len(items):
                nav.append(("Next ▶", f"page:{page + 1}"))
            if nav:
                rows.append(nav)
            if state.get("intent") in {"events", "births", "deaths"}:
                rows.append([("◀ Yesterday", "yesterday"),
                             ("▶ Next Date", "tomorrow")])
            actual_date = str(items[0].get("date") or state["day"]) if items else state["day"]
            return (f"📅 <b>{DAILY[state['intent']]}</b> · {actual_date}\n"
                    f"{len(items)} sourced items", rows + [back], "")
        if view == "readmore":
            sections = state.get("sections") or []
            index = int(state.get("section", 0))
            title = str(sections[index].get("title") or "Section")
            source = str((state.get("item") or {}).get("source_url") or "")
            rows = [[("Next Section ▶", "nextsection")]] if index + 1 < len(sections) else []
            if source.startswith("https://"):
                rows.append([("🔗 Wikipedia Article", source)])
            return (f"📖 <b>{html.escape(title)}</b> · {index + 1}/{len(sections)}\n"
                    f"{html.escape(str(state.get('section_text', '')))}",
                    rows + [back], "")
        item = state.get("item") or {}
        source = str(item.get("source_url") or "")
        body = (f"<b>{html.escape(str(item.get('title', 'Item'))[:170])}</b>\n"
                f"{html.escape(str(item.get('description', ''))[:850])}\n\n"
                f"{html.escape(str(item.get('provider', 'Source')))}"
                + (f"\nCredit · {html.escape(str(item['credit'])[:150])}"
                   if item.get("credit") else "")
                + (f" · {html.escape(str(item['license']))}" if item.get("license") else "")
                + (f" · {html.escape(str(item['date']))}" if item.get("date") else ""))
        rows = []
        if view == "item" and cmd == "/art" and item.get("credit"):
            rows.append([("👨‍🎨 Artist", "creator")])
        if view == "item" and cmd == "/wiki":
            rows.append([("📖 Read More", "readmore"), ("🖼 Article Images", "images")])
        if view == "daily":
            if len(state.get("items") or []) > 1:
                items = state["items"]
                rows += [[(f"{n + 1}. {str(i.get('title',''))[:36]}", f"select:{n}")]
                         for n, i in enumerate(items[:8])]
            if state.get("intent") not in {"dyk", "news", "mostread"}:
                rows.append([("◀ Yesterday", "yesterday"), ("▶ Next Date", "tomorrow")])
            if state.get("intent") in {"picture", "media"}:
                rows.append([("🎲 Random Date", "random_date")])
            if state.get("intent") == "word" and item.get("audio_url"):
                rows.append([("🔊 Recorded Pronunciation", "play_audio")])
            elif state.get("intent") == "word":
                body += "\nRecorded pronunciation unavailable for this word."
        elif cmd in {"/earth", "/art", "/wiki", "/quote"}:
            rows.append([("🎲 Another", "another")])
            if cmd == "/earth":
                rows.append([("🔎 New Species" if state.get("intent") == "species" else
                              "🔎 New Topic", "species" if state.get("intent") == "species"
                              else "nature")])
            if cmd == "/quote":
                rows.append([("👤 More by This Author/Page", "author")])
        if source.startswith("https://"):
            rows.append([("🔗 Original Source", source)])
        rows.append(back)
        return body, rows, str(item.get("image_url") or "")

    async def _display(self, token: str, chat: int, state: dict[str, Any], *,
                       previous: int | None = None, reply_to: int | None = None,
                       error: str = "") -> None:
        session = self.db.execute("SELECT actor_id FROM content_sessions WHERE token=?",
                                  (token,)).fetchone()
        if not session or not self.authorize(int(session[0]), chat):
            self.db.execute("DELETE FROM content_sessions WHERE token=?", (token,))
            return
        body, rows, image_url = self._screen(state)
        if error:
            body = f"⚠️ {html.escape(error)}\n\n" + body
            image_url = ""
        buttons = _markup(token, rows)
        try:
            if image_url and not error:
                sent = await self.bot.send_photo(chat, image_url, caption=body[:1020],
                                                 reply_markup=buttons, reply_to_message_id=reply_to)
            else:
                sent = await self.bot.send_message(chat, body[:4090], reply_markup=buttons,
                                                   reply_to_message_id=reply_to)
        except TelegramBadRequest:
            sent = await self.bot.send_message(chat, body[:4090] + "\nImage preview unavailable.",
                                               reply_markup=buttons, reply_to_message_id=reply_to)
        self.db.execute("UPDATE content_sessions SET message_id=?,state_json=? WHERE token=?",
                        (sent.message_id, json.dumps(state), token))
        if previous:
            try:
                await self.bot.delete_message(chat, previous)
            except TelegramBadRequest:
                try:
                    await self.bot.edit_message_reply_markup(chat_id=chat, message_id=previous,
                                                             reply_markup=None)
                except TelegramBadRequest:
                    pass

    async def callback(self, q: CallbackQuery, data: str) -> bool:
        if not data.startswith("ct:"):
            return False
        parts = data.split(":", 2)
        if len(parts) != 3 or not isinstance(q.message, Message):
            await q.answer()
            return True
        token, action = parts[1:]
        row = self._session(token, q.from_user.id, q.message.chat.id, q.message.message_id)
        if not row or not self.enabled:
            await q.answer("This content panel has expired")
            return True
        await q.answer()
        if action == "done":
            self.db.execute("DELETE FROM content_sessions WHERE token=?", (token,))
            try:
                await self.bot.delete_message(q.message.chat.id, q.message.message_id)
            except TelegramBadRequest:
                await self.bot.edit_message_reply_markup(chat_id=q.message.chat.id,
                    message_id=q.message.message_id, reply_markup=None)
            return True
        state = json.loads(row["state_json"])
        if action == "play_audio":
            url = str((state.get("item") or {}).get("audio_url") or "")
            if url.startswith("https://upload.wikimedia.org/"):
                try:
                    await self.bot.send_audio(q.message.chat.id, url,
                        caption="Recorded pronunciation · Wiktionary / Wikimedia Commons")
                except TelegramBadRequest:
                    await self.bot.send_message(q.message.chat.id,
                        "Recorded audio is unavailable from Telegram right now.")
            return True
        try:
            await self._navigate(state, action)
            await self._display(token, q.message.chat.id, state, previous=q.message.message_id)
        except (ContentError, ValueError) as exc:
            await self._display(token, q.message.chat.id, state, previous=q.message.message_id,
                                error=str(exc))
        except (KeyError, TypeError):
            await self._display(token, q.message.chat.id, state, previous=q.message.message_id,
                                error="Content source returned incomplete data")
        return True

    async def reply(self, m: Message) -> bool:
        if not m.from_user or not m.reply_to_message or not m.text or m.text.startswith("/"):
            return False
        rows = self.db.execute("SELECT * FROM content_sessions WHERE actor_id=? AND chat_id=? "
            "AND message_id=? AND expires>?",
            (m.from_user.id, m.chat.id, m.reply_to_message.message_id, time.time())).fetchall()
        if len(rows) != 1:
            return False
        row = rows[0]
        state = json.loads(row["state_json"])
        if state.get("view") != "prompt":
            return False
        if not 1 <= len(m.text.strip()) <= 100:
            await m.reply("Use 1–100 characters for this search.")
            return True
        try:
            await self._query(state, m.text.strip())
            await self._display(row["token"], m.chat.id, state, previous=row["message_id"])
        except ContentError as exc:
            await m.reply("⚠️ " + html.escape(str(exc)))
        except (KeyError, TypeError, ValueError):
            await m.reply("⚠️ Content source returned incomplete data")
        return True
