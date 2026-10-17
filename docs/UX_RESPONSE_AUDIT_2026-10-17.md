# Mikachan v2: current Telegram response and interface audit (2026-10-17)

This is the **implemented interface**, read from `bot.py`, `community.py`, `utilities.py`, `market_prices.py`, `personality.py`, and the existing downloader presentation. Examples with values are illustrative; they are not a live Telegram conversation or a promise of current market prices. Markdown quotes show bot text after Telegram HTML rendering. Live inbound acceptance of this revision is pending.

## Entry, access and identity

| User input or event | Current visible behavior |
| --- | --- |
| Unknown numeric user, disallowed group, `/start`, ordinary chat or unrelated media | Silent. There is no `/start` dashboard. Callback queries from unauthorized users get only Telegram callback acknowledgement. |
| `/help` in private | A bold `Mikachan · Help` heading followed by five dense Downloader, Community, Utilities, Owner lines. It ends `Ready when you are.` in NORMAL mode. For an authorized configured Mommy ID it ends `I'm here, darling. Tell me what you need.` |
| `/help` in an authorized group | The same full help, always NORMAL, including owner/admin commands the reader may not use. |
| Exact `فال`, `شعر`, `دلار`, `سکه`, `ارز`, `طلا`, `تتر`, `بیت‌کوین`, `بیت کوین` | Routes to its registered feature. Similar sentences such as `قیمت دلار` or `دلار؟` are silent. `ترجمه انگلیسی TEXT`, `ترجمه فارسی TEXT`, and `ارز SYMBOL IRT` are deliberate prefix forms. |

The personality contract has two templates (`ready`, `unavailable`) but only `ready` is called by the current Telegram interface, at `/help`. The rest of the bot uses fixed English technical wording. A group nickname is only substituted into an enabled welcome message; it does not affect prices, moderation, downloader, or private chats.

## Downloader presentation (behavior retained, engine untouched)

| Input or state | Current visible response |
| --- | --- |
| Eligible link | `🔗 **Twitter** · Link 1 of 1` + URL + `Download this item?`, buttons `✅ Yes`, `❌ No`. Source label follows classified URL. |
| `❌ No` | Deletes the confirmation message if Telegram permits; otherwise removes the keyboard where possible. |
| `✅ Yes` | Edits prompt to `⏳ Preparing your download…`, button `Cancel ✕`; worker stages display `🔎 Checking the link…`, `⬇️ Downloading media…`, `🎛 Preparing media…`, `📤 Sending to Telegram…`. |
| `/twitter` | `Send a public Twitter link. Every link asks for Yes/No confirmation first.` There is no `/x` command. |
| `/search`, `/music`, `/podcast` without query | `Send your search query in the next 10 minutes.` Next valid text is searched. |
| Search | `🔎 Searching…`, then a bold query/page heading and numbered cards with source/creator; `🧩 Types`, `🌐 Sources`, navigation, refresh, close, and result buttons. Empty search says `No matching results. Select at least one type and source, or try another query.` |
| `/queue` empty; `/status` | `No jobs.`; `Jobs: N recent · Active: M`. `/cancel` opens job buttons or says `No cancellable jobs.` |
| File delivered | Caption comes from source metadata/description, sometimes with `👤` creator or `🎵` title. Available actions can include `🔗 Link`, `🎵 MP3`, `📝 Lyrics`, `ℹ️ Details`, `📄 Transcript`. The Transcript action currently says `📄 Episode transcription: coming soon`. |

## Community presentation

| Input or event | Current visible response |
| --- | --- |
| `/fal` or `فال`; `/joke`; `/quote` on empty catalog | `No reviewed content is available for this feature yet.` Reviewed imported content instead shows its body and `— AUTHOR · SOURCE`. No built-in content is seeded. |
| `/poem` or `شعر` | `Poetry: choose an option.` Buttons: `Random poem`, `Choose poet`, `Search poetry`. Empty menu action says `No reviewed poetry is available yet.` Search asks for a reply to its prompt. |
| `/dice` | Telegram's native 🎲 dice animation, with no extra text. |
| `/rules` when unset; `/note foo` missing | `No rules have been set.`; `Note not found. Use /note NAME.` |
| `/groupsettings` | `Group settings: welcome off; culture: on, notes: on, utilities: on. Admins: /welcome on|off|TEXT; /groupsettings MODULE on|off.` Values follow stored settings. |
| `/welcome on` by admin; joining allowed user | `Welcome setting saved.`; then `Welcome, NAME.` unless an admin configured another template. Unknown joining members are not welcomed. |
| `/setrules`, `/setnote`, `/setcmd`, `/nickname` by admin | `Rules saved.`, `Saved.`, `Saved.`, `Group nickname saved.` respectively; custom command returns exactly the owner/admin stored text. |
| `/del`, `/mute` by admin | Replies `completed` on Telegram success, `failed: ExceptionType` on failure; missing/invalid target says `Reply to a non-admin member's message.` `/mute` lasts 10 minutes. |
| `/groupstats` | `Observed authorized message requests to Mikachan in this group, last 30 UTC days: N. Distinct requesters: M. Button actions and ordinary conversation are not counted.` |
| `/modlog` by admin when empty | `No Mika actions recorded.` (legacy name remains in this one message). |

## Utilities presentation

| Input | Current visible response |
| --- | --- |
| `/qr Hello` | Replies with a PNG QR photo, no caption. Missing text says `Use /qr TEXT or reply to text with /qr`. |
| `/scan` replying to image | Decoded strings on separate lines, or `No readable code found.` |
| `/ocr` replying to image | Recognized text; if only English OCR data is installed it appends `English OCR only on this host.` If Persian data is explicitly requested but absent: `Persian OCR data is not installed on this host`. |
| `/image png`, `/image jpeg`, `/image webp` replying to image | Sends `image.EXT` document. `/sticker` replying to photo sends a static WebP sticker. `/stickerimage` replying to static sticker sends `sticker.png`. `/gif` replying to animated TGS sends `sticker.gif`. Wrong input returns specific English guidance. |
| `/pack create|add|remove ...` | On create/add: `Sticker pack updated: https://t.me/addstickers/NAME`; on remove: `Sticker removed from your pack.` This requires Telegram acceptance. |
| `/convert json` on CSV or `/convert csv` on JSON | Sends `converted.json` or `converted.csv`; unsupported pair says `Reply to a .csv or .json document with /convert json|csv`. |
| `/weather CITY` | `CITY, COUNTRY · Open-Meteo model`, current weather/temperature/precipitation/wind line, then three daily range lines. Ambiguous city asks to refine with city, country and region. |
| `/rate USD IRT`, exact `دلار` | Multi-line English source comparison headed `Iran market · Toman per unit · indicative quotes`; may show a consensus median, individual source ranges, timestamps and caveats. Cash USD and USDT are separate. See sample below. |
| `/rate USD EUR` | One plain line such as `USD/EUR: VALUE · Frankfurter reference rate · DATE`. `/rate USD IRR` explains official IRR does not represent Iran's open market. |
| `/time Asia/Tehran` | `Asia/Tehran: WEEKDAY YYYY-MM-DD HH:MM +0330` and `Jalali: YYYY-MM-DD`. `/time add`, `remove`, `list` manage saved zones. |
| `/apod` | `NASA APOD · DATE · TYPE`, title, explanation, credit, and media URL; it does not render a Telegram image preview itself. |
| `/translate en TEXT` | If unconfigured: `Translation provider is not configured. Text was not sent.` With a configured HTTPS service: `EN · external translation provider` and translated text. |
| Remote provider HTTP error | `External provider request failed. Try again later.` Credential-bearing request URLs are suppressed in user replies. |

Example generated with two **mocked** source values (263,800 and 264,200 Toman), using the actual formatter:

```text
Iran market · Toman per unit · indicative quotes
US dollar cash: ~264,000 Toman (median of 2 sources; spread 0.2%)
  TGJU via Gheymat: 263,800
  BONBAST via Gheymat: 264,200
  Relay fetched YYYY-MM-DD HH:MM UTC; underlying source trade times unknown.
Relayed ranges are provider quote fields with unverified buy/sell direction.
Cash FX is distinct from USDT. Quotes are informational, not executable offers.
```

## Design and product assessment

The bot currently looks like a functional Telegram command interface. Search menus and downloader confirmations have useful buttons and clear progress. Community and Utilities responses are mostly unstyled plain text; `/help` is one dense block, long market responses end in repeated technical caveats, and successful file tools often send an attachment without a caption. Personality is only visible in `/help`, so the promised calm, selectively affectionate character is barely perceptible. `/modlog` still says `Mika`, and the Transcript button promises a future capability. English interface text complies with the product decision; Persian trigger strings and imported cultural text are intentional exceptions.

No visual redesign or new capability was added as part of this audit. Proposed presentation work for owner approval: concise command-specific help; a shared event wording contract across Community and Utilities; consistent headings, compact market cards with an optional detail button; replacing the inactive Transcript button with an honest unavailable state; removing the legacy `Mika` wording. Such changes should keep numeric authorization, exact triggers, and the existing downloader consent flow intact.
