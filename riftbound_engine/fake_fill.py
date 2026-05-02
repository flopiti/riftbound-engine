from __future__ import annotations

from .engine import RequiredTo

# Preset choices to speed up local iteration when FAKE_FILL is enabled.
FAKE_FILL_CHOICES: dict[RequiredTo, str] = {
    RequiredTo.PLAYER_1: "ember_vanguard",
    RequiredTo.PLAYER_2: "tide_wardens",
}

FAKE_FILL_BATTLEFIELDS: dict[RequiredTo, str] = {
    RequiredTo.PLAYER_1: "Scorch Ridge",
    RequiredTo.PLAYER_2: "Moonwake Shore",
}

FAKE_FILL_FIRST_TURN: RequiredTo = RequiredTo.PLAYER_1

# Mulligan: comma-separated hand indices (0–3) to put on bottom, max 2; empty string = none.
FAKE_FILL_MULLIGAN_BOTTOM: str = ""

