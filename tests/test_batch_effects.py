"""Newly-implemented effect codes from the tagging batch:
  * COUNTER_SPELL_RETURN_HAND         — Abandon
  * RETURN_ME_TO_HAND                 — Blitzcrank, Impassive
  * MOVE_ANY_FRIENDLY_UNITS_BF_TO_BASE — Emperor's Divide
"""

import unittest
from unittest import mock

from riftbound_engine.abilities import Ability
from riftbound_engine.engine import GameEngine, GameState, PlayedUnit, RequiredTo


class CounterSpellReturnHandTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch(
            "riftbound_engine.abilities.triggered_abilities_for",
            side_effect=lambda n: (Ability(active_effects=("COUNTER_SPELL_RETURN_HAND",)),) if n == "Abandon" else (),
        )
        p.start(); self.addCleanup(p.stop)

    def test_countered_spell_returns_to_owner_hand(self):
        eng = GameEngine(game_state=GameState())
        eng._game_state.player_2_hand = []
        eng._push_spell_to_chain(RequiredTo.PLAYER_2, "Gust", [[]])
        cid = eng._game_state.pending_chain.items[0].cid
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Abandon", [[f"spell_cid:{cid}"]])
        eng._resolve_chain()  # Abandon resolves → counters Gust to hand
        gs = eng._game_state
        items = gs.pending_chain.items if gs.pending_chain else []
        self.assertTrue(all(it.card != "Gust" for it in items))
        self.assertIn("Gust", gs.player_2_hand)          # returned to hand…
        self.assertNotIn("Gust", gs.player_2_trash)      # …not trashed


class ReturnMeToHandTests(unittest.TestCase):
    def test_source_unit_returns_to_hand(self):
        eng = GameEngine(game_state=GameState())
        eng._game_state.player_1_hand = []
        eng._game_state.player_1_units = [
            PlayedUnit(card="Blitzcrank, Impassive", location="battlefield_1", uid=1)
        ]
        eng._run_effect_codes(
            controller=RequiredTo.PLAYER_1, source="player_1:0",
            trigger="", event_kind="", label="Blitzcrank",
            codes=["RETURN_ME_TO_HAND"],
        )
        gs = eng._game_state
        self.assertEqual(gs.player_1_units, [])
        self.assertIn("Blitzcrank, Impassive", gs.player_1_hand)


class MoveAnyFriendlyUnitsBfToBaseTests(unittest.TestCase):
    def test_chosen_battlefield_units_move_to_base(self):
        eng = GameEngine(game_state=GameState())
        eng._game_state.player_1_units = [
            PlayedUnit(card="A", location="battlefield_1", uid=1),
            PlayedUnit(card="B", location="battlefield_1", uid=2),
            PlayedUnit(card="C", location="base", uid=3),
        ]
        # The (n)[BF] requirement picks the units; here targets = the two BF units.
        eng._run_effect_codes(
            controller=RequiredTo.PLAYER_1, source=None,
            trigger="", event_kind="", label="Emperor's Divide",
            codes=["MOVE_ANY_FRIENDLY_UNITS_BF_TO_BASE"],
            targets=["player_1:0", "player_1:1"],
        )
        locs = [u.location for u in eng._game_state.player_1_units]
        self.assertEqual(locs, ["base", "base", "base"])


if __name__ == "__main__":
    unittest.main()
