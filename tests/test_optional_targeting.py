"""Tests for OPTIONAL ("up to N", min-0) spell targeting.

A min-0 unit requirement (e.g. Bellows Breath's "Deal 1 to UP TO THREE units
at the same location") now opens the target picker with a "No targets" option,
instead of auto-resolving with no prompt.
"""

from __future__ import annotations

import unittest

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine.engine import PlayedUnit
from riftbound_engine.requirements import spell_target_plan


def _drive_to_action_turn() -> GameEngine:
    rolls = iter([6, 2])
    eng = GameEngine(dice_roller=lambda: next(rolls))
    first = eng.start()
    second = eng.apply_action(f"choose_deck:{first.player_1_options[0]}", RequiredTo.PLAYER_1)
    eng.apply_action(f"choose_deck:{second.player_2_options[0]}", RequiredTo.PLAYER_2)
    fourth = eng.apply_action("choose_first_turn:player_1", RequiredTo.PLAYER_1)
    fifth = eng.apply_action(
        f"choose_battlefield_1:{fourth.player_1_options[0]}", RequiredTo.PLAYER_1
    )
    eng.apply_action(f"choose_battlefield_2:{fifth.player_2_options[0]}", RequiredTo.PLAYER_2)
    eng.apply_action("mulligan_done:player_1", RequiredTo.PLAYER_1)
    eng.apply_action("mulligan_done:player_2", RequiredTo.PLAYER_2)
    return eng


class OptionalTargetPlanTests(unittest.TestCase):
    def test_min0_phrase_is_in_the_plan(self) -> None:
        # Previously min-0 phrases were dropped (so the spell never prompted).
        plan = spell_target_plan("ANY UNIT (0-3)[SAME_LOC]")
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].min_count, 0)
        self.assertEqual(plan[0].max_count, 3)


class OptionalTargetCastTests(unittest.TestCase):
    def _setup(self, units: list[PlayedUnit]) -> tuple[GameEngine, object]:
        eng = _drive_to_action_turn()
        eng.start()
        gs = eng._game_state
        gs.player_1_hand = ["Bellows Breath"]  # ANY UNIT (0-3)[SAME_LOC], costs 1E + 1 Mind
        eng.add_energy(RequiredTo.PLAYER_1, 10)
        gs.player_1_power = {"Mind": 9}
        gs.player_1_units = units
        return eng, gs

    def test_offers_no_targets_plus_units(self) -> None:
        eng, gs = self._setup(
            [PlayedUnit(card="Ravenbloom Student", location="base", exhausted=False)]
        )
        eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
        # min-0 now PARKS for selection (it used to auto-resolve with no prompt).
        self.assertIsNotNone(gs.pending_spell_choice)
        opts = eng.start().player_1_options
        self.assertIn("play:choose_spell_targets:", opts)  # the "No targets" / skip option
        self.assertIn("play:choose_spell_targets:p1-0", opts)  # the unit is targetable

    def test_skip_finalizes_selection(self) -> None:
        eng, gs = self._setup(
            [PlayedUnit(card="Ravenbloom Student", location="base", exhausted=False)]
        )
        eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
        eng.apply_action("play:choose_spell_targets:", RequiredTo.PLAYER_1)  # choose NO targets
        self.assertIsNone(gs.pending_spell_choice)  # selection finalized
        # Bellows Breath has [Repeat], so finalizing opens the repeat decision.
        self.assertIsNotNone(gs.pending_spell_repeat)

    def test_no_units_auto_skips_targeting(self) -> None:
        # With no units on the board there's nothing to optionally target, so it
        # doesn't prompt — it proceeds straight to the [Repeat] decision.
        eng, gs = self._setup([])
        eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
        self.assertIsNone(gs.pending_spell_choice)
        self.assertIsNotNone(gs.pending_spell_repeat)


if __name__ == "__main__":
    unittest.main()
