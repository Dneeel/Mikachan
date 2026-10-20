# CoinPJ photo + caption relay (v2 hotfix)

## Root cause

- `UserbotBridge.currency()` extracted only `answer.raw_text`; the incoming `Message.photo` was never downloaded.
- `App.message()` sent that value with `m.reply(...)`, a text-only Bot API operation.
- The former public `/rate` path used `CoinPJ.quote()` to produce a text-only parsed card, ignoring the source photo too. That slash command was subsequently removed; Persian asset requests and normal currency text still use verified CoinPJ handlers.

## New behavior

Both CoinPJ paths now obtain the original response message's attached JPEG, PNG or WebP image through Telethon **after verifying the provider sender**. The image bytes are passed to the existing aiogram bot, with the HTML-safe quote as the image caption. This is a bot-originated photo, not a Userbot forward. User-session credentials are never included in the delivery.

- **Short caption:** one Telegram photo with the complete quote as its caption.
- **Long caption:** photo with a short query header followed by a separate message containing the complete quote; no truncation.
- **Telegram rejects photo:** fall back to sending the image as a document, keeping the caption.
- **Media download fails / image too large:** preserve the quote text and explicitly warn that the image could not be retrieved.
- **Provider sends text without an image:** preserve text-only behavior.
- **Existing `currency()` and `quote()` callers:** text-returning interfaces are retained; `currency_message()` and `quote_media()` supply rich responses.

A maximum of 9 MB of image bytes is accepted for the normal Bot API photo path. No staging chat, additional login, API key, or service is needed. The existing per-bridge lock and request throttling remain intact.

## Verification completed in this isolated environment

- 11 new mock-driven photo relay tests passed.
- 16 existing Userbot bridge tests passed.
- All Python sources/tests compile successfully.
- The ZIP integrity and hotfix patch applicability to the *provided combined source snapshot* were checked.

**Not yet verified:** a live reply from `@CoinPJbot` and a real outgoing photo from Telegram Bot API. Neither the installed Telethon and aiogram packages nor a VPS Userbot session are available in this offline environment. Do not claim a live success without running the smoke tests below.

## Recommended VPS smoke test

1. Back up the running project and configuration; make sure the installed checkout matches the snapshot before applying this patch.
2. From the project checkout: `git apply --check /path/to/Mikachan_v2_CoinPJ_Photo_Caption_Hotfix.patch` before applying it; never force a failed patch.
3. In the VPS Python environment, run: `python -m pytest -q tests/test_coinpj_media.py tests/test_userbot_bridge.py tests/test_coinpj.py`.
4. Restart the managed bot service through the project's existing deployment manager.
5. In an authorized chat, send `usd`, `100 usd`, `2300 jpy to usd`, `دلار به ین`, and `1200 دلار`.
6. Verify the sender is Mikachan and that **the photo and its complete caption arrive**. Test one text-only provider response if feasible. Confirm no media/session bytes appear in logs.

Do not copy a ZIP over an unrelated live checkout or an active DB. Deploy from the authorized `v2` branch through the existing release/rollback flow.
