from __future__ import annotations

from .deck_files import list_deck_ids, load_deck_file
from .engine import RequiredTo


def _fake_fill_deck_ids() -> tuple[str, str]:
    ids = list_deck_ids()
    if len(ids) < 2:
        raise RuntimeError("FAKE_FILL needs at least two decks in riftbound-engine/decks/*.txt")
    return ids[0], ids[1]


def _default_battlefield(deck_key: str) -> str:
    parsed = load_deck_file(deck_key)
    if parsed.battlefields:
        return parsed.battlefields[0]
    return "Altar to Unity"


def _fake_fill_choices() -> dict[RequiredTo, str]:
    p1, p2 = _fake_fill_deck_ids()
    return {RequiredTo.PLAYER_1: p1, RequiredTo.PLAYER_2: p2}


def _fake_fill_battlefields() -> dict[RequiredTo, str]:
    choices = _fake_fill_choices()
    return {
        RequiredTo.PLAYER_1: _default_battlefield(choices[RequiredTo.PLAYER_1]),
        RequiredTo.PLAYER_2: _default_battlefield(choices[RequiredTo.PLAYER_2]),
    }


# Preset choices to speed up local iteration when FAKE_FILL is enabled.
FAKE_FILL_CHOICES: dict[RequiredTo, str] = _fake_fill_choices()
FAKE_FILL_BATTLEFIELDS: dict[RequiredTo, str] = _fake_fill_battlefields()

FAKE_FILL_FIRST_TURN: RequiredTo = RequiredTo.PLAYER_1

# Mulligan: comma-separated hand indices (0–3) to put on bottom, max 2; empty string = none.
FAKE_FILL_MULLIGAN_BOTTOM: str = ""
