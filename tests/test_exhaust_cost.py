"""EXHAUST_THIS as a payable cost on TRIGGERED abilities.

When such a trigger fires, the controller is asked whether to exhaust the source
to put the effect on the chain (PendingAbilityCost). Paying exhausts the source
and pushes the effect; declining (or a source that can't pay) means the effect
never reaches the chain. Covers a unit source and the Chemtech Cask gear."""

import unittest
from unittest import mock

from riftbound_engine import abilities as A
from riftbound_engine.abilities import Ability
from riftbound_engine.action_turn.builtins import _choose_ability_cost
from riftbound_engine.action_turn.context import ActionTurnContext
from riftbound_engine.engine import (
    GameEngine,
    GameState,
    PlayedGear,
    PlayedUnit,
    RequiredTo,
)
from riftbound_engine.triggers import GameEvent

# A unit whose "when a friendly unit dies, you may exhaust me: draw 1" ability.
UNIT_ABILITY = (Ability(triggers=("WHEN_FRIENDLY_UNIT_DIES",), active_effects=("DRAW_1",), costs=("EXHAUST_THIS",)),)
# Chemtech Cask gear: "when you play a spell on an opponent's turn, you may
# exhaust me to play a Gold token." (effect_text=False → a standalone-gear
# rule-text ability, not attached equipment.)
CASK_ABILITY = (Ability(triggers=("WHEN_YOU_PLAY_SPELL_OPP_TURN",), active_effects=("PLAY_GOLD_EXHAUSTED",), costs=("EXHAUST_THIS",), effect_text=False),)


def _started(**over):
    base = dict(started=True, abcd_a_done=True, abcd_b_done=True, abcd_c_done=True, abcd_d_done=True,
                current_player=RequiredTo.PLAYER_1)
    base.update(over)
    return GameState(**base)


def _yes(eng):
    _choose_ability_cost(ActionTurnContext(engine=eng, actor=RequiredTo.PLAYER_1, verb="choose_ability_cost", payload="yes"))


def _no(eng):
    _choose_ability_cost(ActionTurnContext(engine=eng, actor=RequiredTo.PLAYER_1, verb="choose_ability_cost", payload="no"))


class UnitSourceExhaustCostTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A, "triggered_abilities_for", side_effect=lambda n: UNIT_ABILITY if n == "Hexcore" else ())
        p.start(); self.addCleanup(p.stop)

    def _engine(self, exhausted=False):
        gs = _started(player_1_units=[PlayedUnit(card="Hexcore", location="base", exhausted=exhausted, uid=1)],
                      player_1_library=["A", "B"], player_1_hand=[])
        eng = GameEngine(game_state=gs)
        eng._emit(GameEvent(kind="ON_DEATH", controller="player_1", source="player_1:9"))
        eng._drain_triggers()
        return eng

    def test_trigger_opens_pay_decision(self):
        eng = self._engine()
        pend = eng._game_state.pending_ability_cost
        self.assertIsNotNone(pend)
        self.assertEqual(pend.actor, RequiredTo.PLAYER_1)
        self.assertEqual(pend.source, "player_1:0")
        # Effect is NOT on the chain yet — only after paying.
        self.assertIsNone(eng._game_state.pending_chain)

    def test_pay_exhausts_source_and_puts_effect_on_chain(self):
        eng = self._engine()
        _yes(eng)
        gs = eng._game_state
        self.assertIsNone(gs.pending_ability_cost)
        self.assertTrue(gs.player_1_units[0].exhausted)  # cost paid
        self.assertIsNotNone(gs.pending_chain)
        self.assertIn("DRAW_1", gs.pending_chain.items[0].effect.effects)

    def test_decline_does_nothing(self):
        eng = self._engine()
        _no(eng)
        gs = eng._game_state
        self.assertIsNone(gs.pending_ability_cost)
        self.assertFalse(gs.player_1_units[0].exhausted)  # not paid
        self.assertIsNone(gs.pending_chain)  # effect never reached the chain

    def test_already_exhausted_source_cannot_pay(self):
        eng = self._engine(exhausted=True)
        # Source can't be exhausted again → no decision is even offered.
        self.assertIsNone(eng._game_state.pending_ability_cost)
        self.assertIsNone(eng._game_state.pending_chain)


class ChemtechCaskGearTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A, "triggered_abilities_for", side_effect=lambda n: CASK_ABILITY if n == "Chemtech Cask" else ())
        p.start(); self.addCleanup(p.stop)

    def test_gear_trigger_fires_and_pays_to_chain(self):
        gs = _started(player_2=None) if False else _started()
        gs.player_1_gears = [PlayedGear(card="Chemtech Cask", location="base", exhausted=False)]
        eng = GameEngine(game_state=gs)
        # Simulate "you play a spell on the opponent's turn".
        eng._emit(GameEvent(kind="ON_PLAY_SPELL", controller="player_1", data={"card": "Some Spell"}))
        eng._drain_triggers()
        pend = eng._game_state.pending_ability_cost
        self.assertIsNotNone(pend)
        self.assertEqual(pend.source, "gear:player_1:0")
        _yes(eng)
        gs2 = eng._game_state
        self.assertTrue(gs2.player_1_gears[0].exhausted)  # cask exhausted to pay
        self.assertIsNotNone(gs2.pending_chain)
        self.assertIn("PLAY_GOLD_EXHAUSTED", gs2.pending_chain.items[0].effect.effects)


if __name__ == "__main__":
    unittest.main()
