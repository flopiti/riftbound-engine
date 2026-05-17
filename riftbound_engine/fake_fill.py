from __future__ import annotations

from .csv_data import deck_battlefields_for_key
from .engine import RequiredTo

# Preset choices to speed up local iteration when FAKE_FILL is enabled.
FAKE_FILL_CHOICES: dict[RequiredTo, str] = {
    RequiredTo.PLAYER_1: "ember_vanguard",
    RequiredTo.PLAYER_2: "tide_wardens",
}


def _default_battlefield(deck_key: str) -> str:
    battlefields = deck_battlefields_for_key(deck_key)
    if battlefields:
        return str(battlefields[0])
    return "Altar to Unity"


FAKE_FILL_BATTLEFIELDS: dict[RequiredTo, str] = {
    RequiredTo.PLAYER_1: _default_battlefield("ember_vanguard"),
    RequiredTo.PLAYER_2: _default_battlefield("tide_wardens"),
}

FAKE_FILL_FIRST_TURN: RequiredTo = RequiredTo.PLAYER_1

# Mulligan: comma-separated hand indices (0–3) to put on bottom, max 2; empty string = none.
FAKE_FILL_MULLIGAN_BOTTOM: str = ""
