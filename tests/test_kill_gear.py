"""Tests for the YOU_MAY_KILL_GEAR choice effect: an optional pick over every
gear on the board (either player's). Picking removes the gear — a real card to
its owner's trash, a token out of existence; passing leaves the board intact;
no gears in play fizzles without pausing.

The effect is driven directly through ``GameEngine._run_effect_codes`` (no card
needs to author it yet), then answered with ``play:choose_effect_target``."""

from __future__ import annotations

import random
import unittest

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine import effects as E
from riftbound_engine.engine import PlayedGear


def _engine_with_gears(
    p1_gears: list[PlayedGear], p2_gears: list[PlayedGear] | None = None
) -> GameEngine:
    """Minimal mid-game engine: P1 active, setup flags forced past ABCD so
    ``play:`` actions dispatch, with the given gears on each side."""
    eng = GameEngine(rng=random.Random(1))
    gs = eng._game_state
    gs.current_player = RequiredTo.PLAYER_1
    gs.player_1_gears = p1_gears
    gs.player_2_gears = p2_gears or []
    gs.started = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    return eng


def _open_kill_gear(eng: GameEngine, actor: RequiredTo = RequiredTo.PLAYER_1) -> None:
    eng._run_effect_codes(
        controller=actor,
        source=None,
        trigger="TEST",
        event_kind="TEST",
        label="kill gear",
        codes=["YOU_MAY_KILL_GEAR"],
    )


class KillGearTests(unittest.TestCase):
    def test_reported_as_implemented(self) -> None:
        # A choice effect is dispatched via is_choice_effect, but it's still a
        # faithful handler — so is_implemented / registered_effect_codes (the
        # single source of truth the planner mirror tracks) must include it.
        self.assertTrue(E.is_choice_effect("YOU_MAY_KILL_GEAR"))
        self.assertTrue(E.is_implemented("YOU_MAY_KILL_GEAR"))
        self.assertIn("YOU_MAY_KILL_GEAR", E.registered_effect_codes())

    def test_no_gears_fizzles_without_pausing(self) -> None:
        eng = _engine_with_gears([])
        _open_kill_gear(eng)
        self.assertIsNone(eng._game_state.pending_effect_choice)

    def test_options_enumerate_both_boards(self) -> None:
        eng = _engine_with_gears(
            [PlayedGear(card="Blighted Battleaxe"), PlayedGear(card="Gold", token=True)],
            [PlayedGear(card="Quickblade")],
        )
        _open_kill_gear(eng)
        choice = eng._game_state.pending_effect_choice
        self.assertIsNotNone(choice)
        self.assertEqual(choice.actor, RequiredTo.PLAYER_1)
        self.assertEqual(choice.options, ["g1-0", "g1-1", "g2-0"])
        self.assertTrue(choice.optional)

    def test_single_gear_still_asks_because_may(self) -> None:
        eng = _engine_with_gears([PlayedGear(card="Quickblade")])
        _open_kill_gear(eng)
        self.assertIsNotNone(eng._game_state.pending_effect_choice)

    def test_kill_real_gear_goes_to_owner_trash(self) -> None:
        eng = _engine_with_gears(
            [PlayedGear(card="Blighted Battleaxe"), PlayedGear(card="Quickblade")]
        )
        _open_kill_gear(eng)
        eng.apply_action("play:choose_effect_target:g1-0", RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertEqual([g.card for g in gs.player_1_gears], ["Quickblade"])
        self.assertEqual(gs.player_1_trash, ["Blighted Battleaxe"])
        self.assertIsNone(gs.pending_effect_choice)

    def test_kill_token_ceases_to_exist(self) -> None:
        eng = _engine_with_gears([PlayedGear(card="Gold", token=True)])
        _open_kill_gear(eng)
        eng.apply_action("play:choose_effect_target:g1-0", RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertEqual(gs.player_1_gears, [])
        self.assertEqual(gs.player_1_trash, [])  # token doesn't go to trash
        self.assertIsNone(gs.pending_effect_choice)

    def test_can_kill_opponents_gear(self) -> None:
        eng = _engine_with_gears([], [PlayedGear(card="Quickblade")])
        _open_kill_gear(eng)
        eng.apply_action("play:choose_effect_target:g2-0", RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertEqual(gs.player_2_gears, [])
        self.assertEqual(gs.player_2_trash, ["Quickblade"])

    def test_pass_leaves_board_intact(self) -> None:
        eng = _engine_with_gears([PlayedGear(card="Quickblade")])
        _open_kill_gear(eng)
        eng.apply_action("play:choose_effect_target:pass", RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertEqual([g.card for g in gs.player_1_gears], ["Quickblade"])
        self.assertEqual(gs.player_1_trash, [])
        self.assertIsNone(gs.pending_effect_choice)

    def test_bad_actor_and_token_rejected_state_intact(self) -> None:
        eng = _engine_with_gears([PlayedGear(card="Quickblade")])
        _open_kill_gear(eng)
        with self.assertRaises(ValueError):
            eng.apply_action("play:choose_effect_target:g1-0", RequiredTo.PLAYER_2)
        with self.assertRaises(ValueError):
            eng.apply_action("play:choose_effect_target:g2-0", RequiredTo.PLAYER_1)
        self.assertIsNotNone(eng._game_state.pending_effect_choice)


if __name__ == "__main__":
    unittest.main()
