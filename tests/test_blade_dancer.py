"""Blade Dancer (legend) two abilities, via the legend-trigger subsystem:
  * "When you choose a friendly unit, you may exhaust me and pay 1 rune to ready
    it."  (WHEN_YOU_CHOOSE_FRIENDLY_UNIT, cost EXHAUST_THIS + PAY_1P, READY_IT)
  * "When you conquer, you may pay 1 energy to ready me."
    (WHEN_I_CONQUER, cost PAY_1_ENERGY, READY_ME)
"""

import random
import unittest
from types import SimpleNamespace
from unittest import mock

from riftbound_engine import abilities as A
from riftbound_engine.abilities import Ability
from riftbound_engine.action_turn.registry import ActionTurnContext
from riftbound_engine.action_turn.builtins import _choose_ability_cost
from riftbound_engine.engine import GameEngine, GameState, PlayedUnit, RequiredTo
from riftbound_engine.triggers import GameEvent

BLADE_DANCER = (
    Ability(triggers=("WHEN_YOU_CHOOSE_FRIENDLY_UNIT",), costs=("EXHAUST_THIS", "PAY_1P"),
            active_effects=("READY_IT",)),
    Ability(triggers=("WHEN_I_CONQUER",), costs=("PAY_1_ENERGY",), active_effects=("READY_ME",)),
)


def _fake(name):
    return BLADE_DANCER if name == "Blade Dancer" else ()


def _pay(eng):
    _choose_ability_cost(ActionTurnContext(
        engine=eng, actor=RequiredTo.PLAYER_1, verb="choose_ability_cost", payload="yes"))


class BladeDancerTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A, "triggered_abilities_for", side_effect=_fake)
        p.start(); self.addCleanup(p.stop)

    def _eng(self):
        eng = GameEngine(game_state=GameState(), rng=random.Random(1))
        eng._game_state.player_1_deck = SimpleNamespace(legend="Blade Dancer")
        return eng

    def test_choose_friendly_unit_readies_it(self):
        eng = self._eng()
        eng._game_state.player_1_units = [
            PlayedUnit(card="Ally", location="base", exhausted=True, uid=1)
        ]
        eng.add_power(RequiredTo.PLAYER_1, "Calm", 1)  # for PAY_1P
        eng._emit(GameEvent(kind="ON_CHOOSE", controller="player_1", source="player_1:0"))
        eng._drain_triggers()
        self.assertIsNotNone(eng._game_state.pending_ability_cost)
        self.assertEqual(eng._game_state.pending_ability_cost.source, "legend:player_1")
        _pay(eng)
        while eng._game_state.pending_chain is not None:
            eng._resolve_chain()
        self.assertFalse(eng._game_state.player_1_units[0].exhausted)   # readied
        self.assertTrue(eng._game_state.player_1_legend_exhausted)      # paid exhaust
        self.assertEqual(eng.total_power(RequiredTo.PLAYER_1), 0)       # paid 1 power

    def test_choosing_enemy_unit_does_not_fire(self):
        eng = self._eng()
        eng._game_state.player_2_units = [PlayedUnit(card="Foe", location="base", uid=9)]
        eng._emit(GameEvent(kind="ON_CHOOSE", controller="player_1", source="player_2:0"))
        self.assertEqual(eng._trigger_queue, [])  # chosen unit is an enemy

    def test_conquer_readies_the_legend(self):
        eng = self._eng()
        eng._game_state.player_1_legend_exhausted = True
        eng.add_energy(RequiredTo.PLAYER_1, 1)  # for PAY_1_ENERGY
        eng._emit(GameEvent(kind="ON_CONQUER", controller="player_1", battlefield="battlefield_1"))
        eng._drain_triggers()
        self.assertIsNotNone(eng._game_state.pending_ability_cost)
        _pay(eng)
        while eng._game_state.pending_chain is not None:
            eng._resolve_chain()
        self.assertFalse(eng._game_state.player_1_legend_exhausted)  # readied


if __name__ == "__main__":
    unittest.main()
