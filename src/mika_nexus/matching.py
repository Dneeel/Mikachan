"""Conservative ranking of public recordings against a catalog track or text query."""

import re
import unicodedata

VERSION_WORDS = {
    "unofficial",
    "tribute",
    "fanmade",
    "slowed",
    "slow",
    "reverb",
    "remix",
    "live",
    "cover",
    "acoustic",
    "instrumental",
    "remaster",
    "remastered",
    "sped",
    "speed",
    "nightcore",
    "karaoke",
    "bootleg",
    "piano",
    "pianoforte",
    "edit",
    "extended",
    "lofi",
    "demo",
    "rework",
}

DISPLAY_WORDS = {"official", "audio", "video", "visualizer", "lyrics", "lyric", "hd", "hq"}


def tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return set(re.findall(r"[\w]+", normalized, flags=re.UNICODE))


def version(value: str) -> frozenset[str]:
    words = tokens(value)
    result = words & VERSION_WORDS
    if "remaster" in result or "remastered" in result:
        result = (result - {"remaster", "remastered"}) | {"remaster"}
    if "slow" in result:
        result = (result - {"slow"}) | {"slowed"}
    return frozenset(result)


def match_score(
    title: str,
    creator: str,
    candidate_title: str,
    candidate_creator: str,
    duration: int | None = None,
    candidate_duration: int | None = None,
) -> float:
    """Return 0..1; incompatible variants never qualify as a confident match."""
    wanted = tokens(title) - VERSION_WORDS - DISPLAY_WORDS
    found = tokens(candidate_title) - VERSION_WORDS - DISPLAY_WORDS
    if not wanted or not found:
        return 0.0
    if duration and candidate_duration and abs(duration - candidate_duration) > 20:
        return 0.0
    artist = tokens(creator) - {"and", "the", "a", "of", "feat", "ft"}
    candidate = tokens(candidate_creator) | found
    artist_overlap = len(artist & candidate) / len(artist) if artist else 0.0
    if artist and artist_overlap < 0.5:
        return 0.0
    overlap = len(wanted & found) / len(wanted)
    if overlap < 0.7:
        return 0.0
    extra = found - wanted - artist
    precision = len(wanted & found) / (len(wanted & found) + len(extra))
    score = 0.66 * (0.65 * overlap + 0.35 * precision)
    if artist:
        score += 0.20 * artist_overlap
    else:
        score += 0.10
    if version(title) == version(candidate_title):
        score += 0.14
    else:
        score -= 0.28
    if duration and candidate_duration:
        delta = abs(duration - candidate_duration)
        score += 0.10 if delta <= 5 else (-0.15 if delta > 20 else 0.0)
    return min(1.0, max(0.0, score))
