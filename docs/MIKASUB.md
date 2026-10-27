# MikaSub on Mikachan `v2`

## Direct source mode

When no MikaSub Worker URL is configured, Owner source controls and `/vpn` use
Mikachan's existing SQLite database. **Update Now** downloads each enabled
subscription document, parses supported share links, and atomically publishes
the successful snapshot. The previous snapshot remains available if refresh
fails. The bot serves the node list and exports in Telegram; no Cloudflare
Worker or public subscription URL is needed for these actions. Source servers
are not pinged or probed. A Worker remains an optional publication backend for
owners who need a stable external subscription URL. If Worker settings already
exist, the Owner can choose **Publication → Use Telegram sources**. Mikachan
copies the accessible source definitions into SQLite, leaves the Worker and
its old public URL intact, then switches the bot to local mode. **Update Now**
builds the new local snapshot. The historical Worker
state below describes that optional backend, not a requirement for `/vpn`.

## Current Cloudflare state (2026-10-24)

The existing Worker, KV and D1 contain one enabled source:
`https://raw.githubusercontent.com/0xRadikal/Free-v2ray-Configs/main/top100.txt`.
Its document contained 100 distinct exportable configurations. Snapshot
`31b4df5caed33bd80acc1a6e` was written and read back from the existing KV;
D1 points to it with a total of 100. No proxy endpoint was tested. The
existing personal token hash was preserved, so the already issued link should
continue to address this snapshot. The raw personal URL cannot be reconstructed
from its hash; it must come from the owner's existing saved URL or be rotated
deliberately through the authenticated Owner controls. The bot's VPS config
and an actual subscription request were not accessible from this workspace.

MikaSub publishes an **untested** collection of share configs. It never pings,
handshakes with, tunnels through, or measures the listed proxy servers. `/proxy`
is the pre-existing, independent @Dnproxbot userbot feature. `/vpn` is the new
MikaSub launcher and needs no Telegram user session.

## Existing resources and ownership

Cloudflare account discovery on 2026-10-22 identified the existing Worker
`mikachan-vpn-radar`, binding `VPN_KV` to `mikachan-vpn-snapshots`, and binding
`VPN_DB` to `mikachan-vpn-db`. The Worker had only the Cloudflare starter
response and the D1 database had no product tables. This directory targets
those actual resources. It does not create a second Worker, KV namespace, D1
database, or paid service. `wrangler.jsonc` pins the discovered KV and D1 IDs.

Cloudflare holds source URLs and metadata in D1, versioned snapshots in KV,
and a SHA-256 hash of the public link token in D1. A separate Worker secret
`MIKASUB_ADMIN_TOKEN` authenticates the bot. The bot's protected configuration
holds the Worker URL and matching Admin token. The full public subscription
link is saved in Mikachan's existing SQLite database after rotation, or may
be imported once through the masked `MIKASUB_PUBLIC_URL` setting when the link
was created by an authenticated deployment request before the bot was configured.

The existing bot performs bounded source-document fetch/parse and builds the
precomputed publication payload. This is the explicitly allowed bot-side
fallback for Workers Free's small CPU allowance: a Worker HTTP invocation
does not fetch and parse thousands of nodes. The Worker accepts versioned
100-item chunks, verifies chunk count and both completed publication payloads,
then advances the D1 pointer. The previous version is retained if a new KV
value has not propagated. A failed source or build leaves the current snapshot
untouched. Source URLs are never returned by public routes.

The current and previous snapshots stay in KV without a TTL, so a prolonged
source outage cannot silently erase the published subscription. Older
published versions are eligible for deletion after 48 hours and abandoned
staging versions after one hour. The bot invokes a bounded maintenance API
with at most 10 KV deletions per request. If the bot is offline, cleanup waits
until it returns; the current publication remains readable.

The bot's in-process scheduled attempt is every six hours. Owner **Update Now**
starts a bounded asynchronous rebuild. Owner **Fetch Now** fetches one source
document and updates its counts but does not contact any proxy inside it.
There is no new polling process or systemd unit.

## Bot controls

- `/vpn` has **MikaSub**, **Sub Converter**, and **Thanks**. Approved groups get
  results in that group. Default-deny numeric user and group rules still apply.
- Tapping **MikaSub** shows the actual snapshot count and publication time and
  immediately sends the first five configurations from that published snapshot.
  If no snapshot exists, it explains how the Owner adds a source and publishes
  it in private `/settings` → MikaSub → Sources → Update Now. Source changes do
  not silently replace a published snapshot before a successful build.
  **Just One**
  selects a uniform index from the pinned snapshot, returns one complete
  renamed URI, and supplies single-config QR. **The Whole Stash** offers Here,
  TXT, YAML, MikaSub link, Back and Thanks. Here sends five complete URIs per
  batch, or a five-entry TXT file if the Telegram message limit would cut any
  URI. Old batch buttons stay visible; repeated or out-of-order 5 More taps
  cannot skip a batch. QR chooses one of the five entries. No country button
  is shown because endpoint location has not been credibly classified.
- The **YAML** button intentionally sends `MikaSub.html`, an offline HTML
  viewer, **not** Clash YAML. It includes all configs, Copy, Copy All with
  manual selection fallback, protocol search/filter, and QR generated on
  demand. It has no remote scripts, styles, images or fonts. The QR library is
  Kazuhiko Arase's MIT-licensed `qrcode-generator` 1.4.4, bundled once in
  `src/mikachan/assets/qrcode.js` with its upstream attribution header.
- **Sub Converter** asks for one HTTP(S) subscription URL as a reply in the
  same authorized chat. It uses a short-lived user/chat/message-bound session,
  validates DNS and redirect destinations, fetches at most 4 MB, and exposes
  only Here, YAML (offline HTML), TXT, Thanks and navigation. Its source URL
  is not added to D1 or the MikaSub publication. In groups the bot tries to
  delete the URL message; other members may have seen it first. Converted
  results exist only in bot memory until the session expires or is replaced.
- Private Owner Settings includes a **MikaSub** section. Sources can be added,
  renamed, replaced, enabled/disabled, fetched, and removed; removal and
  public-link rotation require explicit confirmation. Add and Replace fetch
  and validate the document before changing D1. Statistics distinguish raw,
  parsed, duplicates, unsupported, malformed and exported records. Source
  URLs and Cloudflare secrets are never shown in source lists or errors.

## Parser and export boundary

Raw share URI lists and Base64/URL-safe Base64 lists are accepted. `vmess://`
Base64 JSON changes only `ps`; `ssr://` changes only `remarks`; the following
URI schemes change only the percent-encoded display fragment: VLESS, Trojan,
Shadowsocks, Hysteria/Hysteria2, TUIC, SOCKS, HTTP(S), Naive, WireGuard,
Snell and AnyTLS. Authority, path, query and options are left byte-for-byte
unchanged in fragment-based schemes. Unknown schemes and malformed records
are counted and excluded because their alias semantics are not established.

For Clash YAML and sing-box JSON, only simple Shadowsocks and Trojan records
whose full supported option set can be mapped to a share URI are exported.
Structured VLESS, VMess, SSR, TUIC, Hysteria and records with plugin,
transport, reality, TLS verification or other unmapped options are **counted
unsupported**. This prevents a plausible-looking but broken conversion.
Mixed-protocol Base64/TXT is not universally importable by every client.
Actual Clash YAML export is not implemented; the YAML button is explicitly an
HTML viewer per product decision. Source-owned proxy labels are not used to
infer the physical exit country.

Deduplication hashes connection semantics without the presentation name.
Distinct query/transport/SNI/credential strings remain distinct. D1 assigns
stable numeric IDs to hashes on first sight; public labels are exactly
`🏴‍☠️ Maybe It Works 0001`, `0002`, and so on, with numbers above 9999
unwrapped. Removed nodes leave gaps. Owner-provided Sub Converter labels are
preserved, rather than receiving MikaSub names.

## Cloudflare Free budget

The build caps are 50 sources, 5,000 distinct configs, 4 MB per input source,
8 MB combined input and published raw output, 11 MB Base64 output, and three
concurrent source fetches. Each Worker item upload has at most 100 records.
A 4,000-node successful build writes about 40 KV item chunks plus raw,
Base64 and metadata: about **43 KV writes**, or **172/day** at four six-hour
builds, below the stated ~1,000 Free writes/day. Failed attempts can add
writes, so Owner Update Now is intentionally controlled; no 30-minute
automatic refresh is scheduled. D1 node rows are written only for new
canonical hashes. The first 4,000-node build may write around 4,000 D1 rows;
ordinary unchanged refreshes write no node rows. These are estimates, not
measured production quotas or a guarantee of 10 ms CPU compliance. No
node-level external subrequests, GeoIP HTTP lookups, or proxy tests occur.

The Worker returns precomputed Base64 URI lines at `/sub/<opaque-token>` and
plain UTF-8 lines at `?format=raw`. The link is a bearer credential: anyone
holding it can read configs until Owner rotates it. Rotation revokes the old
token; no redirect is used. If no snapshot has been published, the route
returns a truthful 503.

## Deployment and rollback

Use the existing `v2` code. Back up the VPS SQLite database and
`/opt/Mikachan/config/bot.env` through the Mikachan manager before activating
new bot code. Do not start a second bot poller.

1. In Cloudflare, verify the Worker `mikachan-vpn-radar` has bindings named
   exactly `VPN_KV` and `VPN_DB` to the existing namespace/database. Review
   `cloudflare/mikasub/wrangler.jsonc` against the account's current IDs.
2. In `cloudflare/mikasub`, run `npm ci`, then
   `npx wrangler d1 migrations apply VPN_DB --remote` and
   `npx wrangler deploy`. Use a scoped Cloudflare API token in the environment;
   never save it in the repository. The migration is additive.
3. Generate a **new independent random Admin token** (at least 32 characters)
   and set it with `npx wrangler secret put MIKASUB_ADMIN_TOKEN`. The token
   must differ from the Cloudflare deployment API token and the public URL
   token. Save it in your password manager.
4. In the Mikachan terminal panel, open Configuration → MikaSub and set
   `MIKASUB_WORKER_URL` to the actual HTTPS Worker origin and
   `MIKASUB_ADMIN_TOKEN` to the matching Worker secret. If a link was already
   created using the authenticated Worker API, set the optional masked
   `MIKASUB_PUBLIC_URL` to that exact link. The panel masks both credentials.
   Restart through the managed service. No Telegram API ID/hash or
   userbot session is needed for MikaSub.
5. If no public URL was imported, open private `/settings` → MikaSub →
   Publication → Rotate Link once to create it. Then add owner sources and
   select Update Now. Confirm the count, use `/vpn`, test `?format=raw`, TXT,
   HTML offline, QR, group authorization, and old-link revocation. The initial
   source URLs are supplied by the Owner via Telegram after deployment.

The protected Worker contract is `GET /api/v1/sources`, POST/PATCH/DELETE
source actions, `POST /api/v1/node-ids`, `GET /api/v1/publication`,
`GET /api/v1/snapshot/metadata`, `GET /api/v1/snapshot/items`, the bounded
maintenance endpoints, and staged
`POST /api/v1/publish/...`. All require the Admin bearer header. There is no
public CORS grant or query-string Admin credential. `GET /healthz` returns
only redacted readiness.

For rollback, stop the bot, restore the previous managed release and its
matching SQLite backup if schema compatibility requires it, then restart.
The Worker migration adds tables without deleting existing D1 data; an older
Worker ignores them. Revert to a previous Worker deployment through Cloudflare
if its behavior is faulty. Do not delete the D1 tables, KV versions, or public
token during code rollback. Cleanup protects current and previous versions
and waits 48 hours before pruning older published versions, covering active
30-minute Here sessions. Public link rotation is intentionally irreversible
for holders of the old link.

## Verification and limits

The test suite uses fixtures and a local Wrangler D1/KV instance. Local
tests cannot prove proxy reachability, importer support in each VPN client,
Cloudflare cross-region KV propagation speed, the real VPS bot's Telegram
delivery, or Cloudflare Free CPU usage. A live release should check these
operational boundaries without probing individual proxy servers. The public
source URL and the Cloudflare token supplied for deployment must be rotated
if either is disclosed to unintended recipients.
