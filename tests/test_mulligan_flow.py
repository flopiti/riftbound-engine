"""Tests for the one-card-at-a-time, SEQUENTIAL mulligan.

Each player mulligans one card at a time (the four cards + a No-mulligan
option), capped at two; the chosen cards go UNDER the library and are
replaced. Player 1 fully resolves before player 2 is prompted.
"""

from __future__ import annotations

import unittest

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine.protocol import RequiredStep


def _drive_to_mulligan() -> tuple[GameEngine, object]:
    rolls = iter([6, 2])
    eng = GameEngine(dice_roller=lambda: next(rolls))
    first = eng.start()
    second = eng.apply_action(f"choose_deck:{first.player_1_options[0]}", RequiredTo.PLAYER_1)
    eng.apply_action(f"choose_deck:{second.player_2_options[0]}", RequiredTo.PLAYER_2)
    fourth = eng.apply_action("choose_first_turn:player_1", RequiredTo.PLAYER_1)
    fifth = eng.apply_action(
        f"choose_battlefield_1:{fourth.player_1_options[0]}", RequiredTo.PLAYER_1
    )
    out = eng.apply_action(f"choose_battlefield_2:{fifth.player_2_options[0]}", RequiredTo.PLAYER_2)
    return eng, out


class MulliganFlowTests(unittest.TestCase):
    def test_one_card_at_a_time_and_sequential(self) -> None:
        eng, out = _drive_to_mulligan()
        # Only player_1 is prompted first (sequential): 4 cards + No-mulligan.
        self.assertEqual(out.required_action.name, RequiredStep.CHOOSE_MULLIGAN)
        self.assertEqual(out.required_action.actor, RequiredTo.PLAYER_1)
        self.assertEqual(
            out.player_1_options,
            [
                "mulligan_bottom:player_1:0",
                "mulligan_bottom:player_1:1",
                "mulligan_bottom:player_1:2",
                "mulligan_bottom:player_1:3",
                "mulligan_done:player_1",
            ],
        )
        self.assertEqual(out.player_2_options, [])
        # After one mulligan the chosen card drops out; 3 remain + No-mulligan.
        o2 = eng.apply_action("mulligan_bottom:player_1:1", RequiredTo.PLAYER_1)
        self.assertEqual(
            o2.player_1_options,
            [
                "mulligan_bottom:player_1:0",
                "mulligan_bottom:player_1:2",
                "mulligan_bottom:player_1:3",
                "mulligan_done:player_1",
            ],
        )
        self.assertEqual(o2.player_2_options, [])

    def test_cap_two_auto_resolves_then_player_2(self) -> None:
        eng, _ = _drive_to_mulligan()
        mh = list(eng._game_state.player_1_mulligan_hand)
        eng.apply_action("mulligan_bottom:player_1:1", RequiredTo.PLAYER_1)
        out = eng.apply_action("mulligan_bottom:player_1:2", RequiredTo.PLAYER_1)
        # Cap (2) reached → player_1 auto-finalizes; player_2 is now prompted.
        self.assertTrue(eng._game_state.mulligan_player_1_resolved)
        self.assertEqual(out.required_action.actor, RequiredTo.PLAYER_2)
        self.assertEqual(out.player_1_options, [])
        self.assertEqual(len(out.player_2_options), 5)
        # Kept the unbottomed cards (0, 3); bottomed ones (1, 2) went UNDER.
        hand = eng._game_state.player_1_hand
        self.assertEqual(len(hand), 4)
        self.assertEqual(hand[0], mh[0])
        self.assertEqual(hand[1], mh[3])
        self.assertEqual(eng._game_state.player_1_library[-2:], [mh[1], mh[2]])

    def test_third_bottom_rejected_after_cap(self) -> None:
        eng, _ = _drive_to_mulligan()
        eng.apply_action("mulligan_bottom:player_1:0", RequiredTo.PLAYER_1)
        eng.apply_action("mulligan_bottom:player_1:1", RequiredTo.PLAYER_1)  # cap → resolved
        with self.assertRaises(ValueError):
            eng.apply_action("mulligan_bottom:player_1:2", RequiredTo.PLAYER_1)

    def test_no_mulligan_keeps_all_then_finishes_setup(self) -> None:
        eng, _ = _drive_to_mulligan()
        mh1 = list(eng._game_state.player_1_mulligan_hand)
        eng.apply_action("mulligan_done:player_1", RequiredTo.PLAYER_1)
        self.assertTrue(eng._game_state.mulligan_player_1_resolved)
        self.assertEqual(eng._game_state.player_1_hand, mh1)  # kept all four
        eng.apply_action("mulligan_done:player_2", RequiredTo.PLAYER_2)
        self.assertTrue(eng._game_state.is_mulligan_done)
        self.assertTrue(eng._game_state.started)

    def test_player_2_cannot_act_before_player_1(self) -> None:
        eng, _ = _drive_to_mulligan()
        with self.assertRaises(ValueError):
            eng.apply_action("mulligan_done:player_2", RequiredTo.PLAYER_2)
        with self.assertRaises(ValueError):
            eng.apply_action("mulligan_bottom:player_2:0", RequiredTo.PLAYER_2)


if __name__ == "__main__":
    unittest.main()
