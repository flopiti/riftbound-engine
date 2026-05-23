"""Deck registry — lists live in ``riftbound-engine/decks/*.txt``."""

from __future__ import annotations

from .deck_files import deck_data_for_id, deck_ids, list_deck_ids


def _deck_specs() -> dict[str, dict[str, object]]:
    return {deck_id: {"valid": True} for deck_id in list_deck_ids()}


class _DeckSpecsProxy(dict):
    def __getitem__(self, key: str) -> dict[str, object]:
        return _deck_specs()[key]

    def __contains__(self, key: object) -> bool:
        return key in _deck_specs()

    def keys(self):
        return _deck_specs().keys()

    def __iter__(self):
        return iter(_deck_specs())

    def __len__(self) -> int:
        return len(_deck_specs())


DECK_SPECS: dict[str, dict[str, object]] = _DeckSpecsProxy()  # type: ignore[assignment]
HARDCODED_DECKS = DECK_SPECS

__all__ = [
    "DECK_SPECS",
    "HARDCODED_DECKS",
    "deck_data_for_id",
    "deck_ids",
    "list_deck_ids",
]
