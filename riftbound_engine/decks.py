from __future__ import annotations

from .csv_data import (
    build_csv_deck_profile,
    deck_battlefields_for_key,
)


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


HARDCODED_DECKS: dict[str, dict[str, object]] = {
    "ember_vanguard": _build_hardcoded_deck(
        "ember_vanguard",
        domains=frozenset({"Fury"}),
        rune_counts=[("Fury", 12)],
        seed=42_001,
    ),
    "tide_wardens": _build_hardcoded_deck(
        "tide_wardens",
        domains=frozenset({"Fury", "Body"}),
        rune_counts=[("Fury", 6), ("Body", 6)],
        seed=42_002,
    ),
}
