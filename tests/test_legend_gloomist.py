"""Legend triggered abilities (Gloomist): "When you or an ally hold, you may
exhaust me to draw 1." Exercises the new legend-trigger subsystem end to end —
legends are now scanned as ability sources and are a valid EXHAUST_THIS source.
"""

import random
import unittest
from types import SimpleNamespace
from unittest import mock

from riftbound_engine import abilities as A
from riftbound_engine.abilities import Ability
from riftbound_engine.action_turn.registry import ActionTurnContext
from riftbound_engine.action_turn.builtins import _choose_ability_cost
from riftbound_engine.engine import GameEngine, GameState, RequiredTo
from riftbound_engine.triggers import GameEvent

GLOOMIST = (
    Ability(triggers=("WHEN_YOU_HOLD",), costs=("EXHAUST_THIS",), active_effects=("DRAW_1",)),
)


def _fake(name):
    return GLOOMIST if name == "Gloomist" else ()


class GloomistLegendTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A, "triggered_abilities_for", side_effect=_fake)
        p.start(); self.addCleanup(p.stop)

    def _eng(self, legend_exhausted=False):
        eng = GameEngine(game_state=GameState(), rng=random.Random(1))
        gs = eng._game_state
        gs.player_1_deck = SimpleNamespace(legend="Gloomist")
        gs.player_1_library = ["A", "B"]
        gs.player_1_hand = []
        gs.player_1_legend_exhausted = legend_exhausted
        return eng

    def _hold(self, eng):
        eng._emit(GameEvent(kind="ON_HOLD", controller="player_1", battlefield="battlefield_1"))

    def _resolve_all(self, eng):
        while eng._game_state.pending_chain is not None:
            eng._resolve_chain()

    def test_trigger_fires_from_legend_with_exhaust_cost(self):
        eng = self._eng()
        self._hold(eng)
        q = eng._trigger_queue
        self.assertEqual(len(q), 1)
        te = q[0]
        self.assertEqual(te.source, "legend:player_1")
        self.assertEqual(te.effects, ("DRAW_1",))
        self.assertIn("EXHAUST_THIS", te.costs)

    def test_paying_exhausts_legend_and_draws(self):
        eng = self._eng()
        self._hold(eng)
        eng._drain_triggers()
        pend = eng._game_state.pending_ability_cost
        self.assertIsNotNone(pend)
        self.assertEqual(pend.source, "legend:player_1")
        _choose_ability_cost(
            ActionTurnContext(engine=eng, actor=RequiredTo.PLAYER_1,
                              verb="choose_ability_cost", payload="yes")
        )
        self._resolve_all(eng)
        self.assertTrue(eng._game_state.player_1_legend_exhausted)  # paid the cost
        self.assertEqual(len(eng._game_state.player_1_hand), 1)     # drew 1

    def test_declining_does_nothing(self):
        eng = self._eng()
        self._hold(eng)
        eng._drain_triggers()
        _choose_ability_cost(
            ActionTurnContext(engine=eng, actor=RequiredTo.PLAYER_1,
                              verb="choose_ability_cost", payload="no")
        )
        self.assertFalse(eng._game_state.player_1_legend_exhausted)
        self.assertEqual(eng._game_state.player_1_hand, [])

    def test_cannot_pay_when_legend_already_exhausted(self):
        eng = self._eng(legend_exhausted=True)
        self._hold(eng)
        eng._drain_triggers()
        # Source unpayable → "you may pay" with no way to pay → no decision, no draw.
        self.assertIsNone(eng._game_state.pending_ability_cost)
        self.assertEqual(eng._game_state.player_1_hand, [])

    def test_opponent_hold_does_not_fire_my_legend(self):
        eng = self._eng()
        eng._emit(GameEvent(kind="ON_HOLD", controller="player_2", battlefield="battlefield_1"))
        self.assertEqual(eng._trigger_queue, [])  # FRIENDLY scope: only my hold


if __name__ == "__main__":
    unittest.main()
