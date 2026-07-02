"""Conditional passives for Leona, Zealot and Monch.

Leona: (1) "If an opponent's score is within 3 of the Victory Score, I enter
ready"; (2) "Stunned enemy units here have -8 Might, min 1."
Monch: "If an opponent controls a stunned unit, I cost 2 energy less and enter
ready."
"""

import unittest

from riftbound_engine.engine import GameEngine, GameState, PlayedUnit, RequiredTo, VICTORY_SCORE


def _eng():
    return GameEngine(game_state=GameState())


class LeonaEnterReadyTests(unittest.TestCase):
    def test_enters_ready_when_opponent_near_victory(self):
        eng = _eng()
        eng.add_score(RequiredTo.PLAYER_2, VICTORY_SCORE - 3)  # opp within 3
        self.assertTrue(eng.unit_enters_ready("Leona, Zealot", RequiredTo.PLAYER_1))

    def test_summoning_sick_when_opponent_far(self):
        eng = _eng()
        eng.add_score(RequiredTo.PLAYER_2, VICTORY_SCORE - 4)  # not yet within 3
        self.assertFalse(eng.unit_enters_ready("Leona, Zealot", RequiredTo.PLAYER_1))


class LeonaStunAuraTests(unittest.TestCase):
    def _setup(self, victim_bonus, victim_stunned, leona_loc="battlefield_1"):
        eng = _eng()
        gs = eng._game_state
        # P2 victim at battlefield_1; P1 Leona (enemy) at the same battlefield.
        gs.player_2_units = [
            PlayedUnit(card="Dummy", location="battlefield_1", bonus_might=victim_bonus,
                       stunned=victim_stunned, uid=1)
        ]
        gs.player_1_units = [PlayedUnit(card="Leona, Zealot", location=leona_loc, uid=2)]
        return eng

    def test_stunned_enemy_here_gets_minus_8(self):
        eng = self._setup(victim_bonus=12, victim_stunned=True)  # base 12
        self.assertEqual(eng.effective_unit_might("player_2", 0), 4)  # 12 - 8

    def test_floored_at_1(self):
        eng = self._setup(victim_bonus=3, victim_stunned=True)  # base 3
        self.assertEqual(eng.effective_unit_might("player_2", 0), 1)  # max(1, 3-8)

    def test_not_applied_when_not_stunned(self):
        eng = self._setup(victim_bonus=12, victim_stunned=False)
        self.assertEqual(eng.effective_unit_might("player_2", 0), 12)  # unaffected

    def test_not_applied_when_leona_elsewhere(self):
        eng = self._setup(victim_bonus=12, victim_stunned=True, leona_loc="battlefield_2")
        self.assertEqual(eng.effective_unit_might("player_2", 0), 12)  # different BF


class MonchTests(unittest.TestCase):
    def _with_stunned_opp(self, stunned):
        eng = _eng()
        eng._game_state.player_2_units = [
            PlayedUnit(card="Dummy", location="battlefield_1", stunned=stunned, uid=1)
        ]
        return eng

    def test_cost_2_less_and_ready_when_opp_has_stunned(self):
        eng = self._with_stunned_opp(True)
        base = eng.card_energy_cost("Monch")  # 6
        self.assertEqual(eng.effective_card_energy_cost("Monch", RequiredTo.PLAYER_1), base - 2)
        self.assertTrue(eng.unit_enters_ready("Monch", RequiredTo.PLAYER_1))

    def test_full_cost_and_sick_when_no_stunned(self):
        eng = self._with_stunned_opp(False)
        base = eng.card_energy_cost("Monch")
        self.assertEqual(eng.effective_card_energy_cost("Monch", RequiredTo.PLAYER_1), base)
        self.assertFalse(eng.unit_enters_ready("Monch", RequiredTo.PLAYER_1))


if __name__ == "__main__":
    unittest.main()
