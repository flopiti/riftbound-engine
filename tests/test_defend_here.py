"""WHEN_YOU_DEFEND_HERE (Fortified Position): when a showdown opens against a
battlefield you control, you're the defender — fire "when you defend here".

Fortified Position: "When you defend here, choose a unit. It gains [Shield 2]
this turn." This ties the whole defender/Shield arc together: the trigger fires
ON_DEFEND, the controller picks one of their units here (a choice effect), and
that unit's Shield then boosts its Might while it defends."""

import unittest
from unittest import mock

from riftbound_engine import abilities as A
from riftbound_engine.abilities import Ability
from riftbound_engine.action_turn.builtins import _choose_effect_target, _move_unit
from riftbound_engine.action_turn.context import ActionTurnContext
from riftbound_engine.csv_data import card_might_of
from riftbound_engine.engine import (
    GameEngine,
    GameState,
    PendingShowdown,
    PlayedUnit,
    RequiredTo,
)
from riftbound_engine.triggers import GameEvent, trigger_matches

FORT = (Ability(triggers=("WHEN_YOU_DEFEND_HERE",), active_effects=("GIVE_UNIT_SHIELD",)),)


def _fake(name):
    return FORT if name == "Fortified Position" else ()


class DefendTriggerMapTests(unittest.TestCase):
    def _ev(self, kind, bf="battlefield_1"):
        return GameEvent(kind=kind, controller="player_1", battlefield=bf)

    def test_defend_here_matches_only_on_defend_here(self):
        kw = dict(owner_controller="player_1", owner_location="battlefield_1")
        self.assertTrue(trigger_matches("WHEN_YOU_DEFEND_HERE", self._ev("ON_DEFEND"), **kw))
        self.assertFalse(trigger_matches("WHEN_YOU_DEFEND_HERE", self._ev("ON_CONQUER"), **kw))
        self.assertFalse(
            trigger_matches("WHEN_YOU_DEFEND_HERE", self._ev("ON_DEFEND", "battlefield_2"), **kw)
        )


class FortifiedPositionTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A, "triggered_abilities_for", side_effect=_fake)
        p.start()
        self.addCleanup(p.stop)

    def _engine(self):
        gs = GameState(
            started=True,
            abcd_a_done=True,
            abcd_b_done=True,
            abcd_c_done=True,
            abcd_d_done=True,
            current_player=RequiredTo.PLAYER_2,  # P2 is attacking
            battlefield_1="Fortified Position",
            battlefield_1_controller=RequiredTo.PLAYER_1,  # P1 holds it → defender
            player_1_units=[PlayedUnit(card="Plundering Poro", location="battlefield_1", uid=1)],
            pending_showdown=PendingShowdown(battlefield="battlefield_1", initiator=RequiredTo.PLAYER_2),
        )
        return GameEngine(game_state=gs)

    def test_on_defend_fires_the_battlefields_ability(self):
        eng = self._engine()
        eng._emit(GameEvent(kind="ON_DEFEND", controller="player_1", battlefield="battlefield_1"))
        eng._drain_triggers()
        chain = eng._game_state.pending_chain
        self.assertIsNotNone(chain)
        self.assertIn("GIVE_UNIT_SHIELD", chain.items[0].effect.effects)

    def test_choice_grants_shield_and_boosts_might_while_defending(self):
        eng = self._engine()
        base = card_might_of("Plundering Poro") or 0
        # Resolve the defend ability's effect; the battlefield slot is the source.
        eng._run_effect_codes(
            controller=RequiredTo.PLAYER_1,
            source="battlefield_1",
            trigger="WHEN_YOU_DEFEND_HERE",
            event_kind="ON_DEFEND",
            label="Fortified Position",
            codes=["GIVE_UNIT_SHIELD"],
        )
        choice = eng._game_state.pending_effect_choice
        self.assertIsNotNone(choice)
        self.assertIn("p1-0", choice.options)  # the defender's unit here
        _choose_effect_target(
            ActionTurnContext(engine=eng, actor=RequiredTo.PLAYER_1, verb="choose_effect_target", payload="p1-0")
        )
        unit = eng._game_state.player_1_units[0]
        self.assertEqual(unit.bonus_shield, 2)  # Fortified Position → [Shield 2]
        # The showdown is still open and P1 is the defender, so it counts.
        self.assertEqual(eng.effective_unit_might("player_1", 0), base + 2)

    def test_showdown_open_emits_on_defend_for_the_defender(self):
        # End to end via the move: P2 walks a ready unit onto P1's battlefield,
        # opening a showdown — that must fire P1's defend ability onto the chain.
        gs = GameState(
            started=True, abcd_a_done=True, abcd_b_done=True, abcd_c_done=True, abcd_d_done=True,
            current_player=RequiredTo.PLAYER_2,
            battlefield_1="Fortified Position",
            battlefield_1_controller=RequiredTo.PLAYER_1,
            player_1_units=[PlayedUnit(card="Plundering Poro", location="battlefield_1", uid=1)],
            player_2_units=[PlayedUnit(card="Lonely Poro", location="base", exhausted=False, uid=2)],
        )
        eng = GameEngine(game_state=gs)
        _move_unit(
            ActionTurnContext(engine=eng, actor=RequiredTo.PLAYER_2, verb="move_unit", payload="0:battlefield_1")
        )
        eng._drain_triggers()
        chain = eng._game_state.pending_chain
        self.assertIsNotNone(chain)
        self.assertIn("GIVE_UNIT_SHIELD", chain.items[0].effect.effects)


if __name__ == "__main__":
    unittest.main()
