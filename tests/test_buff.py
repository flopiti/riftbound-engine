"""Tests for the [Buff] concept: a distinct, permanent +1-Might marker that
does NOT stack, plus the BUFF_ME effect and Adaptatron's combined
"you may kill a gear; if you do, buff me" choice (KILL_GEAR_BUFF_ME).

Like test_kill_gear, effects are driven directly through
``GameEngine._run_effect_codes`` and answered with ``play:choose_effect_target``.
"""

from __future__ import annotations

import random
import unittest

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine import effects as E
from riftbound_engine.engine import PlayedGear, PlayedUnit


def _engine_with(p1_units, p1_gears=None, p2_gears=None) -> GameEngine:
    eng = GameEngine(rng=random.Random(1))
    gs = eng._game_state
    gs.current_player = RequiredTo.PLAYER_1
    gs.player_1_units = p1_units
    gs.player_1_gears = p1_gears or []
    gs.player_2_gears = p2_gears or []
    gs.started = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    return eng


def _run(eng, codes, source="player_1:0", actor=RequiredTo.PLAYER_1):
    eng._run_effect_codes(
        controller=actor, source=source, trigger="TEST",
        event_kind="TEST", label="buff", codes=codes,
    )


# Adaptatron is a printed-3-Might Calm unit in the CSV.
ADAPTATRON = "Adaptatron"


class BuffConceptTests(unittest.TestCase):
    def test_codes_reported_implemented(self) -> None:
        self.assertIn("BUFF_ME", E.registered_effect_codes())
        self.assertTrue(E.is_choice_effect("KILL_GEAR_BUFF_ME"))
        self.assertTrue(E.is_implemented("KILL_GEAR_BUFF_ME"))

    def test_buff_me_sets_flag_and_grants_plus_one(self) -> None:
        eng = _engine_with([PlayedUnit(card=ADAPTATRON, location="battlefield_1")])
        base = eng.effective_unit_might("player_1", 0)
        _run(eng, ["BUFF_ME"])
        unit = eng._game_state.player_1_units[0]
        self.assertTrue(unit.buffed)
        self.assertEqual(eng.effective_unit_might("player_1", 0), base + 1)

    def test_buff_does_not_stack(self) -> None:
        eng = _engine_with([PlayedUnit(card=ADAPTATRON, location="battlefield_1")])
        base = eng.effective_unit_might("player_1", 0)
        _run(eng, ["BUFF_ME"])
        _run(eng, ["BUFF_ME"])  # second buff is a no-op
        self.assertTrue(eng._game_state.player_1_units[0].buffed)
        self.assertEqual(eng.effective_unit_might("player_1", 0), base + 1)

    def test_buff_persists_across_end_of_turn(self) -> None:
        # A buff is permanent — unlike bonus_might it survives turn cleanup.
        eng = _engine_with([PlayedUnit(card=ADAPTATRON, location="battlefield_1")])
        _run(eng, ["BUFF_ME"])
        unit = eng._game_state.player_1_units[0]
        unit.bonus_might = 2  # this-turn modifier that SHOULD be wiped
        eng._advance_turn()
        self.assertTrue(unit.buffed)          # buff stays
        self.assertEqual(unit.bonus_might, 0)  # transient bump cleared


class KillGearBuffMeTests(unittest.TestCase):
    def test_kill_gear_then_buff(self) -> None:
        eng = _engine_with(
            [PlayedUnit(card=ADAPTATRON, location="battlefield_1")],
            p2_gears=[PlayedGear(card="Quickblade")],
        )
        _run(eng, ["KILL_GEAR_BUFF_ME"])
        self.assertIsNotNone(eng._game_state.pending_effect_choice)
        eng.apply_action("play:choose_effect_target:g2-0", RequiredTo.PLAYER_1)
        self.assertEqual(eng._game_state.player_2_gears, [])  # gear killed
        self.assertTrue(eng._game_state.player_1_units[0].buffed)  # ...so buffed

    def test_pass_does_not_buff(self) -> None:
        # Declining the optional kill ("you MAY") means NO buff ("if you do").
        eng = _engine_with(
            [PlayedUnit(card=ADAPTATRON, location="battlefield_1")],
            p1_gears=[PlayedGear(card="Quickblade")],
        )
        _run(eng, ["KILL_GEAR_BUFF_ME"])
        eng.apply_action("play:choose_effect_target:pass", RequiredTo.PLAYER_1)
        self.assertEqual(len(eng._game_state.player_1_gears), 1)  # gear survives
        self.assertFalse(eng._game_state.player_1_units[0].buffed)

    def test_no_gears_fizzles_no_buff(self) -> None:
        eng = _engine_with([PlayedUnit(card=ADAPTATRON, location="battlefield_1")])
        _run(eng, ["KILL_GEAR_BUFF_ME"])
        self.assertIsNone(eng._game_state.pending_effect_choice)  # didn't pause
        self.assertFalse(eng._game_state.player_1_units[0].buffed)  # no buff


if __name__ == "__main__":
    unittest.main()
