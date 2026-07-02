"""Activity-log feed behaviour:

  1. Raw events ("A unit was played", "A showdown began", …) are NOT written to
     the feed at all — they duplicate the action log and only the abilities
     they fire carry meaning.
  2. A trigger that fires records a feed line carrying the structured ``code``
     (the trigger code) and ``card`` (the source card name), so the UI can
     translate it to human text instead of showing the raw code.
"""

import unittest
from unittest import mock

from riftbound_engine import abilities as A
from riftbound_engine.abilities import Ability
from riftbound_engine.action_turn.builtins import _move_unit
from riftbound_engine.action_turn.context import ActionTurnContext
from riftbound_engine.engine import GameEngine, GameState, PlayedUnit, RequiredTo
from riftbound_engine.triggers import GameEvent

WATCHER = (Ability(triggers=("WHEN_SHOWDOWN_BEGINS_HERE",), active_effects=("DRAW_1",)),)


def _fake(name):
    return WATCHER if name == "Showdown Watcher" else ()


def _started(**kw) -> GameState:
    return GameState(
        started=True,
        abcd_a_done=True,
        abcd_b_done=True,
        abcd_c_done=True,
        abcd_d_done=True,
        **kw,
    )


class FeedSuppressionTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A, "triggered_abilities_for", side_effect=_fake)
        p.start()
        self.addCleanup(p.stop)

    def test_raw_event_with_no_matching_ability_is_not_logged(self):
        # A lone vanilla unit (no abilities) — emitting an event it doesn't
        # respond to must not append anything to the feed.
        gs = _started(
            current_player=RequiredTo.PLAYER_1,
            player_1_units=[PlayedUnit(card="Lonely Poro", location="base", uid=1)],
        )
        eng = GameEngine(game_state=gs)
        before = len(eng._game_state.event_feed)
        eng._emit(GameEvent(kind="ON_DEATH", controller="player_1"))
        self.assertEqual(len(eng._game_state.event_feed), before, "no-op event should not log")

    def test_firing_trigger_logs_only_the_trigger_not_the_raw_event(self):
        # P1 moves a Watcher onto an uncontrolled BF → showdown opens →
        # ON_SHOWDOWN_BEGIN fires the Watcher's ability. The feed should carry
        # the trigger line (with structured code + card) but NO raw "event"
        # line for the showdown beginning — that would just duplicate the log.
        gs = _started(
            current_player=RequiredTo.PLAYER_1,
            battlefield_1_controller=None,
            player_1_units=[
                PlayedUnit(card="Showdown Watcher", location="base", exhausted=False, uid=1)
            ],
        )
        eng = GameEngine(game_state=gs)
        _move_unit(
            ActionTurnContext(
                engine=eng, actor=RequiredTo.PLAYER_1, verb="move_unit", payload="0:battlefield_1"
            )
        )
        eng._drain_triggers()

        feed = eng._game_state.event_feed
        triggers = [e for e in feed if e.kind == "trigger"]

        # No raw event-announcement line for the showdown beginning.
        self.assertFalse(any(e.code == "ON_SHOWDOWN_BEGIN" for e in feed))

        # The fired ability is logged as a trigger line with the structured
        # trigger code + source card for the UI to translate.
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0].code, "WHEN_SHOWDOWN_BEGINS_HERE")
        self.assertEqual(triggers[0].card, "Showdown Watcher")
        # The raw fallback text is still populated.
        self.assertIn("WHEN_SHOWDOWN_BEGINS_HERE", triggers[0].text)


if __name__ == "__main__":
    unittest.main()
