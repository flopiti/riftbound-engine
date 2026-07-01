"""OPPONENT_DISCARD_1 — Bewitching Spirit: "choose a player. They discard 1."

Faithful two-step: the caster picks WHICH player (a forced choice over both
players), then THAT player picks WHICH card to discard (a forced discard owned
by them, not the caster). Driven through _run_effect_codes, answered via
play:choose_effect_target."""

from __future__ import annotations

import random
import unittest

from riftbound_engine import GameEngine, RequiredTo


def _engine(p1_hand: list[str], p2_hand: list[str]) -> GameEngine:
    eng = GameEngine(rng=random.Random(1))
    gs = eng._game_state
    gs.current_player = RequiredTo.PLAYER_1
    gs.player_1_hand = list(p1_hand)
    gs.player_2_hand = list(p2_hand)
    gs.started = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    return eng


def _cast(eng: GameEngine) -> None:
    eng._run_effect_codes(
        controller=RequiredTo.PLAYER_1,
        source=None,
        trigger="TEST",
        event_kind="TEST",
        label="Bewitching Spirit",
        codes=["OPPONENT_DISCARD_1"],
    )


class OpponentDiscardTests(unittest.TestCase):
    def test_first_choice_is_the_caster_picking_a_player(self) -> None:
        eng = _engine(["A", "B"], ["X", "Y"])
        _cast(eng)
        ch = eng._game_state.pending_effect_choice
        self.assertIsNotNone(ch)
        self.assertEqual(ch.actor, RequiredTo.PLAYER_1)
        self.assertEqual(ch.options, ["player_1", "player_2"])
        self.assertFalse(ch.optional)  # must choose a player

    def test_chosen_opponent_then_picks_their_own_card(self) -> None:
        eng = _engine(["A", "B"], ["X", "Y"])
        _cast(eng)
        # Caster picks the opponent.
        eng.apply_action("play:choose_effect_target:player_2", RequiredTo.PLAYER_1)
        ch = eng._game_state.pending_effect_choice
        self.assertIsNotNone(ch)
        self.assertEqual(ch.actor, RequiredTo.PLAYER_2)  # THEY choose, not caster
        self.assertEqual(ch.code, "DISCARD_1")
        self.assertEqual(ch.options, ["h-0", "h-1"])
        # The opponent discards their second card.
        eng.apply_action("play:choose_effect_target:h-1", RequiredTo.PLAYER_2)
        gs = eng._game_state
        self.assertEqual(gs.player_2_hand, ["X"])
        self.assertEqual(gs.player_2_trash, ["Y"])
        self.assertEqual(gs.player_1_hand, ["A", "B"])  # caster untouched
        self.assertIsNone(gs.pending_effect_choice)

    def test_caster_may_choose_themselves(self) -> None:
        eng = _engine(["A", "B"], ["X"])
        _cast(eng)
        eng.apply_action("play:choose_effect_target:player_1", RequiredTo.PLAYER_1)
        ch = eng._game_state.pending_effect_choice
        self.assertEqual(ch.actor, RequiredTo.PLAYER_1)
        eng.apply_action("play:choose_effect_target:h-0", RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertEqual(gs.player_1_hand, ["B"])
        self.assertEqual(gs.player_1_trash, ["A"])

    def test_chosen_player_with_empty_hand_discards_nothing(self) -> None:
        eng = _engine(["A"], [])
        _cast(eng)
        eng.apply_action("play:choose_effect_target:player_2", RequiredTo.PLAYER_1)
        gs = eng._game_state
        # No second choice — nothing to discard.
        self.assertIsNone(gs.pending_effect_choice)
        self.assertEqual(gs.player_2_trash, [])

    def test_only_the_chosen_player_may_answer_the_discard(self) -> None:
        eng = _engine(["A"], ["X", "Y"])
        _cast(eng)
        eng.apply_action("play:choose_effect_target:player_2", RequiredTo.PLAYER_1)
        # Caster can't make the discard choice for the opponent.
        with self.assertRaises(ValueError):
            eng.apply_action("play:choose_effect_target:h-0", RequiredTo.PLAYER_1)
        self.assertIsNotNone(eng._game_state.pending_effect_choice)


if __name__ == "__main__":
    unittest.main()
