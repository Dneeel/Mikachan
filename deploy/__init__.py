"""Standalone deployment and terminal-management tools.

An older bootstrap panel refreshes its known files on the first upgrade. Let
that panel load newly added, validated UI helpers from the active release until
the refreshed manager has copied them into the panel on the next activation.
"""

from pathlib import Path

_package = Path(__file__).resolve().parent
if _package.parent.name == "panel":
    _root = _package.parent.parent
    _active = _root / "current"
    try:
        _release = _active.resolve(strict=True)
        if (_release.is_relative_to(_root / "releases") and
                (_release / "deploy" / "terminal_assets.py").is_file() and
                (_release / "deploy" / "terminal_intro.py").is_file()):
            __path__.append(str(_release / "deploy"))
    except OSError:
        pass
