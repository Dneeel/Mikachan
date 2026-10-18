# Existing system audit and integration map — 2026-10-17

## Source and baseline

Audited active `v2` at `36435d5` before edits, including the first-party Python modules, tests, runtime configuration, deployment templates and engineering records. The checkout's remote default branch was `v1`; `v2` is the intended integration branch. In a fresh locked environment, the pre-change baseline passed **51 pytest tests**, Ruff and mypy. These are offline checks. The repository's prior live Telegram and Freestyle VM receipts belong to earlier deployed revisions; they do not prove this integrated build has been deployed or accepted.

## Runtime and boundaries

| Path / interface | Actual behavior and extension point |
| --- | --- |
| `src/mikachan/cli.py`, `__main__.py`, `config.py` | `mikachan run` constructs `App(Settings())`; doctor inspects dependencies. Pydantic settings read `.env`. One bot token and one process. Legacy package, SQLite filename and service name remain for compatibility. |
| `bot.py:App` | One aiogram `Dispatcher`/`Router`, one message handler, one callback handler, and one `start_polling` call. `App.run` also starts one downloader `Worker.loop`. Community and Utilities now dispatch inside these handlers; there is no competing update consumer. |
| `security.py` | Numeric sender allowlist and chat-type checks; URL syntax/host classification and fetch-time public-DNS checks. Group authorization now requires the numeric user allowlist **and** a separately approved numeric group ID, with `PRIVATE_CHATS_ONLY=false`. URLs still reach a consent prompt before work. |
| `store.py` | One SQLite database in WAL mode. `Store.decision` checks initiating user, chat, prompt ID, pending state and expiry inside an immediate transaction, then creates a durable job. Search sessions bind user/chat/generation; cache keys hold Telegram file IDs per owner. Recovery distinguishes retryable work from uncertain delivery. New Community and Utilities tables are additive `CREATE TABLE IF NOT EXISTS` changes. |
| `search.py`, `catalog.py`, `matching.py`, `podcasts.py`, `music_ui.py` | Bounded federated search across Apple, Deezer, SoundCloud, YouTube, YouTube Music, Spotify and Apple Podcasts; independent kind/source filters; catalog identity and public recording matching; feed/collection browsing; attributed lyrics lookup. Spotify official API requires optional credentials. Search metadata is not licensed catalog audio. |
| `jobs.py`, `media.py` | At most two downloader jobs globally and one per user. On-demand yt-dlp/gallery-dl/direct transfer, ffprobe verification, MP3 or Telegram-compatible conversion where needed, Telegram receipts and owner-scoped file-ID cache. Failed/ambiguous delivery is recorded conservatively. Downloader engines and matching were left intact. |
| `community.py`, `personality.py`, `utilities.py`, `render_tgs.py` | New peer domains. Group data, reviewed content, sessions and usage share the existing SQLite connection. Utility commands use the same Bot object. TGS rendering runs in a bounded child process. Numeric-ID persona selection is a small shared contract; `/help` exercises it in private chat. |
| `pyproject.toml`, `uv.lock`, `deploy/` | Locked Python dependencies and existing systemd/Ubuntu templates. No service was deployed or modified. New utilities need Pillow, QR, ZXing, Jalali calendar, Lottie/CairoSVG; English OCR requires a local Tesseract executable. |

## Downloader behavior retained

Eligible public HTTP(S) links in authorized text still produce separate **Yes/No** prompts (up to five links); they do not start downloads. No removes the bot's prompt when Telegram allows. Yes creates a job. `/twitter` remains; `/x` is absent. `/music`, `/podcast`, `/search`, search filters, paginated results, public-media selection, collection browsing, `/queue`, `/cancel`, `/status`, source guidance and the worker are retained. The old unapproved `/start` dashboard and its stale menu actions were removed. Unauthorized senders and stale callbacks cannot invoke the new domains through the dispatcher.

## Risks and status at intake

- Extractors and proxies can follow media URLs outside the local DNS validation path; the existing [security record](SECURITY.md) calls for restrictive egress before accepting less-trusted users.
- Source availability changes; Instagram story and some carousel access, TikTok music pages, private catalog audio and the expired adult-site sample were not proven working. Several historical source receipts exist, but the current `v2` checkout was not live-tested against Telegram in this task.
- `jobs.py` has multiple delivery paths and cancellation at delivery time can be uncertain. Revocation cancels queued/active jobs through the owner path, but acceptance must still exercise revocation during a real upload.
- New cultural features require reviewed, permissioned content. The catalog starts empty; nothing generated or misattributed is seeded.
- Group access, global user allowlisting, Mommy-mode membership and group nicknames are separate concepts. Nicknames never authorize actions and never carry across groups or into private chats.

## Product decision reconciliation

The attached blueprint's earlier Persian-first interface and blanket group-member
access are superseded by Mikachan's English interface and strict per-user numeric
authorization. Exact Persian triggers (`فال`, `شعر`, `ارز`, `ترجمه …`) and authentic
Persian content remain. `v1` is retired; Community, Utilities, and the terminal
manager are integrated on `v2`.
