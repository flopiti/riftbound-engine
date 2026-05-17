from __future__ import annotations

from .csv_data import (
    build_csv_deck_profile,
    deck_battlefields_for_key,
)

DECK_SPECS: dict[str, dict[str, object]] = {
    "ember_vanguard": {
        "domains": frozenset({"Fury"}),
        "rune_counts": [("Fury", 12)],
        "seed": 42_001,
    },
    "tide_wardens": {
        "domains": frozenset({"Fury", "Body"}),
        "rune_counts": [("Fury", 6), ("Body", 6)],
        "seed": 42_002,
    },
}


def _build_hardcoded_deck(
    key: str,
    *,
    domains: frozenset[str],
    rune_counts: list[tuple[str, int]],
    seed: int,
) -> dict[str, object]:
    profile = build_csv_deck_profile(
        key,
        domains=domains,
        rune_counts=rune_counts,
        seed=seed,
    )
    return {
        "valid": True,
        "battlefields": list(deck_battlefields_for_key(key)),
        "chosen_champion": profile.chosen_champion,
        "legend": profile.legend,
        "cards": list(profile.cards),
        "runes": profile.runes,
    }


def deck_data_for_id(deck_id: str) -> dict[str, object]:
    """Build deck contents from CSV on each call (always matches current card DB)."""
    if deck_id not in DECK_SPECS:
        raise ValueError(f"unknown deck id '{deck_id}'")
    spec = DECK_SPECS[deck_id]
    return _build_hardcoded_deck(
        deck_id,
        domains=spec["domains"],  # type: ignore[arg-type]
        rune_counts=spec["rune_counts"],  # type: ignore[arg-type]
        seed=spec["seed"],  # type: ignore[arg-type]
    )


# Preset ids exposed to clients (contents are built lazily via `deck_data_for_id`).
HARDCODED_DECKS: dict[str, dict[str, object]] = {deck_id: {"valid": True} for deck_id in DECK_SPECS}
