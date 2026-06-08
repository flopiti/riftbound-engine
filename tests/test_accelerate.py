"""[Accelerate] — after a unit's location is chosen, its controller may pay an
additional cost (extra Energy + a domain rune) to have it enter READY instead
of exhausted. Modeled on [Repeat]: the decision appears AFTER choose_location.

We use a real Accelerate card (Legion Rearguard: 2 Energy, Fury; Accelerate =
1 Energy + 1 Fury). Tests drive the handlers directly (like test_equip_timing)
and check the decision flow: play → choose location → pending_accelerate → pay
(ready) or decline (stays exhausted), and the cost is charged only on pay."""

import unittest

from riftbound_engine.action_turn.builtins import (
    _choose_accelerate,
    _choose_location,
    _play_unit,
)
from riftbound_engine.action_turn.context import ActionTurnContext
from riftbound_engine.csv_data import card_accelerate_cost
from riftbound_engine.engine import GameEngine, GameState, RequiredTo
from riftbound_engine.shortcuts import compute_accelerate_intents

CARD = "Legion Rearguard"  # 2 Energy, Fury; Accelerate (1 Energy, 1 Fury)


def _started_state(**over) -> GameState:
    base = dict(
        started=True,
        abcd_a_done=True,
        abcd_b_done=True,
        abcd_c_done=True,
        abcd_d_done=True,
        current_player=RequiredTo.PLAYER_1,
        player_1_hand=[CARD],
        player_1_energy=10,
        player_1_power={"Fury": 5},
    )
    base.update(over)
    return GameState(**base)


def _ctx(eng, verb, payload):
    return ActionTurnContext(engine=eng, actor=RequiredTo.PLAYER_1, verb=verb, payload=payload)


def _play_and_place(eng):
    _play_unit(_ctx(eng, "play_unit", "0"))
    _choose_location(_ctx(eng, "choose_location", "base"))


class AccelerateDecisionTests(unittest.TestCase):
    def test_card_has_accelerate_cost(self):
        self.assertEqual(card_accelerate_cost(CARD), (1, 1, "Fury"))

    def test_decision_opens_after_location_and_unit_starts_exhausted(self):
        eng = GameEngine(game_state=_started_state())
        _play_and_place(eng)
        gs = eng._game_state
        # Unit is on the board, exhausted, and an Accelerate decision is pending.
        self.assertEqual(gs.player_1_units[0].card, CARD)
        self.assertTrue(gs.player_1_units[0].exhausted)
        self.assertIsNotNone(gs.pending_accelerate)
        self.assertEqual(gs.pending_accelerate.cost["energy"], 1)
        self.assertEqual(gs.pending_accelerate.cost["power"], {"Fury": 1})

    def test_pay_readies_the_unit_and_charges_cost(self):
        eng = GameEngine(game_state=_started_state())
        _play_and_place(eng)
        # Normal play already spent 2 Energy; pools now 8 Energy, 5 Fury.
        self.assertEqual(eng.player_energy(RequiredTo.PLAYER_1), 8)
        _choose_accelerate(_ctx(eng, "choose_accelerate", "yes"))
        gs = eng._game_state
        self.assertIsNone(gs.pending_accelerate)
        self.assertFalse(gs.player_1_units[0].exhausted)  # READY
        # Accelerate cost: 1 Energy + 1 Fury.
        self.assertEqual(eng.player_energy(RequiredTo.PLAYER_1), 7)
        self.assertEqual(eng.player_power(RequiredTo.PLAYER_1).get("Fury"), 4)

    def test_decline_leaves_unit_exhausted_and_spends_nothing(self):
        eng = GameEngine(game_state=_started_state())
        _play_and_place(eng)
        before_e = eng.player_energy(RequiredTo.PLAYER_1)
        before_f = eng.player_power(RequiredTo.PLAYER_1).get("Fury")
        _choose_accelerate(_ctx(eng, "choose_accelerate", "no"))
        gs = eng._game_state
        self.assertIsNone(gs.pending_accelerate)
        self.assertTrue(gs.player_1_units[0].exhausted)
        self.assertEqual(eng.player_energy(RequiredTo.PLAYER_1), before_e)
        self.assertEqual(eng.player_power(RequiredTo.PLAYER_1).get("Fury"), before_f)

    def test_pay_rejected_when_unaffordable(self):
        # 2 Energy for the normal play, but no Fury for the Accelerate rune.
        eng = GameEngine(game_state=_started_state(player_1_power={"Fury": 0}))
        _play_and_place(eng)
        with self.assertRaises(ValueError):
            _choose_accelerate(_ctx(eng, "choose_accelerate", "yes"))
        # Decision still pending, unit still exhausted.
        self.assertIsNotNone(eng._game_state.pending_accelerate)
        self.assertTrue(eng._game_state.player_1_units[0].exhausted)

    def test_intents_offer_accelerate_payment(self):
        eng = GameEngine(game_state=_started_state())
        _play_and_place(eng)
        intents = compute_accelerate_intents(eng, RequiredTo.PLAYER_1)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].play_action, "accelerate")
        self.assertEqual(intents[0].card_name, CARD)
        self.assertTrue(intents[0].combos)

    def test_non_accelerate_unit_has_no_decision(self):
        eng = GameEngine(game_state=_started_state(player_1_hand=["Plundering Poro"]))
        _play_unit(_ctx(eng, "play_unit", "0"))
        _choose_location(_ctx(eng, "choose_location", "base"))
        self.assertIsNone(eng._game_state.pending_accelerate)
        self.assertTrue(eng._game_state.player_1_units[0].exhausted)


if __name__ == "__main__":
    unittest.main()
