# Mikachan v2 response examples — 2026-10-18

These are rendered examples from current handlers and provider probes, not a claim that this commit was tested in Telegram. Values are illustrative and will change. The downloader Yes/No flow and `/twitter` command are unchanged.

| Input | Visible response or attachment |
| --- | --- |
| `/help` | Short sections headed **Media**, **Community**, **Utilities**, and **Toman quotes**, with examples and a note that file tools act on replies. Owner and group-admin lines appear in their relevant contexts. |
| `/settings` | `⚙️ Your settings` followed by `Search filters · change them in a result view`, `Public audio · MP3`, any saved Types/Sources, `Time zones · /time list`, `Queue · /queue`. |
| `/weather اصفهان` | `🌤 اصفهان, ایران`, `Now · 24.5 °C · Overcast`, rain/wind, a short **Next 3 days** list and a local model time. Open-Meteo supplies values. |
| `/weather tehran` | Chooses the large exact Tehran instead of listing ten duplicates and unrelated names. Ambiguity displays at most three distinct choices with a refinement hint. |
| `/rate USD EUR` | `💱 USD → EUR` / `1 USD = 0.88865 EUR` / `Source · frankfurter` / `As of · 2026-10-16`. A source outage tries Open ER API. |
| `/rate BTC USD` | The same compact layout with CoinGecko or CoinPaprika and its reported update time. It is a global spot reference, separate from Iranian cash or Toman quotes. |
| `دلار` or `/rate USD IRT` | `💱 Iran market · Toman per unit`, one cash-USD estimate/consensus line, compact source names, snapshot time and one indicative-price caveat. The underlying source comparison and validation remain intact. |
| `/apod` on the 2026-10-18 NASA page | A genuine 517,704-byte image preview attached as a Telegram photo; caption begins `🌌 NASA · APOD video preview` / `Sonified: Eagle Nebula Pillars · 2026-10-18`, followed by a short explanation and credit plus a **NASA story ↗** button. The full media is a video on NASA's page. |
| `/fal` / `فال` | One of nine source-attributed Hafez poems, headed `📜 Hafez`; the source link and Wikisource CC BY-SA attribution follow the original Persian text. |
| `/poem` / `شعر` | `📜 Poetry` and three buttons: **Random poem**, **Choose poet**, **Search poetry**. The bundled Hafez set gives these buttons real results. |
| `/joke`, `/quote` without an owner catalog | `No reviewed content is installed for this feature yet.` |

Unauthorized numeric IDs remain silent. Ordinary chat, `/start`, and unrelated media do not trigger replies. A new photo or QR command uses bounded memory, and downloader job files are removed after delivery or on the next startup after a crash.

The APOD JSON API currently sometimes returns HTTP 500 or a NASA logo as the apparent image. The current handler reads the official APOD page first, validates the actual preview, and uses the JSON API only as fallback. When neither source provides valid media, it sends a short, truthful error or link instead of a logo or a long text dump.
