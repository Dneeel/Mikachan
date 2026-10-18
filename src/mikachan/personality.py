"""Shared, numeric-ID-only event wording contract."""

from enum import StrEnum


class Mode(StrEnum):
    NORMAL = "normal"
    MOMMY = "mommy"


def mode_for(user_id: int, authorized: set[int], mommy_ids: set[int]) -> Mode:
    return Mode.MOMMY if user_id in authorized and user_id in mommy_ids else Mode.NORMAL


def response(event: str, mode: Mode) -> str:
    templates = {
        "ready": ("Ready when you are.", "I'm here, darling. Tell me what you need."),
        "unavailable": ("That source is unavailable right now.",
                        "That source is unavailable right now, darling."),
    }
    normal, mommy = templates[event]
    return mommy if mode is Mode.MOMMY else normal
