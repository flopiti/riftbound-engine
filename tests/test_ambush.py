"""[Ambush]: play a Unit from hand as a Reaction, at a battlefield where you
already have a unit, paying its normal cost. Reaction timing only (open chain or
showdown); it enters exhausted.
"""

import unittest

from riftbound_engine.action_turn.registry import ActionTurnContext
from riftbound_engine.action_turn.builtins import _ambush
from riftbound_engine.engine import GameEngine, GameState, PlayedUnit, RequiredTo

KHAZIX = "Kha'Zix, Mutating Horror"  # 4 Energy, 1 Chaos Power, [Ambush]


def _do(eng, payload):
    _ambush(ActionTurnContext(engine=eng, actor=RequiredTo.PLAYER_1, verb="ambush", payload=payload))


class AmbushTests(unittest.TestCase):
    def _eng(self, *, has_unit_at="battlefield_1", open_chain=True):
        eng = GameEngine(game_state=GameState())
        gs = eng._game_state
        if has_unit_at:
            gs.player_1_units = [PlayedUnit(card="Ally", location=has_unit_at, uid=1)]
        gs.player_1_hand = [KHAZIX]
        eng.add_energy(RequiredTo.PLAYER_1, 4)
        eng.add_power(RequiredTo.PLAYER_1, "Chaos", 1)
        if open_chain:
            eng._push_spell_to_chain(RequiredTo.PLAYER_2, "Gust", [[]])  # sets an open chain
            gs.pending_chain.priority = RequiredTo.PLAYER_1  # P1 holds priority to react
        return eng

    def test_offered_and_plays_to_battlefield_with_your_unit(self):
        eng = self._eng()
        self.assertIn("play:ambush:0:battlefield_1", eng._ambush_options(RequiredTo.PLAYER_1))
        _do(eng, "0:battlefield_1")
        gs = eng._game_state
        played = [u for u in gs.player_1_units if u.card == KHAZIX]
        self.assertEqual(len(played), 1)
        self.assertEqual(played[0].location, "battlefield_1")
        self.assertTrue(played[0].exhausted)               # enters exhausted
        self.assertEqual(eng.player_energy(RequiredTo.PLAYER_1), 0)   # paid 4
        self.assertEqual(eng.total_power(RequiredTo.PLAYER_1), 0)     # paid 1
        self.assertNotIn(KHAZIX, gs.player_1_hand)

    def test_not_offered_at_battlefield_without_your_unit(self):
        eng = self._eng(has_unit_at="battlefield_1")
        opts = eng._ambush_options(RequiredTo.PLAYER_1)
        self.assertIn("play:ambush:0:battlefield_1", opts)
        self.assertNotIn("play:ambush:0:battlefield_2", opts)  # no unit there
        with self.assertRaises(ValueError):
            _do(eng, "0:battlefield_2")

    def test_rejected_outside_reaction_timing(self):
        eng = self._eng(open_chain=False)
        self.assertEqual(eng._game_state.pending_chain, None)
        with self.assertRaises(ValueError):
            _do(eng, "0:battlefield_1")  # no open chain / showdown → illegal

    def test_no_units_anywhere_means_no_ambush(self):
        eng = self._eng(has_unit_at=None)
        self.assertEqual(eng._ambush_options(RequiredTo.PLAYER_1), [])


if __name__ == "__main__":
    unittest.main()
