"""AT_START_EACH_FIRST_BEGGINNING_PHASE — Obelisk of Power / The Arena's
Greatest: "At the start of each player's FIRST Beginning Phase, that player
channels 1 rune / gains 1 point."

It must fire for EITHER player (scope ANY), but only on that player's first turn
(turn_number == 1), regardless of who holds the battlefield. The gate lives in
GameEngine._trigger_state_ok; here we drive TURN_START directly and inspect the
queued triggers."""

from __future__ import annotations

import random
import unittest
from unittest import mock

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine import abilities as A
from riftbound_engine.triggers import GameEvent

_TAXONOMY = {
    "Obelisk of Power": (
        A.Ability(
            triggers=("AT_START_EACH_FIRST_BEGGINNING_PHASE",),
            active_effects=("CHANNEL_1_RUNE",),
        ),
    ),
}


def _engine(p1_turn: int, p2_turn: int, holder: RequiredTo | None) -> GameEngine:
    eng = GameEngine(rng=random.Random(1))
    gs = eng._game_state
    gs.battlefield_1 = "Obelisk of Power"
    gs.battlefield_1_controller = holder
    gs.player_1_turn_number = p1_turn
    gs.player_2_turn_number = p2_turn
    gs.started = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    return eng


def _fired_for(eng: GameEngine, controller: str) -> bool:
    eng._trigger_queue = []
    with mock.patch.object(A, "triggered_abilities_for", side_effect=lambda n: _TAXONOMY.get(n, ())):
        eng._emit(GameEvent(kind="TURN_START", controller=controller))
    return any(te.trigger == "AT_START_EACH_FIRST_BEGGINNING_PHASE" for te in eng._trigger_queue)


class FirstBeginningPhaseTests(unittest.TestCase):
    def test_fires_on_each_players_first_turn(self) -> None:
        # P1's first beginning phase.
        self.assertTrue(_fired_for(_engine(1, 0, RequiredTo.PLAYER_1), "player_1"))
        # P2's first beginning phase (even though P1 holds the battlefield).
        self.assertTrue(_fired_for(_engine(1, 1, RequiredTo.PLAYER_1), "player_2"))

    def test_does_not_fire_on_later_turns(self) -> None:
        self.assertFalse(_fired_for(_engine(2, 1, RequiredTo.PLAYER_1), "player_1"))
        self.assertFalse(_fired_for(_engine(5, 4, RequiredTo.PLAYER_2), "player_2"))

    def test_beneficiary_is_the_player_whose_phase_it_is(self) -> None:
        eng = _engine(1, 0, RequiredTo.PLAYER_1)
        eng._trigger_queue = []
        with mock.patch.object(A, "triggered_abilities_for", side_effect=lambda n: _TAXONOMY.get(n, ())):
            eng._emit(GameEvent(kind="TURN_START", controller="player_1"))
        te = next(t for t in eng._trigger_queue if t.trigger == "AT_START_EACH_FIRST_BEGGINNING_PHASE")
        self.assertEqual(te.controller, "player_1")


if __name__ == "__main__":
    unittest.main()
