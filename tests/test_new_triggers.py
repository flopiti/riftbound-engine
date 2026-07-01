"""Wiring tests for the newly-mapped triggers. Each drives the underlying event
through GameEngine._emit and inspects the queued triggered abilities (and the
event-derived target), the same hermetic approach used by
test_first_beginning_phase / test_first_choose_friendly."""

from __future__ import annotations

import random
import unittest
from unittest import mock

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine import abilities as A
from riftbound_engine.engine import PlayedGear, PlayedUnit
from riftbound_engine.triggers import GameEvent


def _ability(trigger: str, *effects: str) -> tuple:
    return (A.Ability(triggers=(trigger,), active_effects=tuple(effects) or ("DRAW_1",)),)


def _engine() -> GameEngine:
    eng = GameEngine(rng=random.Random(1))
    gs = eng._game_state
    gs.started = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    return eng


def _fire(eng: GameEngine, taxonomy: dict, event: GameEvent):
    eng._trigger_queue = []
    with mock.patch.object(A, "triggered_abilities_for", side_effect=lambda n: taxonomy.get(n, ())):
        eng._emit(event)
    return eng._trigger_queue


class NewTriggerWiringTests(unittest.TestCase):
    def test_when_i_hold_fires_for_unit_at_held_bf(self) -> None:
        eng = _engine()
        eng._game_state.player_1_units = [
            PlayedUnit(card="Ahri", location="battlefield_1", exhausted=False),
            PlayedUnit(card="Ahri", location="base", exhausted=False),
        ]
        tax = {"Ahri": _ability("WHEN_I_HOLD", "SCORE_1_POINT")}
        q = _fire(eng, tax, GameEvent(kind="ON_HOLD", controller="player_1", battlefield="battlefield_1"))
        # Only the unit AT the held battlefield fires (HERE scope).
        self.assertEqual([te.source for te in q], ["player_1:0"])

    def test_when_i_attack_or_defend_fires_in_showdown(self) -> None:
        eng = _engine()
        eng._game_state.player_1_units = [PlayedUnit(card="Ahri", location="battlefield_1", exhausted=False)]
        tax = {"Ahri": _ability("WHEN_I_ATTACK_OR_DEFEND", "GIVE_UNIT_HERE_-2M_MIN_1")}
        q = _fire(eng, tax, GameEvent(kind="ON_SHOWDOWN_BEGIN", controller="player_2", battlefield="battlefield_1"))
        self.assertEqual([te.trigger for te in q], ["WHEN_I_ATTACK_OR_DEFEND"])

    def test_when_unit_move_from_here_fires_and_targets_moved_unit(self) -> None:
        eng = _engine()
        eng._game_state.battlefield_1 = "Back-Alley Bar"
        tax = {"Back-Alley Bar": _ability("WHEN_UNIT_MOVE_FROM_HERE", "GIVE_UNIT_+1M")}
        q = _fire(
            eng,
            tax,
            GameEvent(
                kind="ON_MOVE",
                controller="player_1",
                source="player_1:0",
                battlefield=None,
                data={"origin": "battlefield_1", "unit": "player_1:0"},
            ),
        )
        self.assertEqual(len(q), 1)
        # The moved unit is fed to the ability as its target.
        self.assertEqual(q[0].targets, ("player_1:0",))

    def test_when_unit_move_from_here_ignores_other_origin(self) -> None:
        eng = _engine()
        eng._game_state.battlefield_1 = "Back-Alley Bar"
        tax = {"Back-Alley Bar": _ability("WHEN_UNIT_MOVE_FROM_HERE", "GIVE_UNIT_+1M")}
        q = _fire(
            eng, tax,
            GameEvent(kind="ON_MOVE", controller="player_1", source="player_1:0",
                      data={"origin": "battlefield_2", "unit": "player_1:0"}),
        )
        self.assertEqual(q, [])

    def test_when_unit_returned_hand_fires_for_bf_of_origin(self) -> None:
        eng = _engine()
        eng._game_state.battlefield_1 = "Ripper's Bay"
        tax = {"Ripper's Bay": _ability("WHEN_UNIT_RETURNED_HAND", "CHANNEL_1_RUNE_EXHAUSTED")}
        q = _fire(
            eng, tax,
            GameEvent(kind="ON_RETURN_TO_HAND", controller="player_2", data={"origin": "battlefield_1"}),
        )
        self.assertEqual([te.trigger for te in q], ["WHEN_UNIT_RETURNED_HAND"])

    def test_when_you_move_enemy_unit_fires_for_mover_and_targets_unit(self) -> None:
        eng = _engine()
        eng._game_state.player_1_gears = [PlayedGear(card="Blast Cone")]
        tax = {"Blast Cone": _ability("WHEN_YOU_MOVE_ENEMY_UNIT", "STUN_IT")}
        q = _fire(
            eng, tax,
            GameEvent(kind="ON_MOVE_ENEMY", controller="player_1", data={"unit": "player_2:0"}),
        )
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0].controller, "player_1")
        self.assertEqual(q[0].targets, ("player_2:0",))

    def test_choose_or_ready_fires_on_either_event(self) -> None:
        eng = _engine()
        eng._game_state.player_1_units = [PlayedUnit(card="Irelia", location="base", exhausted=False)]
        tax = {"Irelia": _ability("WHEN_YOU_CHOOSE_OR_READY_ME", "GIVE_ME_+2M")}
        for kind in ("ON_CHOOSE", "ON_READY"):
            q = _fire(eng, tax, GameEvent(kind=kind, controller="player_1", source="player_1:0"))
            self.assertEqual([te.trigger for te in q], ["WHEN_YOU_CHOOSE_OR_READY_ME"], kind)


if __name__ == "__main__":
    unittest.main()
