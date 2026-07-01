"""WHEN_SHOWDOWN_BEGINS_HERE: when a showdown opens at a battlefield, every card
sitting HERE fires — on EITHER side, and even when the battlefield was
uncontrolled (no defender). This is the broader sibling of WHEN_YOU_DEFEND_HERE
(ON_DEFEND), which fires only for the prior controller and never on an
uncontrolled battlefield.

We use a stand-in card "Showdown Watcher" whose only ability is
WHEN_SHOWDOWN_BEGINS_HERE -> DRAW_1, and assert the ability lands on the chain
the moment a unit moves in to open the showdown."""

import unittest
from unittest import mock

from riftbound_engine import abilities as A
from riftbound_engine.abilities import Ability
from riftbound_engine.action_turn.builtins import _move_unit
from riftbound_engine.action_turn.context import ActionTurnContext
from riftbound_engine.engine import (
    GameEngine,
    GameState,
    PlayedUnit,
    RequiredTo,
)
from riftbound_engine.triggers import GameEvent, trigger_matches

WATCHER = (Ability(triggers=("WHEN_SHOWDOWN_BEGINS_HERE",), active_effects=("DRAW_1",)),)


def _fake(name):
    return WATCHER if name == "Showdown Watcher" else ()


class ShowdownBeginsTriggerMapTests(unittest.TestCase):
    """Pure-function checks on the trigger -> event mapping (no engine)."""

    def _ev(self, kind, bf="battlefield_1"):
        return GameEvent(kind=kind, controller="player_1", battlefield=bf)

    def test_matches_only_on_showdown_begin_here(self):
        kw = dict(owner_controller="player_1", owner_location="battlefield_1")
        self.assertTrue(
            trigger_matches("WHEN_SHOWDOWN_BEGINS_HERE", self._ev("ON_SHOWDOWN_BEGIN"), **kw)
        )
        # Wrong event kind.
        self.assertFalse(
            trigger_matches("WHEN_SHOWDOWN_BEGINS_HERE", self._ev("ON_DEFEND"), **kw)
        )
        # Right event, but a different battlefield (HERE scope).
        self.assertFalse(
            trigger_matches(
                "WHEN_SHOWDOWN_BEGINS_HERE",
                self._ev("ON_SHOWDOWN_BEGIN", "battlefield_2"),
                **kw,
            )
        )

    def test_fires_for_either_side_regardless_of_event_controller(self):
        # HERE scope ignores who caused the event: an owner on the OPPOSITE
        # side of the initiator still fires as long as they sit at the BF.
        ev = self._ev("ON_SHOWDOWN_BEGIN")  # controller=player_1 (the initiator)
        self.assertTrue(
            trigger_matches(
                "WHEN_SHOWDOWN_BEGINS_HERE",
                ev,
                owner_controller="player_2",
                owner_location="battlefield_1",
            )
        )


class ShowdownBeginsEndToEndTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A, "triggered_abilities_for", side_effect=_fake)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _started_state(**kw) -> GameState:
        return GameState(
            started=True,
            abcd_a_done=True,
            abcd_b_done=True,
            abcd_c_done=True,
            abcd_d_done=True,
            **kw,
        )

    def test_invade_opponent_bf_fires_for_defender_here(self):
        # P2 walks a unit onto P1's controlled battlefield. P1's Watcher sits
        # there (defending side) and must fire when the showdown opens.
        gs = self._started_state(
            current_player=RequiredTo.PLAYER_2,
            battlefield_1="Some Battlefield",
            battlefield_1_controller=RequiredTo.PLAYER_1,
            player_1_units=[PlayedUnit(card="Showdown Watcher", location="battlefield_1", uid=1)],
            player_2_units=[PlayedUnit(card="Lonely Poro", location="base", exhausted=False, uid=2)],
        )
        eng = GameEngine(game_state=gs)
        _move_unit(
            ActionTurnContext(
                engine=eng, actor=RequiredTo.PLAYER_2, verb="move_unit", payload="0:battlefield_1"
            )
        )
        eng._drain_triggers()
        chain = eng._game_state.pending_chain
        self.assertIsNotNone(chain)
        self.assertIn("DRAW_1", chain.items[0].effect.effects)
        # The defender (P1) owns the fired ability.
        self.assertEqual(chain.items[0].effect.controller, "player_1")

    def test_invading_unit_itself_fires_here(self):
        # The attacker's OWN invading unit is "here" the instant it lands, so
        # a Watcher walked in by the initiator fires for the initiator.
        gs = self._started_state(
            current_player=RequiredTo.PLAYER_1,
            battlefield_1="Some Battlefield",
            battlefield_1_controller=RequiredTo.PLAYER_2,
            player_1_units=[PlayedUnit(card="Showdown Watcher", location="base", exhausted=False, uid=1)],
        )
        eng = GameEngine(game_state=gs)
        _move_unit(
            ActionTurnContext(
                engine=eng, actor=RequiredTo.PLAYER_1, verb="move_unit", payload="0:battlefield_1"
            )
        )
        eng._drain_triggers()
        chain = eng._game_state.pending_chain
        self.assertIsNotNone(chain)
        self.assertIn("DRAW_1", chain.items[0].effect.effects)
        self.assertEqual(chain.items[0].effect.controller, "player_1")

    def test_uncontrolled_bf_fires_where_on_defend_would_not(self):
        # No prior controller => no defender => ON_DEFEND never fires. But the
        # showdown still BEGINS, so WHEN_SHOWDOWN_BEGINS_HERE must fire for the
        # unit that just walked onto the uncontrolled battlefield.
        gs = self._started_state(
            current_player=RequiredTo.PLAYER_1,
            battlefield_1="Some Battlefield",
            battlefield_1_controller=None,
            player_1_units=[PlayedUnit(card="Showdown Watcher", location="base", exhausted=False, uid=1)],
        )
        eng = GameEngine(game_state=gs)
        _move_unit(
            ActionTurnContext(
                engine=eng, actor=RequiredTo.PLAYER_1, verb="move_unit", payload="0:battlefield_1"
            )
        )
        eng._drain_triggers()
        chain = eng._game_state.pending_chain
        self.assertIsNotNone(chain)
        self.assertIn("DRAW_1", chain.items[0].effect.effects)
        self.assertEqual(chain.items[0].effect.controller, "player_1")

    def test_does_not_fire_for_unit_at_other_battlefield(self):
        # A Watcher parked at battlefield_2 is not "here" when the showdown
        # opens at battlefield_1.
        gs = self._started_state(
            current_player=RequiredTo.PLAYER_1,
            battlefield_1_controller=None,
            player_1_units=[
                PlayedUnit(card="Lonely Poro", location="base", exhausted=False, uid=1),
                PlayedUnit(card="Showdown Watcher", location="battlefield_2", uid=2),
            ],
        )
        eng = GameEngine(game_state=gs)
        _move_unit(
            ActionTurnContext(
                engine=eng, actor=RequiredTo.PLAYER_1, verb="move_unit", payload="0:battlefield_1"
            )
        )
        eng._drain_triggers()
        self.assertIsNone(eng._game_state.pending_chain)


if __name__ == "__main__":
    unittest.main()
