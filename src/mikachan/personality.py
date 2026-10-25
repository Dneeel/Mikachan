"""Shared, numeric-ID-only event wording contract."""

from enum import StrEnum


class Mode(StrEnum):
    NORMAL = "normal"
    MOMMY = "mommy"


def mode_for(user_id: int, authorized: set[int], mommy_ids: set[int],
             owner_ids: set[int] | None = None) -> Mode:
    """Use authenticated numeric IDs; active Owners receive Mommy by default."""
    return (Mode.MOMMY if user_id in authorized and
            user_id in (mommy_ids | (owner_ids or set())) else Mode.NORMAL)


def response(event: str, mode: Mode) -> str:
    templates = {
        "ready": ("Ready when you are.", "I'm here, darling. Tell me what you need."),
        "called": ("I'm here. What do you need?",
                   "I'm here, darling. What do you need?"),
        "unavailable": ("That source is unavailable right now.",
                        "That source is unavailable right now, darling."),
    }
    normal, mommy = templates[event]
    return mommy if mode is Mode.MOMMY else normal
