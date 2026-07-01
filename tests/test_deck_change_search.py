"""Tests for the search's deck-changing capability (``allow_deck_changes``).

A goal naming a card the loaded decks can't reach is unreachable by default
(the search exhausts at distance 5.0). With deck changes allowed, the search
brings the card into play by sideboarding it from the current deck or switching
to a deck that lists it, then finds a real line. "Disarming Rake" is the fixture
card: it's in ``yasuo_unforgiven``'s main deck and in ``irelia_nates``'s
sideboard, so both paths are exercisable.
"""

from __future__ import annotations

import unittest

from riftbound_engine.engine import GameEngine, GameState, RequiredTo, Rune, build_deck_from_id
from riftbound_engine.search import apply_deck_changes, bfs_search

PRED = {"units": {"name": "Disarming Rake", "at": "hand"}}


def _root(p1_id: str, p2_id: str) -> GameState:
    """A clean, playable turn-1 root for the two decks (ABCD done, P1 active),
    with rune libraries so ABCD's channel step works when ending turns."""
    d1 = build_deck_from_id(p1_id)
    d2 = build_deck_from_id(p2_id)
    gs = GameState()
    gs.started = True
    gs.is_mulligan_done = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    gs.current_player = RequiredTo.PLAYER_1
    gs.player_1_deck = d1
    gs.player_2_deck = d2
    gs.player_1_deck_id = p1_id
    gs.player_2_deck_id = p2_id
    gs.battlefield_1 = "A"
    gs.battlefield_2 = "B"
    gs.player_1_hand = list(d1.cards[:4])
    gs.player_2_hand = list(d2.cards[:4])
    gs.player_1_library = list(d1.cards[4:])
    gs.player_2_library = list(d2.cards[4:])
    gs.player_1_rune_library = [Rune(domain="Demacia") for _ in range(12)]
    gs.player_2_rune_library = [Rune(domain="Bilgewater") for _ in range(12)]
    return GameEngine(game_state=gs).game_state


class DeckChangeSearchTests(unittest.TestCase):
    def test_unreachable_without_deck_changes(self) -> None:
        # Sideboard-only card in P1's deck → not in library → not found.
        res = bfs_search(
            _root("irelia_nates", "ezreal_prodigal_explorer"),
            PRED,
            max_depth=20,
            allow_deck_changes=False,
        )
        self.assertFalse(res.found)
        self.assertEqual(res.deck_changes, [])

    def test_sideboard_swap_makes_it_reachable(self) -> None:
        res = bfs_search(
            _root("irelia_nates", "ezreal_prodigal_explorer"),
            PRED,
            max_depth=20,
            allow_deck_changes=True,
        )
        self.assertTrue(res.found)
        self.assertEqual(len(res.deck_changes), 1)
        self.assertIn("sideboarded in Disarming Rake", res.deck_changes[0])
        self.assertTrue(res.deck_changes[0].startswith("player_1"))

    def test_deck_switch_when_no_loaded_deck_has_it(self) -> None:
        # Neither deck lists Disarming Rake anywhere → switch P1 to one that does.
        res = bfs_search(
            _root("ezreal_prodigal_explorer", "garen_might_of_demacia"),
            PRED,
            max_depth=20,
            allow_deck_changes=True,
        )
        self.assertTrue(res.found)
        self.assertEqual(len(res.deck_changes), 1)
        self.assertIn("switched deck to", res.deck_changes[0])

    def test_apply_deck_changes_puts_card_in_hand(self) -> None:
        start = _root("irelia_nates", "ezreal_prodigal_explorer")
        new_state, changes = apply_deck_changes(start, PRED)
        self.assertTrue(changes)
        # Card is now in P1's HAND (immediately usable) and the deck lists it.
        self.assertIn("Disarming Rake", new_state.player_1_hand)
        self.assertIn("Disarming Rake", new_state.player_1_deck.cards)
        self.assertEqual(len(new_state.player_1_deck.cards), len(start.player_1_deck.cards))
        # The original state is untouched (deepcopy semantics).
        self.assertNotIn("Disarming Rake", start.player_1_hand or [])

    def test_deck_change_satisfies_hand_goal_immediately(self) -> None:
        # With the card placed in hand, a "card in hand" goal is reached with
        # ZERO moves — robust regardless of the starting position's turn state.
        res = bfs_search(
            _root("irelia_nates", "ezreal_prodigal_explorer"),
            PRED,
            max_depth=20,
            allow_deck_changes=True,
        )
        self.assertTrue(res.found)
        self.assertEqual(res.depth, 0)
        self.assertEqual(res.moves, [])
        self.assertTrue(res.deck_changes)

    def test_no_change_when_already_reachable(self) -> None:
        start = _root("yasuo_unforgiven", "ezreal_prodigal_explorer")
        _new, changes = apply_deck_changes(start, PRED)
        # yasuo's main deck already has it → nothing to change.
        self.assertEqual(changes, [])


if __name__ == "__main__":
    unittest.main()
