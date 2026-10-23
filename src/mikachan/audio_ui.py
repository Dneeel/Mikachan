"""Explicit, account-catalog-driven TTS/STT screens; n8n owns provider calls."""

import asyncio
import datetime as dt
import html
import io
import json
import math
import re
import secrets
import sqlite3
import time
from typing import Any, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from .n8n_gateway import GatewayError, N8NGateway


def _keyboard(token: str, rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"au:{token}:{action}")
        for label, action in row] for row in rows])


def _audio_from_message(m: Message | None) -> tuple[str, str, int, int] | None:
    if m is None:
        return None
    if m.voice:
        return m.voice.file_id, "audio/ogg", m.voice.file_size or 0, m.voice.duration
    if m.audio:
        return m.audio.file_id, m.audio.mime_type or "audio/mpeg", \
            m.audio.file_size or 0, m.audio.duration
    if m.video:
        return m.video.file_id, m.video.mime_type or "video/mp4", \
            m.video.file_size or 0, m.video.duration
    if m.document and m.document.mime_type in {"audio/mpeg", "audio/ogg", "audio/mp4",
                                               "audio/wav", "video/mp4"}:
        return m.document.file_id, m.document.mime_type, m.document.file_size or 0, 0
    return None


class AudioUI:
    TELEGRAM_FORMATS = frozenset({"mp3_44100_128", "mp3_44100_64",
                                  "mp3_22050_32", "ogg_48000_64"})

    @classmethod
    def _formats(cls, model: dict[str, Any]) -> list[str]:
        values = model.get("output_formats")
        return [value for value in values if isinstance(value, str) and
                value in cls.TELEGRAM_FORMATS] \
            if isinstance(values, list) else []

    def __init__(self, db: sqlite3.Connection, bot: Bot, gateway: N8NGateway,
                 *, tts_enabled: bool, stt_enabled: bool, poll_seconds: float = 3,
                 authorize: Callable[[int, int], bool] | None = None):
        self.db, self.bot, self.gateway = db, bot, gateway
        self.tts_enabled, self.stt_enabled = tts_enabled, stt_enabled
        self.poll_seconds = poll_seconds
        self.authorize = authorize or (lambda actor, chat: True)
        self.inputs: dict[str, str | tuple[str, str, int, int]] = {}
        self.recovery_tasks: set[asyncio.Task[None]] = set()
        db.executescript("""
        CREATE TABLE IF NOT EXISTS audio_sessions (
          token TEXT PRIMARY KEY, actor_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          message_id INTEGER, state_json TEXT NOT NULL, expires REAL NOT NULL,
          stage TEXT NOT NULL DEFAULT 'config', request_id TEXT, job_id TEXT);
        CREATE TABLE IF NOT EXISTS audio_presets (
          actor_id INTEGER NOT NULL, task TEXT NOT NULL, name TEXT NOT NULL,
          settings_json TEXT NOT NULL, created REAL NOT NULL,
          PRIMARY KEY(actor_id,task,name));
        CREATE TABLE IF NOT EXISTS audio_usage (
          day TEXT NOT NULL, actor_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          task TEXT NOT NULL, count INTEGER NOT NULL,
          PRIMARY KEY(day,actor_id,chat_id,task));
        """)
        fields = {str(row[1]) for row in db.execute("PRAGMA table_info(audio_sessions)")}
        if "job_id" not in fields:
            db.execute("ALTER TABLE audio_sessions ADD COLUMN job_id TEXT")

    async def recover(self) -> None:
        """Resume accepted jobs after restart without submitting a second paid call."""
        self.db.execute("UPDATE audio_sessions SET stage='failed' WHERE stage='processing' "
            "AND (job_id IS NULL OR expires<?)", (time.time(),))
        if not self.gateway.enabled:
            return
        rows = self.db.execute("SELECT token FROM audio_sessions WHERE stage='processing' "
            "AND job_id IS NOT NULL AND expires>?", (time.time(),)).fetchall()
        for row in rows[:4]:
            task = asyncio.create_task(self._resume_job(str(row[0])))
            self.recovery_tasks.add(task)
            task.add_done_callback(self.recovery_tasks.discard)

    async def close(self) -> None:
        for task in self.recovery_tasks:
            task.cancel()
        if self.recovery_tasks:
            await asyncio.gather(*self.recovery_tasks, return_exceptions=True)

    async def _resume_job(self, token: str) -> None:
        row = self.db.execute("SELECT * FROM audio_sessions WHERE token=?", (token,)).fetchone()
        if not row or not row["job_id"]:
            return
        actor, chat = int(row["actor_id"]), int(row["chat_id"])
        if not self.authorize(actor, chat):
            self.db.execute("UPDATE audio_sessions SET stage='failed' WHERE token=?", (token,))
            return
        state = json.loads(row["state_json"])
        try:
            for _ in range(20):
                current = self.db.execute("SELECT stage FROM audio_sessions WHERE token=?",
                                          (token,)).fetchone()
                if not current:
                    return
                if current[0] == "cancelled":
                    await self.gateway.cancel_job(str(row["job_id"]))
                    return
                if current[0] != "processing":
                    return
                result = await self.gateway.get_job(str(row["job_id"]))
                if result["status"] == "succeeded":
                    if not self.authorize(actor, chat):
                        return
                    await self._deliver(chat, result["result"], str(row["request_id"]),
                                        state["task"], state["output"], actor)
                    self.db.execute("UPDATE audio_sessions SET stage='done' WHERE token=?", (token,))
                    await self._finish_message(token, actor, chat, "✅ Audio task completed.")
                    return
                if result["status"] in {"failed", "cancelled", "expired"}:
                    raise GatewayError("Provider job did not complete")
                await asyncio.sleep(self.poll_seconds)
            raise GatewayError("Provider job status timed out after restart")
        except (GatewayError, TelegramBadRequest):
            self.db.execute("UPDATE audio_sessions SET stage='failed' WHERE token=?", (token,))
            await self._finish_message(token, actor, chat, "⚠️ Audio job could not be recovered.")

    def _session(self, token: str, actor: int, chat: int, message: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM audio_sessions WHERE token=? AND actor_id=? "
            "AND chat_id=? AND message_id=? AND expires>?",
            (token, actor, chat, message, time.time())).fetchone()

    @staticmethod
    def _catalog(payload: dict[str, Any]) -> list[dict[str, Any]]:
        result = payload.get("result", {})
        if result.get("kind") != "catalog":
            raise GatewayError("n8n catalog is incompatible")
        return [i for i in result.get("items", []) if isinstance(i, dict)]

    async def command(self, m: Message, cmd: str, arg: str) -> bool:
        if cmd not in {"/tts", "/stt"}:
            return False
        if not m.from_user:
            return True
        task = cmd[1:]
        if not self.gateway.enabled or not (self.tts_enabled if task == "tts" else self.stt_enabled):
            await m.reply(f"{task.upper()} is not connected to the owner's n8n workflow yet.")
            return True
        self.db.execute("DELETE FROM audio_sessions WHERE expires<?", (time.time(),))
        live_tokens = {str(row[0]) for row in self.db.execute("SELECT token FROM audio_sessions")}
        for token in list(self.inputs):
            if token not in live_tokens:
                self.inputs.pop(token, None)
        waiting = await m.reply("⏳ Checking the account's available models…")
        try:
            models = self._catalog(await self.gateway.get_models(task))
            models = [x for x in models if x.get("id") and x.get("name") and
                      x.get("free_entitled") is True]
            if not models:
                raise GatewayError("No verified free-eligible model is available for this account")
            voices = []
            if task == "tts":
                voices = self._catalog(await self.gateway.get_voices(model_id=str(models[0]["id"])))
                voices = [x for x in voices if x.get("id") and x.get("name") and
                          x.get("free_entitled") is True]
                if not voices:
                    raise GatewayError("No compatible free-eligible voice is available")
            token = secrets.token_urlsafe(9)
            state: dict[str, Any] = {"task": task, "view": "home", "models": models[:30],
                "voices": voices[:80], "model": 0, "voice": 0, "controls": {}, "language": "auto",
                "diarize": False, "speakers": 0, "timestamps": "none",
                "audio_events": False, "output": "plain", "history": [],
                "voice_page": 0, "model_page": 0, "delivery": "Natural",
                "speaker_boost": False, "text_normalization": "auto", "output_format": ""}
            self._reset_controls(state)
            default_preset = self.db.execute("SELECT 1 FROM audio_presets WHERE actor_id=? "
                "AND task=? AND name='Default'", (m.from_user.id, task)).fetchone()
            if default_preset:
                try:
                    await self._action(token, state, "preset:Default", m.from_user.id)
                except GatewayError:
                    state["view"] = "home"
            source: str | tuple[str, str, int, int] | None = None
            if task == "tts":
                source = (m.reply_to_message.text or m.reply_to_message.caption or "") \
                    if m.reply_to_message else arg
                if source and len(source) > 3000:
                    raise GatewayError("TTS text exceeds 3,000 characters")
            else:
                source = _audio_from_message(m.reply_to_message)
                if source and (source[2] > 20_000_000 or source[3] > 600):
                    raise GatewayError("STT supports at most 20 MB and 10 minutes per request")
            self.db.execute("INSERT INTO audio_sessions VALUES (?,?,?,NULL,?,?,'config',NULL,NULL)",
                (token, m.from_user.id, m.chat.id, json.dumps(state), time.time() + 900))
            if source:
                self.inputs[token] = source
            await self._show(token, m.chat.id, state, old=waiting.message_id,
                             reply_to=m.message_id)
        except GatewayError as exc:
            try:
                await waiting.edit_text("⚠️ " + html.escape(str(exc)))
            except TelegramBadRequest:
                await m.reply("⚠️ " + html.escape(str(exc)))
        return True

    @staticmethod
    def _model(state: dict[str, Any]) -> dict[str, Any]:
        return state["models"][state["model"]]

    def _reset_controls(self, state: dict[str, Any]) -> None:
        model = self._model(state)
        controls = model.get("controls") or {}
        valid = {}
        for name, info in controls.items():
            if not isinstance(info, dict) or not all(isinstance(info.get(key), (int, float))
                    for key in ("min", "max", "default")):
                continue
            minimum, maximum, default = (float(info[key]) for key in ("min", "max", "default"))
            if all(map(math.isfinite, (minimum, maximum, default))) and \
                    0 <= minimum <= default <= maximum <= 2:
                valid[name] = default
        state["controls"] = valid
        state["language"] = "auto"
        state["delivery"] = "Natural"
        state["speaker_boost"] = bool(model.get("speaker_boost_default")) if \
            model.get("supports_speaker_boost") else False
        state["text_normalization"] = "auto"
        state["output_format"] = (self._formats(model) or [""])[0]

    def _screen(self, token: str, state: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
        task, view = state["task"], state["view"]
        model = self._model(state)
        back = [("◀ Back", "back"), ("🙏 Done", "done")]
        if state.get("stage") == "processing":
            return "⏳ <b>Processing with n8n</b>\nWaiting for the provider result.", \
                _keyboard(token, [[("✖ Cancel", "cancel")]])
        if view == "home":
            if task == "tts":
                voice = state["voices"][state["voice"]]
                details = ", ".join(f"{k}: {v:g}" for k, v in state["controls"].items())
                tts_rows = [[("🎤 Choose Voice", "voices"),
                             ("🧠 Choose Model", "models")],
                            [("🌐 Language", "languages"),
                             ("🎛 Voice Settings", "settings")]]
                extras = []
                if model.get("delivery_directions"):
                    extras.append(("🎭 Delivery", "delivery"))
                if self._formats(model) or model.get("supports_text_normalization"):
                    extras.append(("⚙️ Advanced", "advanced"))
                if extras:
                    tts_rows.append(extras)
                tts_rows += [[("🗂 Presets", "presets")],
                             [("▶ Continue · Enter Text", "continue")],
                             [("↻ Reset", "reset"), ("🙏 Done", "done")]]
                return (f"🎙 <b>Text to Speech</b>\nModel · {html.escape(str(model['name']))}\n"
                        f"Voice · {html.escape(str(voice['name']))}\n"
                        f"Language · {html.escape(state['language'])}\n"
                        f"Delivery · {html.escape(str(state['delivery']))}\n"
                        f"{html.escape(details[:250])}\n"
                        "No provider call occurs until Generate.",
                        _keyboard(token, tts_rows))
            stt_rows = [[("🌐 Language", "languages")]]
            if model.get("max_speakers") or model.get("supports_diarize"):
                stt_rows[0].append(("👥 Speakers", "speakers"))
            stt_rows.append([("⏱ Timestamps", "timestamps")])
            if model.get("supports_audio_events"):
                stt_rows[-1].append(("🎧 Audio Events", "events"))
            stt_rows += [[("📄 Output", "outputs"), ("🧠 Model", "models")],
                         [("🗂 Presets", "presets")],
                         [("▶ Continue · Send Audio", "continue")],
                         [("↻ Reset", "reset"), ("🙏 Done", "done")]]
            return (f"📝 <b>Speech to Text</b>\nModel · {html.escape(str(model['name']))}\n"
                    f"Language · {html.escape(state['language'])}\n"
                    f"Speakers · {'Auto' if not state['speakers'] else state['speakers']}\n"
                    f"Diarization · {'On' if state['diarize'] else 'Off'}\n"
                    f"Timestamps · {state['timestamps']}\n"
                    f"Audio events · {'On' if state['audio_events'] else 'Off'}\n"
                    f"Output · {state['output']}",
                    _keyboard(token, stt_rows))
        if view in {"models", "voices"}:
            key = "models" if view == "models" else "voices"
            entries = state[key]
            page_key = "model_page" if key == "models" else "voice_page"
            page = int(state.get(page_key, 0))
            rows = [[(str(entry.get("name") or entry["id"])[:55],
                      f"{'voice_detail' if key == 'voices' else 'select:models'}:{n}")]
                    for n, entry in enumerate(entries) if page * 8 <= n < (page + 1) * 8]
            nav = []
            if page:
                nav.append(("◀ Prev", f"catalogpage:{key}:{page - 1}"))
            if (page + 1) * 8 < len(entries):
                nav.append(("Next ▶", f"catalogpage:{key}:{page + 1}"))
            if nav:
                rows.append(nav)
            if key == "voices":
                rows.append([("🔎 Search Voice", "voice_search")])
            return f"Choose a verified {view[:-1]}:", _keyboard(token, rows + [back])
        if view == "voice_detail":
            voice = state["voices"][state["selected_voice"]]
            rows = []
            if voice.get("preview_ref"):
                rows.append([("▶ Listen to Existing Sample", "voice_preview")])
            rows.append([("✔ Select Voice", f"select:voices:{state['selected_voice']}")])
            return (f"🎤 <b>{html.escape(str(voice.get('name') or voice['id']))}</b>\n"
                    f"{html.escape(str(voice.get('description') or '')[:300])}",
                    _keyboard(token, rows + [back]))
        if view == "voice_search":
            return "Reply to this exact message with a voice name.", _keyboard(token, [back])
        if view == "languages":
            languages = model.get("languages") or []
            if task == "tts" and not model.get("supports_language_code"):
                languages = []
            rows = [[("Auto Detect", "language:auto")]] + [
                [(str(x.get("name") or x.get("code"))[:45], f"language:{x['code']}")]
                for x in languages[:20] if isinstance(x, dict) and x.get("code")]
            return "🌐 <b>Language</b> · account-verified model options", \
                _keyboard(token, rows + [back])
        if view == "settings":
            controls = state["controls"]
            if not controls and not model.get("supports_speaker_boost"):
                return "This model exposes no verified per-request voice controls.", \
                    _keyboard(token, [back])
            rows = [[(f"{name.replace('_', ' ').title()} · {value:g}", f"control:{name}")]
                    for name, value in controls.items()]
            if model.get("supports_speaker_boost"):
                rows.append([("Speaker Boost · " + ("On" if state["speaker_boost"] else "Off"),
                              "speaker_boost")])
            return "🎛 <b>Voice Settings</b> · model-supported fields only", \
                _keyboard(token, rows + [back])
        if view == "delivery":
            directions = [str(x) for x in model.get("delivery_directions", [])
                          if isinstance(x, str) and 1 <= len(x) <= 35]
            rows = [[(label, f"direction:{n}")] for n, label in enumerate(directions)]
            if model.get("supports_custom_direction"):
                rows.append([("✏ Custom Direction", "custom_direction")])
            return "🎭 <b>Delivery</b> · n8n verifies model support", \
                _keyboard(token, rows + [back])
        if view == "custom_direction":
            return "Reply to this exact message with a short delivery direction.", \
                _keyboard(token, [back])
        if view == "advanced":
            rows = []
            if self._formats(model):
                rows.append([("📦 Output Format · " + state["output_format"], "formats")])
            if model.get("supports_text_normalization"):
                rows.append([("Text Normalization · " + state["text_normalization"],
                              "normalization")])
            return "⚙️ <b>Advanced</b> · available verified settings", \
                _keyboard(token, rows + [back])
        if view == "formats":
            formats = self._formats(model)
            return "📦 <b>Output Format</b>", _keyboard(token,
                [[(value[:50], f"format:{n}")] for n, value in enumerate(formats[:15])] + [back])
        if view == "normalization":
            return "Text normalization", _keyboard(token,
                [[("Auto", "norm:auto"), ("On", "norm:on"), ("Off", "norm:off")], back])
        if view.startswith("control:"):
            name = view.split(":", 1)[1]
            info = model["controls"][name]
            value = state["controls"][name]
            multiplier = 100 if float(info.get("max", 1)) <= 2 else 1
            presets = (70, 85, 100, 110, 120) if name == "speed" else (0, 25, 50, 75, 100)
            rows = [[(f"{delta:+}", f"delta:{delta}") for delta in (-10, -5, -1, 1, 5, 10)],
                    [(str(p), f"value:{p}") for p in presets],
                    [("✏ Enter Exact Value", "exact")], back]
            return (f"🎛 <b>{html.escape(name.title())}</b> · {value * multiplier:g}"
                    f" / {float(info.get('max', 1)) * multiplier:g}", _keyboard(token, rows))
        if view == "speakers":
            maximum = int(model.get("max_speakers") or 0)
            speaker_choices = [("Auto", "speaker:0")] + [(str(n), f"speaker:{n}")
                for n in range(1, min(4, maximum) + 1)]
            rows = [speaker_choices]
            if maximum > 4:
                rows.append([("−", "speaker:minus"), ("+", "speaker:plus")])
            if model.get("supports_diarize"):
                rows.append([("Diarization On/Off", "diarize")])
            return "👥 <b>Speakers</b> · maximum hint, not an exact count", \
                _keyboard(token, rows + [back])
        if view == "timestamps":
            choices = model.get("timestamps") or ["none"]
            return "⏱ <b>Timestamp granularity</b>", _keyboard(token,
                [[(x.title(), f"timestamp:{x}")] for x in choices if x in
                 {"none", "word", "character"}] + [back])
        if view == "events":
            return "🎧 <b>Audio events</b>", _keyboard(token,
                [[("On", "event:on"), ("Off", "event:off")], back])
        if view == "outputs":
            return "📄 <b>Transcript format</b>", _keyboard(token,
                [[("Plain Text", "output:plain"), ("Speaker Labels", "output:labeled")],
                 [("Timed Text", "output:timed"), ("TXT", "output:txt")],
                 [("SRT", "output:srt"), ("VTT", "output:vtt")], back])
        if view == "presets":
            rows = [[(str(row[0])[:35], f"preset:{row[0]}"),
                     ("⭐", f"preset_default:{row[0]}"),
                     ("✏", f"preset_rename:{row[0]}"),
                     ("🗑", f"preset_delete:{row[0]}")]
                    for row in self.db.execute(
                "SELECT name FROM audio_presets WHERE actor_id=? AND task=? ORDER BY name LIMIT 10",
                (state["actor"], task))]
            return "🗂 <b>Presets</b> · saved for this numeric user ID", \
                _keyboard(token, [[("💾 Save Current As…", "savepreset")]] + rows + [back])
        if view == "preset_name":
            return "Reply to this exact message with a preset name (1–24 English characters).", \
                _keyboard(token, [back])
        if view == "preset_rename":
            return "Reply to this exact message with a new preset name.", \
                _keyboard(token, [back])
        if view == "preset_delete":
            return f"Delete preset {html.escape(str(state['pending_preset']))}?", \
                _keyboard(token, [[("✅ Delete", "preset_confirm")], back])
        if view == "prompt":
            prompt_kind = "text to speak" if task == "tts" else "an audio/voice/video file"
            return f"Reply to this exact message with {prompt_kind}.", _keyboard(token, [back])
        if view.startswith("exact:"):
            control = view.split(":", 1)[1]
            info = model["controls"][control]
            return (f"Reply to this exact message with a value from "
                    f"{info.get('min', 0)} to {info.get('max', 1)}.",
                    _keyboard(token, [back]))
        if view == "preview":
            preview_source = self.inputs.get(token)
            if task == "tts":
                description = html.escape(str(preview_source or "")[:250])
            else:
                media_details = preview_source if isinstance(preview_source, tuple) else None
                description = (f"{media_details[2]} bytes · {media_details[3]} seconds"
                               if media_details else "No file")
            return (f"{'🎙' if task == 'tts' else '📝'} <b>Ready to process</b>\n"
                    f"Model · {html.escape(str(model['name']))}\n{description}\n\n"
                    "Generation uses external provider quota. One tap = one request.",
                    _keyboard(token, [[("▶ Generate" if task == "tts" else "▶ Transcribe",
                                       "generate")], back]))
        return "Session unavailable.", _keyboard(token, [back])

    async def _show(self, token: str, chat: int, state: dict[str, Any], *,
                    old: int | None = None, reply_to: int | None = None) -> None:
        row = self.db.execute("SELECT actor_id FROM audio_sessions WHERE token=?",
                              (token,)).fetchone()
        if not row or not self.authorize(int(row[0]), chat):
            self.db.execute("DELETE FROM audio_sessions WHERE token=?", (token,))
            return
        body, markup = self._screen(token, state)
        sent = await self.bot.send_message(chat, body[:4090], reply_markup=markup,
                                           reply_to_message_id=reply_to)
        self.db.execute("UPDATE audio_sessions SET message_id=?,state_json=? WHERE token=?",
                        (sent.message_id, json.dumps(state), token))
        if old:
            try:
                await self.bot.delete_message(chat, old)
            except TelegramBadRequest:
                await self.bot.edit_message_reply_markup(chat_id=chat, message_id=old,
                                                         reply_markup=None)

    def _quota_available(self, actor: int, chat: int, task: str) -> bool:
        day = dt.datetime.now(dt.UTC).date().isoformat()
        actor_count = self.db.execute("SELECT COALESCE(SUM(count),0) FROM audio_usage "
            "WHERE day=? AND actor_id=? AND task=?", (day, actor, task)).fetchone()[0]
        chat_count = self.db.execute("SELECT COALESCE(SUM(count),0) FROM audio_usage "
            "WHERE day=? AND chat_id=? AND task=?", (day, chat, task)).fetchone()[0]
        return actor_count < 2 and chat_count < 8

    def _reserve_quota(self, actor: int, chat: int, task: str) -> bool:
        day = dt.datetime.now(dt.UTC).date().isoformat()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if not self._quota_available(actor, chat, task):
                self.db.execute("ROLLBACK")
                return False
            self.db.execute("INSERT INTO audio_usage VALUES (?,?,?,?,1) "
                "ON CONFLICT(day,actor_id,chat_id,task) DO UPDATE SET count=count+1",
                (day, actor, chat, task))
            self.db.execute("COMMIT")
            return True
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    async def callback(self, q: CallbackQuery, data: str) -> bool:
        if not data.startswith("au:"):
            return False
        if not isinstance(q.message, Message):
            await q.answer()
            return True
        parts = data.split(":", 2)
        if len(parts) != 3:
            await q.answer()
            return True
        token, action = parts[1:]
        row = self._session(token, q.from_user.id, q.message.chat.id, q.message.message_id)
        if not row:
            await q.answer("Audio session expired")
            return True
        state = json.loads(row["state_json"])
        state["actor"] = q.from_user.id
        if action == "done":
            await q.answer()
            self.db.execute("DELETE FROM audio_sessions WHERE token=?", (token,))
            self.inputs.pop(token, None)
            await self.bot.delete_message(q.message.chat.id, q.message.message_id)
            return True
        if row["stage"] == "processing":
            if action == "cancel":
                self.db.execute("UPDATE audio_sessions SET stage='cancelled' WHERE token=?", (token,))
                await q.answer("Cancelling")
                await q.message.edit_text("✖ Cancellation requested. Waiting for the workflow to stop.")
            else:
                await q.answer("Already processing")
            return True
        if row["stage"] != "config":
            await q.answer("This request has already finished")
            return True
        if action == "generate":
            await q.answer()
            await self._generate(token, row, state)
            return True
        await q.answer()
        try:
            await self._action(token, state, action, q.from_user.id)
            await self._show(token, q.message.chat.id, state, old=q.message.message_id)
        except (GatewayError, ValueError) as exc:
            await self.bot.send_message(q.message.chat.id, "⚠️ " + html.escape(str(exc)))
        return True

    async def _action(self, token: str, state: dict[str, Any], action: str, actor: int) -> None:
        view = state["view"]
        model = self._model(state)
        if action == "back":
            state["view"] = "voices" if view == "voice_detail" else "home"
        elif action in {"models", "voices", "languages", "settings", "speakers", "timestamps",
                        "events", "outputs", "presets", "delivery", "advanced", "formats",
                        "normalization"}:
            state["view"] = action
        elif action == "speaker_boost" and model.get("supports_speaker_boost"):
            state["speaker_boost"] = not state["speaker_boost"]
        elif action.startswith("direction:") and model.get("delivery_directions"):
            index = int(action.split(":", 1)[1])
            directions = model["delivery_directions"]
            if not 0 <= index < len(directions):
                raise GatewayError("Delivery direction is invalid")
            state.update(delivery=str(directions[index]), view="home")
        elif action == "custom_direction" and model.get("supports_custom_direction"):
            state["view"] = "custom_direction"
        elif action.startswith("format:") and self._formats(model):
            index = int(action.split(":", 1)[1])
            formats = self._formats(model)
            if not 0 <= index < len(formats):
                raise GatewayError("Output format is unavailable")
            state.update(output_format=str(formats[index]), view="home")
        elif action.startswith("norm:") and model.get("supports_text_normalization"):
            value = action.split(":", 1)[1]
            if value not in {"auto", "on", "off"}:
                raise GatewayError("Text normalization is invalid")
            state.update(text_normalization=value, view="home")
        elif action == "voice_search":
            state["view"] = "voice_search"
        elif action.startswith("catalogpage:"):
            _, key, page_raw = action.split(":", 2)
            page = int(page_raw)
            if key not in {"models", "voices"} or not 0 <= page <= 10:
                raise GatewayError("Catalog page is invalid")
            state["model_page" if key == "models" else "voice_page"] = page
            state["view"] = key
        elif action.startswith("voice_detail:"):
            index = int(action.split(":", 1)[1])
            if not 0 <= index < len(state["voices"]):
                raise GatewayError("Voice is no longer available")
            state.update(selected_voice=index, view="voice_detail")
        elif action == "voice_preview":
            voice = state["voices"][state["selected_voice"]]
            ref = str(voice.get("preview_ref") or "")
            if not ref:
                raise GatewayError("This voice has no existing sample")
            data, mime = await self.gateway.fetch_file(ref, secrets.token_urlsafe(18),
                                                       {"audio/mpeg", "audio/ogg"})
            session = self.db.execute("SELECT chat_id FROM audio_sessions WHERE token=?",
                                      (token,)).fetchone()
            if not session:
                raise GatewayError("Audio session expired")
            await self.bot.send_audio(session[0], BufferedInputFile(data,
                "sample.mp3" if mime == "audio/mpeg" else "sample.ogg"))
        elif action == "reset":
            self._reset_controls(state)
            state["view"] = "home"
        elif action.startswith("select:"):
            _, key, selected_number = action.split(":", 2)
            idx = int(selected_number)
            if key not in {"models", "voices"} or not 0 <= idx < len(state[key]):
                raise GatewayError("Selection expired")
            if key == "models":
                state["model"] = idx
                self._reset_controls(state)
                if state["task"] == "tts":
                    voices = self._catalog(await self.gateway.get_voices(
                        model_id=str(state["models"][idx]["id"])))
                    voices = [x for x in voices if x.get("id") and x.get("name") and
                              x.get("free_entitled") is True]
                    if not voices:
                        raise GatewayError("No free-eligible voice supports this model")
                    state["voices"] = voices[:80]
                    state["voice"] = 0
            else:
                state["voice"] = idx
            state["view"] = "home"
        elif action.startswith("language:"):
            code = action.split(":", 1)[1]
            allowed = {str(x.get("code")) for x in model.get("languages", [])
                       if isinstance(x, dict)} | {"auto"}
            if code not in allowed or state["task"] == "tts" and code != "auto" and not \
                    model.get("supports_language_code"):
                raise GatewayError("Language is unsupported by this model")
            state.update(language=code, view="home")
        elif action.startswith("control:"):
            name = action.split(":", 1)[1]
            if name not in state["controls"]:
                raise GatewayError("Control is unsupported by this model")
            state["view"] = action
        elif action.startswith(("delta:", "value:")) and view.startswith("control:"):
            name = view.split(":", 1)[1]
            info = model["controls"][name]
            current_control = float(state["controls"][name])
            multiplier = 100 if float(info.get("max", 1)) <= 2 else 1
            adjustment = float(action.split(":", 1)[1]) / multiplier
            if not math.isfinite(adjustment) or abs(adjustment) > 100:
                raise GatewayError("Invalid control value")
            target = current_control + adjustment if action.startswith("delta:") else adjustment
            state["controls"][name] = round(max(float(info.get("min", 0)),
                min(float(info.get("max", 1)), target)), 3)
        elif action == "exact" and view.startswith("control:"):
            state["view"] = "exact:" + view.split(":", 1)[1]
        elif action.startswith("speaker:"):
            maximum = min(32, int(model.get("max_speakers") or 0))
            if not maximum:
                raise GatewayError("Speaker count is unsupported by this model")
            speaker_value = action.split(":", 1)[1]
            if speaker_value == "plus":
                state["speakers"] = min(maximum, max(1, state["speakers"] + 1))
            elif speaker_value == "minus":
                state["speakers"] = max(0, state["speakers"] - 1)
            elif speaker_value.isdigit() and 0 <= int(speaker_value) <= maximum:
                state["speakers"] = int(speaker_value)
            else:
                raise GatewayError("Invalid speaker count")
        elif action == "diarize":
            if not model.get("supports_diarize"):
                raise GatewayError("Diarization is unsupported by this model")
            state["diarize"] = not state["diarize"]
        elif action.startswith("timestamp:"):
            choice = action.split(":", 1)[1]
            if choice not in model.get("timestamps", ["none"]):
                raise GatewayError("Timestamp option is unavailable")
            state["timestamps"] = choice
            if choice == "none" and state["output"] in {"timed", "srt", "vtt"}:
                state["output"] = "plain"
            state["view"] = "home"
        elif action.startswith("event:"):
            if not model.get("supports_audio_events"):
                raise GatewayError("Audio event detection is unavailable")
            state["audio_events"] = action.endswith(":on")
            state["view"] = "home"
        elif action.startswith("output:"):
            choice = action.split(":", 1)[1]
            if choice not in {"plain", "labeled", "timed", "txt", "srt", "vtt"} or \
                    choice in {"timed", "srt", "vtt"} and state["timestamps"] == "none":
                raise GatewayError("Timed output requires real timestamps")
            state.update(output=choice, view="home")
        elif action == "continue":
            state["view"] = "preview" if token in self.inputs else "prompt"
        elif action == "savepreset":
            state["view"] = "preset_name"
        elif action.startswith("preset_rename:"):
            name = action.split(":", 1)[1]
            if not self.db.execute("SELECT 1 FROM audio_presets WHERE actor_id=? "
                    "AND task=? AND name=?", (actor, state["task"], name)).fetchone():
                raise GatewayError("Preset no longer exists")
            state.update(view="preset_rename", pending_preset=name)
        elif action.startswith("preset_delete:"):
            name = action.split(":", 1)[1]
            if not self.db.execute("SELECT 1 FROM audio_presets WHERE actor_id=? "
                    "AND task=? AND name=?", (actor, state["task"], name)).fetchone():
                raise GatewayError("Preset no longer exists")
            state.update(view="preset_delete", pending_preset=name)
        elif action == "preset_confirm" and view == "preset_delete":
            self.db.execute("DELETE FROM audio_presets WHERE actor_id=? AND task=? AND name=?",
                            (actor, state["task"], state["pending_preset"]))
            state.pop("pending_preset", None)
            state["view"] = "presets"
        elif action.startswith("preset_default:"):
            name = action.split(":", 1)[1]
            row = self.db.execute("SELECT settings_json FROM audio_presets WHERE "
                "actor_id=? AND task=? AND name=?", (actor, state["task"], name)).fetchone()
            if not row:
                raise GatewayError("Preset no longer exists")
            self.db.execute("INSERT INTO audio_presets VALUES (?,?,?,?,?) "
                "ON CONFLICT(actor_id,task,name) DO UPDATE SET "
                "settings_json=excluded.settings_json,created=excluded.created",
                (actor, state["task"], "Default", row[0], time.time()))
            state["view"] = "presets"
        elif action.startswith("preset:"):
            name = action.split(":", 1)[1]
            row = self.db.execute("SELECT settings_json FROM audio_presets WHERE "
                "actor_id=? AND task=? AND name=?", (actor, state["task"], name)).fetchone()
            if not row:
                raise GatewayError("Preset no longer exists")
            saved = json.loads(row[0])
            preset_model_index = next((n for n, entry in enumerate(state["models"])
                                       if entry.get("id") == saved.get("model_id")), None)
            if preset_model_index is None:
                raise GatewayError("Saved model is no longer available")
            state["model"] = preset_model_index
            self._reset_controls(state)
            if state["task"] == "tts":
                voices = self._catalog(await self.gateway.get_voices(
                    model_id=str(state["models"][preset_model_index]["id"])))
                voices = [x for x in voices if x.get("id") and x.get("name") and
                          x.get("free_entitled") is True]
                voices = voices[:80]
                voice_index = next((n for n, entry in enumerate(voices)
                    if entry.get("id") == saved.get("voice_id")), None)
                if voice_index is None:
                    raise GatewayError("Saved voice is no longer available")
                state.update(voices=voices, voice=voice_index)
            for key in ("language", "diarize", "speakers", "timestamps", "audio_events",
                        "output", "delivery", "speaker_boost", "text_normalization",
                        "output_format"):
                if key in saved:
                    state[key] = saved[key]
            selected_model = self._model(state)
            if state["output_format"] not in self._formats(selected_model):
                state["output_format"] = (self._formats(selected_model) or [""])[0]
            if state["language"] != "auto" and state["language"] not in {
                    str(x.get("code")) for x in selected_model.get("languages", [])
                    if isinstance(x, dict)}:
                state["language"] = "auto"
            if state["task"] == "tts" and not selected_model.get("supports_language_code"):
                state["language"] = "auto"
            if state["timestamps"] not in selected_model.get("timestamps", ["none"]):
                state["timestamps"] = "none"
            if state["timestamps"] == "none" and state["output"] in {"timed", "srt", "vtt"}:
                state["output"] = "plain"
            state["speakers"] = min(int(state.get("speakers") or 0),
                min(32, int(selected_model.get("max_speakers") or 0)))
            if not selected_model.get("supports_diarize"):
                state["diarize"] = False
            if not selected_model.get("supports_audio_events"):
                state["audio_events"] = False
            if not selected_model.get("supports_speaker_boost"):
                state["speaker_boost"] = False
            if state["delivery"] != "Natural" and not (
                state["delivery"] in selected_model.get("delivery_directions", []) or
                selected_model.get("supports_custom_direction")):
                state["delivery"] = "Natural"
            for key, value in saved.get("controls", {}).items():
                info = self._model(state).get("controls", {}).get(key) or {}
                if key in state["controls"] and isinstance(value, (int, float)) and \
                        isinstance(info.get("min"), (int, float)) and \
                        isinstance(info.get("max"), (int, float)) and \
                        info["min"] <= value <= info["max"]:
                    state["controls"][key] = value
            state["view"] = "home"
        else:
            raise GatewayError("This audio option is unavailable")

    async def reply(self, m: Message) -> bool:
        if not m.from_user or not m.reply_to_message:
            return False
        rows = self.db.execute("SELECT * FROM audio_sessions WHERE actor_id=? AND chat_id=? "
            "AND message_id=? AND expires>? AND stage='config'",
            (m.from_user.id, m.chat.id, m.reply_to_message.message_id,
             time.time())).fetchall()
        if len(rows) != 1:
            return False
        row = rows[0]
        state = json.loads(row["state_json"])
        if state["view"] == "prompt":
            if state["task"] == "tts":
                if not m.text or not 1 <= len(m.text) <= 3000:
                    await m.reply("Reply with 1–3,000 text characters.")
                    return True
                self.inputs[row["token"]] = m.text
            else:
                media = _audio_from_message(m)
                if not media or media[2] > 20_000_000 or media[3] > 600:
                    await m.reply("Reply with an audio/voice/video file under 20 MB and 10 minutes.")
                    return True
                self.inputs[row["token"]] = media
            state["view"] = "preview"
        elif state["view"] == "voice_search":
            if not m.text or not 1 <= len(m.text) <= 80:
                await m.reply("Reply with a voice name of 1–80 characters.")
                return True
            try:
                voices = self._catalog(await self.gateway.get_voices(
                    query=m.text.strip(), model_id=str(self._model(state)["id"])))
            except GatewayError as exc:
                await m.reply("⚠️ " + html.escape(str(exc)))
                return True
            voices = [x for x in voices if x.get("id") and x.get("name") and
                      x.get("free_entitled") is True]
            if not voices:
                await m.reply("No compatible voice matched this search.")
                return True
            state.update(voices=voices[:80], voice_page=0, view="voices")
        elif state["view"] == "custom_direction":
            if not m.text or not 1 <= len(m.text.strip()) <= 80:
                await m.reply("Use a short delivery direction (1–80 characters).")
                return True
            state.update(delivery=m.text.strip(), view="home")
        elif state["view"] == "preset_name":
            name = (m.text or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9 _-]{1,24}", name):
                await m.reply("Use 1–24 English letters, digits, spaces, _ or -.")
                return True
            existing = self.db.execute("SELECT 1 FROM audio_presets WHERE actor_id=? "
                "AND task=? AND name=?", (m.from_user.id, state["task"], name)).fetchone()
            count = self.db.execute("SELECT COUNT(*) FROM audio_presets WHERE actor_id=? "
                "AND task=?", (m.from_user.id, state["task"])).fetchone()[0]
            if not existing and count >= 10:
                await m.reply("Maximum 10 presets per audio task. Delete one first.")
                return True
            selected = state["models"][state["model"]]
            saved = {key: state[key] for key in ("controls", "language", "diarize",
                "speakers", "timestamps", "audio_events", "output", "delivery",
                "speaker_boost", "text_normalization", "output_format")}
            saved["model_id"] = selected["id"]
            if state["task"] == "tts":
                saved["voice_id"] = state["voices"][state["voice"]]["id"]
            self.db.execute("INSERT INTO audio_presets VALUES (?,?,?,?,?) "
                "ON CONFLICT(actor_id,task,name) DO UPDATE SET "
                "settings_json=excluded.settings_json,created=excluded.created",
                (m.from_user.id, state["task"], name, json.dumps(saved), time.time()))
            state["view"] = "presets"
        elif state["view"] == "preset_rename":
            new_name = (m.text or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9 _-]{1,24}", new_name):
                await m.reply("Use 1–24 English letters, digits, spaces, _ or -.")
                return True
            try:
                self.db.execute("UPDATE audio_presets SET name=? WHERE actor_id=? "
                    "AND task=? AND name=?", (new_name, m.from_user.id, state["task"],
                    state["pending_preset"]))
            except sqlite3.IntegrityError:
                await m.reply("A preset with that name already exists.")
                return True
            state.pop("pending_preset", None)
            state["view"] = "presets"
        elif state["view"].startswith("exact:"):
            name = state["view"].split(":", 1)[1]
            info = self._model(state)["controls"][name]
            try:
                value = float(m.text or "")
            except ValueError:
                await m.reply("Enter a number within the displayed range.")
                return True
            if not float(info.get("min", 0)) <= value <= float(info.get("max", 1)):
                await m.reply("Value is outside the model-supported range.")
                return True
            state["controls"][name] = value
            state["view"] = f"control:{name}"
        else:
            return False
        await self._show(row["token"], m.chat.id, state, old=row["message_id"])
        return True

    async def _generate(self, token: str, row: sqlite3.Row, state: dict[str, Any]) -> None:
        chat, actor, task = row["chat_id"], row["actor_id"], state["task"]
        source = self.inputs.get(token)
        if source is None or not self.authorize(actor, chat) or not \
                self._quota_available(actor, chat, task):
            await self.bot.send_message(chat, "⚠️ Input missing or daily request cap reached.")
            return
        # Fail closed when n8n has not reported measured free entitlement.
        try:
            catalog = (await self.gateway.get_capabilities()).get("result", {})
            if catalog.get("free_quota_source") != "provider_reported" or not \
                    isinstance(catalog.get("free_remaining"), (int, float)) or \
                    catalog["free_remaining"] <= 0:
                raise GatewayError("Provider-reported free quota is unavailable")
        except GatewayError as exc:
            await self.bot.send_message(chat, "⚠️ " + html.escape(str(exc)))
            return
        request_id = secrets.token_urlsafe(18)
        changed = self.db.execute("UPDATE audio_sessions SET stage='processing', request_id=? "
            "WHERE token=? AND stage='config' AND expires>?",
            (request_id, token, time.time())).rowcount
        if changed != 1:
            return
        if not self._reserve_quota(actor, chat, task):
            self.db.execute("UPDATE audio_sessions SET stage='failed' WHERE token=?", (token,))
            await self.bot.send_message(chat, "Daily audio request cap reached.")
            return
        state["stage"] = "processing"
        await self._show(token, chat, state, old=row["message_id"])
        try:
            if not self.authorize(actor, chat):
                raise GatewayError("Access was revoked before processing")
            model = self._model(state)
            options: dict[str, Any] = {"provider": "elevenlabs", "model_id": model["id"]}
            input_data: dict[str, Any]
            if task == "tts":
                options["voice_id"] = state["voices"][state["voice"]]["id"]
                options["voice_settings"] = dict(state["controls"])
                if model.get("supports_speaker_boost"):
                    options["voice_settings"]["use_speaker_boost"] = state["speaker_boost"]
                if state["language"] != "auto" and model.get("supports_language_code"):
                    options["language_code"] = state["language"]
                formats = self._formats(model)
                if state["output_format"] not in formats:
                    raise GatewayError("No verified Telegram-compatible output format is available")
                options["output_format"] = state["output_format"]
                if state["delivery"] != "Natural" and model.get("delivery_directions"):
                    options["delivery_direction"] = state["delivery"]
                if model.get("supports_text_normalization"):
                    options["text_normalization"] = state["text_normalization"]
                input_data = {"text": str(source)}
            else:
                if not isinstance(source, tuple):
                    raise GatewayError("Audio input is missing")
                file = await self.bot.get_file(source[0])
                if file.file_size and file.file_size > 20_000_000:
                    raise GatewayError("Telegram media exceeds the request limit")
                if not file.file_path:
                    raise GatewayError("Telegram media path is unavailable")
                buffer = io.BytesIO()
                await self.bot.download_file(file.file_path, buffer)
                if buffer.tell() > 20_000_000:
                    raise GatewayError("Downloaded audio exceeds the request limit")
                file_ref = await self.gateway.upload(buffer.getvalue(), source[1], request_id)
                input_data = {"file_ref": file_ref, "mime_type": source[1],
                              "duration_seconds": source[3] or None}
                if state["language"] != "auto":
                    options["language_code"] = state["language"]
                if model.get("supports_diarize"):
                    options["diarize"] = state["diarize"]
                if model.get("max_speakers") and state["speakers"]:
                    options["num_speakers"] = state["speakers"]
                if state["timestamps"] in model.get("timestamps", []):
                    options["timestamps_granularity"] = state["timestamps"]
                if model.get("supports_audio_events"):
                    options["tag_audio_events"] = state["audio_events"]
                options["output"] = state["output"]
            result = await self.gateway.submit(task,
                {"actor_id": actor, "chat_id": chat, "locale": "en", "source": "telegram"},
                input_data, options, request_id)
            if result["status"] == "accepted":
                job_id = result["job_id"]
                self.db.execute("UPDATE audio_sessions SET job_id=? WHERE token=?",
                                (job_id, token))
                for _ in range(20):
                    await asyncio.sleep(self.poll_seconds)
                    current = self.db.execute("SELECT stage FROM audio_sessions WHERE token=?",
                                              (token,)).fetchone()
                    if not current or current[0] == "cancelled":
                        await self.gateway.cancel_job(job_id)
                        raise GatewayError("Request cancelled")
                    result = await self.gateway.get_job(job_id)
                    if result["status"] not in {"pending", "running", "accepted"}:
                        break
                else:
                    raise GatewayError("Provider job is still running; check workflow status")
            if result["status"] != "succeeded":
                error = result.get("error") or {}
                safe_codes = {"QUOTA_EXHAUSTED": "Free quota reached",
                              "UNSUPPORTED_MODEL": "Selected model is unavailable",
                              "INVALID_MEDIA": "Provider rejected the audio file",
                              "NO_ENTITLEMENT": "This account is not entitled to this task"}
                raise GatewayError(safe_codes.get(str(error.get("code")),
                                                    "Provider task failed"))
            current = self.db.execute("SELECT stage FROM audio_sessions WHERE token=?",
                                      (token,)).fetchone()
            if not current or current[0] == "cancelled":
                raise GatewayError("Request cancelled")
            if not self.authorize(actor, chat):
                raise GatewayError("Access was revoked before delivery")
            await self._deliver(chat, result["result"], request_id, task,
                                state["output"], actor)
            self.db.execute("UPDATE audio_sessions SET stage='done' WHERE token=?", (token,))
            await self._finish_message(token, actor, chat, "✅ Audio task completed.")
        except (GatewayError, TelegramBadRequest) as exc:
            self.db.execute("UPDATE audio_sessions SET stage='failed' WHERE token=?", (token,))
            if self.authorize(actor, chat):
                await self.bot.send_message(chat, "⚠️ " + html.escape(str(exc)[:250]))
                await self._finish_message(token, actor, chat, "⚠️ Audio task stopped.")
        finally:
            self.inputs.pop(token, None)

    async def _finish_message(self, token: str, actor: int, chat: int, body: str) -> None:
        if not self.authorize(actor, chat):
            return
        row = self.db.execute("SELECT message_id FROM audio_sessions WHERE token=?",
                              (token,)).fetchone()
        if row and row[0]:
            try:
                await self.bot.edit_message_text(body, chat_id=chat, message_id=int(row[0]),
                                                 reply_markup=None)
            except TelegramBadRequest:
                pass

    async def _deliver(self, chat: int, result: dict[str, Any], request_id: str,
                       task: str, output: str, actor: int) -> None:
        kind = result["kind"]
        if kind in {"text", "json_transcript"}:
            text = str(result.get("text") or "")
            if not text or len(text) > 100_000:
                raise GatewayError("Provider returned no usable transcript")
            if output in {"srt", "vtt", "timed"}:
                words = result.get("words") or []
                if not isinstance(words, list) or not words or any(
                        not isinstance(w, dict) or not isinstance(w.get("start"), (int, float))
                        or not isinstance(w.get("end"), (int, float)) for w in words):
                    raise GatewayError("Provider returned no real word timestamps")
                def stamp(seconds: float, sep: str) -> str:
                    millis = max(0, int(seconds * 1000))
                    hours, remainder = divmod(millis, 3_600_000)
                    minutes, remainder = divmod(remainder, 60_000)
                    secs, fraction = divmod(remainder, 1000)
                    return f"{hours:02}:{minutes:02}:{secs:02}{sep}{fraction:03}"
                blocks: list[str] = []
                for index in range(0, len(words), 8):
                    group = words[index:index + 8]
                    line = " ".join(str(w.get("text", "")).strip() for w in group).strip()
                    if not line:
                        continue
                    sep = "." if output == "vtt" else ","
                    timing = f"{stamp(group[0]['start'], sep)} --> {stamp(group[-1]['end'], sep)}"
                    blocks.append((f"{len(blocks)+1}\n" if output == "srt" else "") +
                                  timing + "\n" + line)
                if not blocks:
                    raise GatewayError("Provider returned empty timed words")
                timed_text = ("WEBVTT\n\n" if output == "vtt" else "") + \
                    "\n\n".join(blocks) + "\n"
                if output in {"srt", "vtt"}:
                    if not self.authorize(actor, chat):
                        return
                    await self.bot.send_document(chat, BufferedInputFile(timed_text.encode(),
                        "transcript." + output), caption="📝 Timed transcript")
                    return
                text = timed_text
            elif output == "labeled" and result.get("words"):
                words = result["words"]
                if isinstance(words, list) and all(isinstance(w, dict) for w in words):
                    lines, speaker = [], None
                    for word in words:
                        current = word.get("speaker_id")
                        if current != speaker:
                            speaker = current
                            lines.append(f"\nSpeaker {current}:" if current is not None else "\n")
                        lines.append(str(word.get("text", "")))
                    text = " ".join(lines).strip()
            if output == "txt":
                if not self.authorize(actor, chat):
                    return
                await self.bot.send_document(chat, BufferedInputFile(text.encode(),
                    "transcript.txt"), caption="📝 Transcript")
                return
            if len(text) <= 3900:
                if not self.authorize(actor, chat):
                    return
                await self.bot.send_message(chat, html.escape(text))
            else:
                if not self.authorize(actor, chat):
                    return
                await self.bot.send_document(chat, BufferedInputFile(text.encode(), "transcript.txt"),
                                             caption="📝 Transcript")
            return
        if task == "tts" and kind in {"audio", "voice"}:
            audio_bytes, mime = await self.gateway.fetch_file(result["file_ref"], request_id,
                {"audio/mpeg", "audio/ogg"})
            if mime == "audio/mpeg" and audio_bytes[:3] != b"ID3" and \
                    not (audio_bytes[0] == 0xff and audio_bytes[1] & 0xe0 == 0xe0):
                raise GatewayError("Provider audio is not a valid MP3")
            if mime == "audio/ogg" and not audio_bytes.startswith(b"OggS"):
                raise GatewayError("Provider audio is not a valid OGG file")
            if kind == "voice" and mime == "audio/ogg" and b"OpusHead" in audio_bytes[:100]:
                if not self.authorize(actor, chat):
                    return
                await self.bot.send_voice(chat, BufferedInputFile(audio_bytes, "speech.ogg"))
            else:
                if not self.authorize(actor, chat):
                    return
                await self.bot.send_audio(chat, BufferedInputFile(audio_bytes,
                    "speech.mp3" if mime == "audio/mpeg" else "speech.ogg"))
            return
        raise GatewayError("Provider returned an unsupported output kind")
