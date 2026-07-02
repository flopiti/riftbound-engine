"""FIRST_TIME_PLAYER_CHOOSE_FRIENDLY_WITH_SPELL_EACH_TURN — The Dreaming Tree:
"When a player chooses a friendly unit here with a spell for the first time each
turn, they draw 1."

Fires only when the caster chose one of THEIR units at THIS battlefield, and
only the first such spell per player per turn. Gate lives in
GameEngine._trigger_state_ok / _record_trigger_fired."""

from __future__ import annotations

import random
import unittest
from unittest import mock

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine import abilities as A
from riftbound_engine.engine import ChainItem, PlayedUnit
from riftbound_engine.triggers import GameEvent

_TAXONOMY = {
    "The Dreaming Tree": (
        A.Ability(
            triggers=("FIRST_TIME_PLAYER_CHOOSE_FRIENDLY_WITH_SPELL_EACH_TURN",),
            active_effects=("DRAW_1",),
        ),
    ),
}


def _engine() -> GameEngine:
    eng = GameEngine(rng=random.Random(1))
    gs = eng._game_state
    gs.battlefield_1 = "The Dreaming Tree"
    gs.started = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    return eng


def _emit_spell(eng: GameEngine, controller: str, bfs: set[str]) -> bool:
    eng._trigger_queue = []
    with mock.patch.object(A, "triggered_abilities_for", side_effect=lambda n: _TAXONOMY.get(n, ())):
        eng._emit(
            GameEvent(
                kind="ON_PLAY_SPELL",
                controller=controller,
                data={"card": "Some Spell", "chosen_friendly_bfs": bfs},
            )
        )
    return any(
        te.trigger == "FIRST_TIME_PLAYER_CHOOSE_FRIENDLY_WITH_SPELL_EACH_TURN"
        for te in eng._trigger_queue
    )


class FirstChooseFriendlyTests(unittest.TestCase):
    def test_fires_when_caster_chose_a_friendly_unit_here(self) -> None:
        eng = _engine()
        self.assertTrue(_emit_spell(eng, "player_1", {"battlefield_1"}))

    def test_does_not_fire_when_no_friendly_unit_here_chosen(self) -> None:
        eng = _engine()
        # Spell chose a unit elsewhere (or none here).
        self.assertFalse(_emit_spell(eng, "player_1", {"battlefield_2"}))
        self.assertFalse(_emit_spell(eng, "player_1", set()))

    def test_only_first_time_each_turn(self) -> None:
        eng = _engine()
        self.assertTrue(_emit_spell(eng, "player_1", {"battlefield_1"}))  # first
        self.assertFalse(_emit_spell(eng, "player_1", {"battlefield_1"}))  # capped

    def test_cap_is_per_player(self) -> None:
        eng = _engine()
        self.assertTrue(_emit_spell(eng, "player_1", {"battlefield_1"}))
        # P2's first choose-friendly-here this turn still fires.
        self.assertTrue(_emit_spell(eng, "player_2", {"battlefield_1"}))

    def test_cap_resets_next_turn(self) -> None:
        eng = _engine()
        self.assertTrue(_emit_spell(eng, "player_1", {"battlefield_1"}))
        eng._game_state.first_choose_friendly_fired_this_turn = set()  # what _advance_turn does
        self.assertTrue(_emit_spell(eng, "player_1", {"battlefield_1"}))

    def test_beneficiary_is_the_caster(self) -> None:
        eng = _engine()
        eng._trigger_queue = []
        with mock.patch.object(A, "triggered_abilities_for", side_effect=lambda n: _TAXONOMY.get(n, ())):
            eng._emit(
                GameEvent(
                    kind="ON_PLAY_SPELL",
                    controller="player_2",
                    data={"card": "S", "chosen_friendly_bfs": {"battlefield_1"}},
                )
            )
        te = next(t for t in eng._trigger_queue if t.trigger.startswith("FIRST_TIME"))
        self.assertEqual(te.controller, "player_2")


class ChosenFriendlyBattlefieldsHelperTests(unittest.TestCase):
    def test_collects_casters_unit_locations_at_battlefields(self) -> None:
        eng = _engine()
        gs = eng._game_state
        gs.player_1_units = [
            PlayedUnit(card="A", location="battlefield_1", exhausted=False),
            PlayedUnit(card="B", location="base", exhausted=False),
        ]
        gs.player_2_units = [PlayedUnit(card="E", location="battlefield_2", exhausted=False)]
        # Spell (cast by P1) chose P1's unit at bf1, P1's unit at base, and P2's
        # unit at bf2. Only the friendly unit AT a battlefield counts.
        item = ChainItem(actor=RequiredTo.PLAYER_1, card="S", targets=["player_1:0", "player_1:1", "player_2:0"])
        self.assertEqual(eng._chosen_friendly_battlefields(item), {"battlefield_1"})


if __name__ == "__main__":
    unittest.main()
