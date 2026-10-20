# Follow and large media investigation · 2026-10-20

## What the current implementation actually does

`src/mikachan/follow.py` keeps per-category baselines, health and source errors in
SQLite, polls up to 25 follows roughly every 15 minutes, and checks a creator's
categories concurrently (three slots). It does **not** get an authenticated
timeline from Telegram's userbot. Instagram and X use a mixture of gallery-dl
and public web endpoints; TikTok uses public endpoints and extractor fallbacks;
YouTube has an Atom feed and extractor fallbacks. A working direct download URL
does not mean a timeline can discover the next unknown post. An empty response
after a previously populated timeline is treated as a failure, not deletion.

The read-only probe from this environment on 2026-10-20 returned profile name,
bio and avatar for X `Dniuis` but its tweet listing was unavailable. Instagram
`mehrdad.balagar` returned name and avatar but post, Reel and Story listings
were unavailable. These results are specific to this environment and time;
repeat on the actual VPS. They confirm that more frequent polling alone cannot
repair provider access. They do not establish absence of new posts.

The strongest practical Follow design is a **source contract per category**:

1. Each adapter must return the exact creator identity, stable item IDs, event
   type, ordering/publication time when available, source timestamp and an
   explicit result state: verified items, verified empty, stale, challenged,
   unauthorized, rate-limited or unavailable. A profile screenshot or
   on-demand media result cannot substitute for a timeline cursor.
2. Store cursor, last verified successful check, provider identity and health
   separately for each account **and** category. Advance a cursor only after
   a complete verified listing. On a source failure, keep the old baseline.
3. Poll new and previously healthy sources with per-provider budgets, jitter,
   `Retry-After`, exponential backoff and a circuit breaker. Preserve manual
   Check now, but cap concurrent calls so it cannot consume all provider quota.
   Summarize repeated outages instead of sending an alert every interval.
4. Deduplicate by provider item ID, not a temporary CDN URL; resolve
   attachments only after detecting a new ID. Check creator identity again
   before delivery. Keep a small overlap window to catch out-of-order posts.
5. Run canary accounts per platform/category and compare expected IDs with
   retrieved IDs. Record detection delay, source failures and false positives.
   A feature is marked healthy only for the categories the canary actually
   verifies. Distinguish source status from delivery status.
6. Prefer authorized platform APIs for categories they expose. YouTube channel
   uploads can use its public feed; official Data API gives richer metadata
   but requires a key/quota. X user posts require supported X API access or an
   authorized account; anonymous mirrors are brittle. Instagram Business
   Discovery applies to eligible professional accounts and does not grant
   general access to arbitrary Stories/Live. TikTok Display API is tied to an
   authorized TikTok user, so it does not provide unrestricted monitoring of
   arbitrary handles. Official access still needs account and scope tests.

This architecture can make available categories reliable and make failures
clear. It cannot guarantee 100% coverage of arbitrary Instagram, X or TikTok
accounts from unauthenticated public endpoints. Do not classify a category as
monitored solely because a profile page renders. No broader Follow adapter
was enabled in this change because the probes above supply no trustworthy
new timeline source.

## Large video path and limits

`src/mikachan/jobs.py` already uses Telethon `send_file` to a private staging
chat followed by Bot API `copyMessage`, then deletes the staged message. It
requires `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, a valid `USERBOT_SESSION` (or
legacy `COINPJ_SESSION`), a negative `USERBOT_STAGING_CHAT_ID`, and both the
user account and bot in that private group. API ID/hash alone cannot upload.
The application currently caps a single userbot upload at 1.9 GB. The public
Bot API direct upload limit is 50 MB; the code switches above 48 MiB. Telegram
may reject a particular copy even when upload succeeds. A local Bot API server
is another transport option, but this revision does not change that mode.

The existing code always deletes per-job downloaded files in `_run`'s `finally`
block. Telegram file IDs are cached for fast repeat delivery; those are server
references, **not** a retained media file under Mikachan's job directory.
After this change, configured userbot transport is preferred for YouTube
Up to 1080 and Pornhub video even when their files are smaller than 48 MiB.
Other media keeps the existing size-based route. If userbot staging is not
configured, the existing public/local Bot API route applies, with its smaller
limits. A quality ceiling selects the best available resolution **at or below**
the choice; it does not invent a 720p source when only 480p exists.

Pornhub now offers 480p, 720p and Up to 1080p after Yes. Its format selector
prefers progressive HTTPS MP4 before HLS under the chosen ceiling. This avoids
some HLS playlist HTTP 410 cases but cannot fix a watch page, media CDN, geo or
account denial. The supplied `viewkey=6ac5dd5aca7cb` exposed progressive
MP4 metadata at 240/480/720/1080 in this environment. This was **not** a full
download and was **not** a Telegram delivery test. Source metadata may change
between probe and download.

## Safe operator scripts

Run from the installed release. These commands do not start another Telegram
poller, download media bytes, or change SQLite:

```bash
cd /opt/Mikachan/current
sudo ./.venv/bin/python scripts/mikachan_probe.py readiness
sudo ./.venv/bin/python scripts/mikachan_probe.py follow-db
sudo ./.venv/bin/python scripts/mikachan_probe.py follow instagram mehrdad.balagar
sudo ./.venv/bin/python scripts/mikachan_probe.py follow twitter Dniuis
sudo ./.venv/bin/python scripts/mikachan_probe.py follow youtube ishowspeed
sudo ./.venv/bin/python scripts/mikachan_probe.py follow tiktok bnetanyahu
sudo ./.venv/bin/python scripts/mikachan_probe.py media 'https://www.pornhub.com/view_video.php?viewkey=6ac5dd5aca7cb'
sudo ./.venv/bin/python scripts/mikachan_probe.py media 'https://youtu.be/CkfAbU-rts0'
```

`readiness` reports **presence** of required userbot settings, not successful
login or copy. `follow` exercises the production provider code and reports
counts, elapsed time and safe error categories. An empty listing is labeled
`empty_unverified`. `follow-db` aggregates current error counts without
printing owners, chats or source error text. `media` asks yt-dlp for metadata
only; it does not prove that the selected CDN bytes are downloadable. The
script reads only configured provider cookie *paths* from the protected bot
environment; it never prints cookie contents, sessions, bot tokens or signed
media URLs. A private cookie file can also be supplied with `--cookies` for
the media probe.

For an actual end-to-end transport check, the owner can explicitly run:

```bash
cd /opt/Mikachan/current
sudo ./.venv/bin/python scripts/mikachan_probe.py transport --live
```

This creates one tiny text file inside `/opt/Mikachan/tmp/manager`, uploads it
through the configured user account to the private staging group, copies it to
the configured owner with the bot, and attempts to delete the copied message.
The bridge attempts staged-message deletion as well. It reports whether the
destination deletion succeeded and erases the local test file. It does not
start polling or touch the Follow database. A successful small-file copy
verifies credentials and chat access, **not** throughput or 1.9 GB acceptance.

Repeat the same probes on the VPS and another network to separate source
outage from VPS IP/region blocking. For a large-file live test, use a short,
permitted source first and inspect `journalctl -u mikachan.service`; the
read-only probe deliberately cannot validate Telegram upload, group access or
`copyMessage`. Do not post unredacted service logs or session strings.

## Primary references

- [Telegram Bot API file limits and copyMessage](https://core.telegram.org/bots/api)
- [Telegram file upload protocol](https://core.telegram.org/api/files)
- [YouTube playlist items](https://developers.google.com/youtube/data/reference/rest/v3/playlistItems/list)
- [YouTube search](https://developers.google.com/youtube/data/reference/rest/v3/search/list)
- [X user posts API](https://docs.x.com/x-api/users/get-posts)
- [Instagram Business Discovery](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/business-discovery)
- [yt-dlp Pornhub HTTP 410 report](https://github.com/yt-dlp/yt-dlp/issues/17642)
- [Prior project provider investigation](FOLLOW_PROVIDER_RESEARCH_2026-10-20.md)
