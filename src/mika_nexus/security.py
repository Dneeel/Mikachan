"""Authorization and conservative URL validation before and after consent."""

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


def allowed(user_id: int, chat_id: int, chat_type: str, ids: set[int], private_only: bool) -> bool:
    return user_id in ids and (not private_only or (chat_type == "private" and chat_id == user_id))


def clean_url(raw: str) -> str:
    url = raw.strip().rstrip(".,;!?)]}")
    if len(url) > 2048 or len(url) < 10:
        raise ValueError("invalid URL length")
    p = urlsplit(url)
    host = p.hostname
    if p.scheme.lower() not in {"https", "http"} or not host or p.username or p.password:
        raise ValueError("only public HTTP(S) URLs without credentials")
    if p.port not in (None, 80, 443):
        raise ValueError("URL port not allowed")
    host = host.rstrip(".").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("local host refused")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ValueError("invalid public host") from None
    else:
        if not ip.is_global:
            raise ValueError("nonpublic IP refused")
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


def validate_dns(url: str) -> None:
    """Reject hosts with any nonpublic answer. Recheck on every redirect."""
    p = urlsplit(clean_url(url))
    answers = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    if not answers or any(not ipaddress.ip_address(a[4][0]).is_global for a in answers):
        raise ValueError("DNS has a nonpublic address")


def links(text: str, limit: int) -> tuple[list[str], int]:
    found = URL_RE.findall(text)
    valid: list[str] = []
    for raw in found:
        try:
            valid.append(clean_url(raw))
        except ValueError:
            continue
    return valid[:limit], max(0, len(valid) - limit)


def source(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    mapping = {
        "youtube": ("youtube.com", "youtu.be", "music.youtube.com"),
        "tiktok": ("tiktok.com",),
        "instagram": ("instagram.com",),
        "twitter": ("twitter.com", "x.com", "t.co"),
        "reddit": ("reddit.com", "redd.it"),
        "pinterest": ("pinterest.com", "pin.it"),
        "facebook": ("facebook.com", "fb.watch"),
        "gdrive": ("drive.google.com", "docs.google.com"),
        "mega": ("mega.nz", "mega.io"),
        "soundcloud": ("soundcloud.com",),
        "spotify": ("spotify.com",),
        "apple": ("music.apple.com", "podcasts.apple.com"),
        "deezer": ("deezer.com",),
        "castbox": ("castbox.fm",),
    }
    for name, domains in mapping.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return name
    return "direct"


def collection(url: str) -> bool:
    p = urlsplit(url)
    path = p.path.lower()
    if p.hostname == "podcasts.apple.com":
        return True
    if path.rstrip("/").endswith((".xml", ".rss", "/feed", "/rss")):
        return True
    if p.hostname == "music.apple.com" and "i=" in p.query:
        return False
    provider = source(url)
    parts = [part for part in path.split("/") if part]
    if provider == "tiktok" and len(parts) == 1 and parts[0].startswith("@"):
        return True
    if (
        provider == "instagram"
        and len(parts) == 1
        and parts[0] not in {"explore", "accounts", "direct", "stories", "reels"}
    ):
        return True
    if provider == "twitter" and len(parts) == 1 and p.hostname != "t.co":
        return True
    if provider == "soundcloud" and len(parts) == 1:
        return True
    if provider == "pinterest" and len(parts) == 1 and p.hostname != "pin.it":
        return True
    return (
        any(x in path for x in ("/playlist", "/album", "/artist", "/user/", "/profile/", "/show/"))
        or "list=" in p.query
    )
