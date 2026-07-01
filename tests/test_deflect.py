"""[Deflect]: choosing an OPPONENT unit with Deflect as an ability target taxes
the ability's controller 1 Power per stack (any domain), or they decline and the
target is dropped. Combat targeting never routes through the chain-push gate, so
it's exempt. Deflect stacks (printed + this-turn grant + equipment).
"""

from __future__ import annotations

import unittest

from riftbound_engine.csv_data import deflect_amount_in_text
from riftbound_engine.engine import GameEngine, GameState, PlayedUnit, RequiredTo, build_deck_from_id


def _engine_with_units() -> GameEngine:
    """A started, post-setup game (P1 active in the action turn) with 3 Fury
    Power for P1; P2 has one unit at a battlefield."""
    gs = GameState()
    gs.started = True
    gs.is_mulligan_done = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    gs.current_player = RequiredTo.PLAYER_1
    gs.player_1_deck = build_deck_from_id("ezreal_prodigal_explorer")
    gs.player_2_deck = build_deck_from_id("irelia_nates")
    gs.player_1_units = [PlayedUnit(card="Scout", location="base", exhausted=False, uid=1)]
    gs.player_2_units = [PlayedUnit(card="Target Dummy", location="battlefield_1", exhausted=False, uid=2)]
    gs.player_1_power = {"Fury": 3}
    gs.battlefield_1 = "A"
    gs.battlefield_2 = "B"
    return GameEngine(game_state=gs)


class DeflectHelperTests(unittest.TestCase):
    def test_deflect_amount_in_text(self) -> None:
        self.assertEqual(deflect_amount_in_text("[Deflect 2]"), 2)
        self.assertEqual(deflect_amount_in_text("[Deflect]"), 1)  # bare = 1

    def test_unit_deflect_count_from_grant_and_stacking(self) -> None:
        eng = _engine_with_units()
        self.assertEqual(eng.unit_deflect_count("player_2", 0), 0)
        eng._game_state.player_2_units[0].bonus_deflect = 2
        self.assertEqual(eng.unit_deflect_count("player_2", 0), 2)


class DeflectGateTests(unittest.TestCase):
    def test_no_gate_when_target_has_no_deflect(self) -> None:
        eng = _engine_with_units()
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Gust", [["player_2:0"]])
        # Pushed straight onto the chain; no Deflect decision pending.
        self.assertIsNone(eng._game_state.pending_deflect)
        self.assertIsNotNone(eng._game_state.pending_chain)

    def test_friendly_target_is_not_taxed(self) -> None:
        eng = _engine_with_units()
        eng._game_state.player_1_units[0].bonus_deflect = 1  # caster's OWN unit
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Gust", [["player_1:0"]])
        self.assertIsNone(eng._game_state.pending_deflect)
        self.assertIsNotNone(eng._game_state.pending_chain)

    def test_pay_lets_ability_keep_the_target(self) -> None:
        eng = _engine_with_units()
        eng._game_state.player_2_units[0].bonus_deflect = 1
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Gust", [["player_2:0"]])
        pd = eng._game_state.pending_deflect
        self.assertIsNotNone(pd)
        self.assertEqual(pd.queue[0][0], "player_2:0")
        self.assertEqual(pd.queue[0][1], 1)  # 1 stack
        # Pay: 1 Power deducted, target kept, item on chain.
        eng.resolve_deflect_decision(RequiredTo.PLAYER_1, pay=True)
        self.assertIsNone(eng._game_state.pending_deflect)
        self.assertEqual(eng._total_power(RequiredTo.PLAYER_1), 2)  # 3 - 1
        chain = eng._game_state.pending_chain
        self.assertIsNotNone(chain)
        self.assertIn("player_2:0", chain.items[0].targets)

    def test_decline_drops_only_that_target(self) -> None:
        eng = _engine_with_units()
        eng._game_state.player_2_units[0].bonus_deflect = 1
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Gust", [["player_2:0"]])
        eng.resolve_deflect_decision(RequiredTo.PLAYER_1, pay=False)
        self.assertIsNone(eng._game_state.pending_deflect)
        self.assertEqual(eng._total_power(RequiredTo.PLAYER_1), 3)  # nothing paid
        chain = eng._game_state.pending_chain
        self.assertIsNotNone(chain)
        self.assertNotIn("player_2:0", chain.items[0].targets)  # dropped

    def test_stacking_costs_more_and_gates_affordability(self) -> None:
        eng = _engine_with_units()
        eng._game_state.player_1_power = {"Fury": 1}  # only 1 Power
        eng._game_state.player_2_units[0].bonus_deflect = 2  # needs 2
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Gust", [["player_2:0"]])
        # Options: can't afford 2 → only decline is offered.
        out = eng.start()
        self.assertNotIn("play:pay_deflect", out.player_1_options)
        self.assertIn("play:decline_deflect", out.player_1_options)
        # Trying to pay anyway is rejected.
        with self.assertRaises(ValueError):
            eng.resolve_deflect_decision(RequiredTo.PLAYER_1, pay=True)

    def test_pay_offered_when_affordable(self) -> None:
        eng = _engine_with_units()
        eng._game_state.player_2_units[0].bonus_deflect = 2
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Gust", [["player_2:0"]])
        out = eng.start()
        self.assertIn("play:pay_deflect", out.player_1_options)
        self.assertIn("play:decline_deflect", out.player_1_options)
        eng.resolve_deflect_decision(RequiredTo.PLAYER_1, pay=True)
        self.assertEqual(eng._total_power(RequiredTo.PLAYER_1), 1)  # 3 - 2


if __name__ == "__main__":
    unittest.main()
