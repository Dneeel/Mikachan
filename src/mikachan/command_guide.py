"""One source for the Telegram command menu and the in-chat directory."""

from __future__ import annotations

from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup


SECTIONS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "music": ("01 · MUSIC & DOWNLOADS", (
        ("music", "Search songs, albums & playlists"),
        ("pod", "Find podcasts and episodes"),
        ("lyric", "Find lyrics for a song"),
        ("book", "Search books in connected Telegram sources"),
        ("queue", "View and cancel downloads"),
    )),
    "social": ("02 · SOCIAL & DISCOVERY", (
        ("tw", "Explore an X profile"), ("in", "Explore an Instagram profile"),
        ("tt", "Explore a TikTok profile"), ("yt", "Explore a YouTube profile"),
        ("follow", "Monitor a public profile"),
        ("following", "Manage followed accounts"),
        ("explore", "Discover TikTok posts by topic"),
    )),
    "tools": ("03 · TOOLS & UTILITIES", (
        ("qr", "Create a QR code from text"), ("scan", "Decode a replied QR code"),
        ("ocr", "Extract text from a replied image"),
        ("st", "Turn a replied image into a sticker"),
        ("xst", "Turn a replied sticker into PNG"),
        ("edit", "Edit or convert replied media"),
        ("url", "Create a link from a replied file"),
        ("tr", "Translate text into another language"),
        ("pron", "Hear English word pronunciation"),
        ("asr", "Transcribe replied audio"),
        ("style", "Explore alternative writing styles"),
        ("weather", "Weather, forecast & air quality"),
        ("now", "Local time and calendars"),
        ("nasa", "NASA's astronomy picture"),
    )),
    "culture": ("04 · ART, NATURE & CULTURE", (
        ("fal", "Get a Hafez fortune"), ("poem", "Explore poetry and poets"),
        ("dice", "Roll a Telegram die"), ("pishi", "Discover cats"),
        ("earth", "Explore wildlife and nature"),
        ("art", "Discover artworks and museums"),
        ("xotd", "Explore today's discoveries"),
        ("quote", "Discover sourced quotations"),
        ("wiki", "Explore Wikipedia"),
    )),
    "network": ("05 · NETWORK & ACCESS", (
        ("proxy", "Find Telegram proxies"),
        ("vpn", "Open MikaSub and subscription tools"),
    )),
    "system": ("06 · PERSONAL & SYSTEM", (
        ("start", "Open the home screen"),
        ("help", "Browse the command directory"),
        ("settings", "Manage your preferences"),
        ("status", "Check bot and job status"),
    )),
    "ai": ("◈ MIKACHAN AI", (
        ("mika", "Ask Mika Serious anything"),
        ("tts", "Turn text into speech"),
        ("stt", "Transcribe voice and audio"),
    )),
    "group": ("👥 GROUP COMMANDS", (
        ("rules", "View the group's rules"),
        ("groupstatus", "View observed message statistics"),
        ("groupstats", "View Mikachan request statistics"),
        ("id", "View a replied member's identity card"),
    )),
    "admin": ("GROUP MANAGEMENT · AUTHORIZED MANAGERS", (
        ("groupsettings", "Configure group features"),
        ("welcome", "Manage welcome messages"),
        ("nickname", "Set a member's nickname"),
        ("notes", "Manage saved group notes"),
        ("setnote", "Save a note from text or a reply"),
        ("setrules", "Set rules from a reply"),
        ("delrules", "Remove the group's rules"),
        ("del", "Delete a replied message"),
        ("pin", "Pin a replied message"),
        ("mute", "Temporarily restrict a member"),
        ("unmute", "Restore a member's permissions"),
        ("ban", "Ban a member"),
        ("unban", "Remove a member's ban"),
        ("bans", "View recorded bans"),
        ("modlog", "View recent moderation actions"),
    )),
}

PUBLIC = ("music", "social", "tools", "culture", "network", "system")


def commands(*, relay_enabled: bool, tts_enabled: bool, stt_enabled: bool) -> list[BotCommand]:
    """Put daily actions first in BotFather, using the same directory labels."""
    order = ("start", "help", "music", "pod", "lyric", "book", "explore", "in",
             "tt", "yt", "tw", "follow", "following", "mika", "tts", "stt",
             "weather", "now", "qr", "scan", "ocr", "st", "xst", "edit", "url",
             "tr", "pron", "asr", "style", "proxy", "vpn",
             "pishi", "earth", "art", "xotd", "poem", "fal", "dice",
             "quote", "wiki", "nasa",
             "settings", "queue", "status")
    lookup = {name: description for _, entries in SECTIONS.values()
              for name, description in entries}
    enabled = set(lookup)
    if not relay_enabled:
        enabled.discard("mika")
    # Keep speech commands discoverable in BotFather. Their handlers explain
    # setup state and may use either the relay or a configured local job path.
    return [BotCommand(command=name, description=lookup[name]) for name in order if name in enabled]


def page(section: str, *, relay_enabled: bool, tts_enabled: bool = True,
         stt_enabled: bool = True, group: bool = False, admin: bool = False) -> str:
    visible = list(PUBLIC)
    if relay_enabled:
        visible.insert(2, "ai")
    if group:
        visible.append("group")
    if admin and group:
        visible.append("admin")
    if section == "home":
        return ("✦ <b>MIKACHAN</b>\nCOMMAND DIRECTORY · v2\n\n"
                "<i>Everything you need, one command at a time.</i>\n\n"
                + "\n".join("⌁ " + SECTIONS[key][0] for key in visible)
                + "\n\nChoose a section below. Send a supported link to begin.")
    if section == "meet":
        return ("🎭 <b>MEET MIKA</b>\n\nStart a message with <code>میکا</code> for Mika Chat.\n"
                "Use <code>/mika QUESTION</code> for Mika Serious.\n"
                "Reply to an AI answer to continue its conversation." if relay_enabled else
                "🎭 <b>MEET MIKA</b>\n\nMika needs the owner's n8n connection before chatting.")
    if section not in visible:
        section = "home"
        return page(section, relay_enabled=relay_enabled, tts_enabled=tts_enabled,
                    stt_enabled=stt_enabled, group=group, admin=admin)
    heading, entries = SECTIONS[section]
    items = [(name, description) for name, description in entries
             if not (name == "tts" and not tts_enabled or name == "stt" and not stt_enabled)]
    body = ("✦ <b>MIKACHAN</b>\n" + heading + "\n\n" +
            "\n".join(f"<code>/{name}</code> — {description}." for name, description in items))
    if section == "ai":
        body += "\n\nStart with <code>میکا</code> for Mika Chat. Reply to continue."
    if section == "tools":
        body += "\n\nQuick math: send an expression such as <code>12*7</code>."
    if section == "admin":
        body += "\n\n<i>Requires Mikachan Admin or Owner access.</i>"
    return body


def navigation(section: str, *, relay_enabled: bool, group: bool,
               admin: bool) -> InlineKeyboardMarkup:
    options = [("🎧 Music & Downloads", "music"),
               ("👀 Social & Discovery", "social"),
               ("🛠 Tools & Utilities", "tools"),
               ("🌿 Culture & Nature", "culture"),
               ("↗ Network & Access", "network")]
    options.append(("⚙️ Personal & System", "system"))
    if relay_enabled:
        options.insert(2, ("◈ Mika AI", "ai"))
    if group:
        options.append(("👥 Group Commands", "group"))
    if admin and group:
        options.append(("⚙️ Admin Tools", "admin"))
    rows = [[InlineKeyboardButton(text=label, callback_data=f"guide:{key}")]
            for label, key in options] if section == "home" else []
    if section != "home":
        rows.append([InlineKeyboardButton(text="◀ Back", callback_data="guide:home"),
                     InlineKeyboardButton(text="⌂ Home", callback_data="guide:start")])
    else:
        rows.append([InlineKeyboardButton(text="⌂ Home", callback_data="guide:start")])
    rows.append([InlineKeyboardButton(text="✖ Close", callback_data="guide:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


START_TEXT = ("✦ <b>MIKACHAN</b>\n<i>Your pocket universe.</i>\n\n"
              "A little bit of everything, all in one place.\n\n"
              "Discover music, download media, explore the internet, create things, "
              "and talk to Mika.\n\n"
              "⌁ <b>WHAT'S INSIDE</b>\n"
              "🎧 Music, podcasts & books\n"
              "↗ Social media & downloads\n"
              "◈ Smart tools & AI\n"
              "✿ Art, nature & discoveries\n"
              "◉ Profiles & live monitoring\n\n"
              "<b>Send a supported link to begin, or choose where to go.</b>")


def start_navigation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Command Guide", callback_data="guide:home"),
         InlineKeyboardButton(text="🎧 Find Music", callback_data="guide:musicfind")],
        [InlineKeyboardButton(text="🎭 Meet Mika", callback_data="guide:meet"),
         InlineKeyboardButton(text="⚙️ Settings", callback_data="guide:settings")],
        [InlineKeyboardButton(text="✖ Close", callback_data="guide:close")],
    ])
