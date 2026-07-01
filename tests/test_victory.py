"""Tests for the Victory concept: reaching the Victory Score to win, and the
Winning Point rule (§466.1.b) governing how the final point is gained.

Points flow through GameEngine.add_score (effect sources) and award_bf_point
(Conquer/Hold). Win detection (§467/§323.1) fires from add_score; the
Winning-Point restriction lives in award_bf_point's point resolution.
"""

from __future__ import annotations

import random
import unittest

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine.engine import VICTORY_SCORE

P1 = RequiredTo.PLAYER_1
P2 = RequiredTo.PLAYER_2


def _engine() -> GameEngine:
    eng = GameEngine(rng=random.Random(1))
    gs = eng._game_state
    gs.current_player = P1
    gs.started = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    return eng


class VictoryDetectionTests(unittest.TestCase):
    def test_no_winner_below_victory_score(self) -> None:
        eng = _engine()
        eng.add_score(P1, VICTORY_SCORE - 1)
        self.assertIsNone(eng._game_state.winner)

    def test_reaching_victory_score_wins(self) -> None:
        eng = _engine()
        eng.add_score(P1, VICTORY_SCORE)
        self.assertEqual(eng._game_state.winner, P1.value)

    def test_tie_at_victory_score_is_not_a_win(self) -> None:
        # §467: need strictly MORE points than the opponent, not just >= score.
        eng = _engine()
        eng.add_score(P1, VICTORY_SCORE)
        eng._game_state.winner = None  # reset so we can test the tie in isolation
        eng._game_state.player_1_score = VICTORY_SCORE
        eng._game_state.player_2_score = VICTORY_SCORE
        eng._check_victory()
        self.assertIsNone(eng._game_state.winner)

    def test_winner_is_sticky(self) -> None:
        eng = _engine()
        eng.add_score(P1, VICTORY_SCORE)
        # Opponent later overtakes — the recorded winner does not change.
        eng.add_score(P2, VICTORY_SCORE + 5)
        self.assertEqual(eng._game_state.winner, P1.value)


class WinningPointTests(unittest.TestCase):
    def test_hold_gains_the_winning_point(self) -> None:
        # §466.1.b.1: the final point via Hold is always gained.
        eng = _engine()
        eng._game_state.player_1_score = VICTORY_SCORE - 1
        gained = eng.award_bf_point(P1, "battlefield_1", via="hold")
        self.assertTrue(gained)
        self.assertEqual(eng._game_state.player_1_score, VICTORY_SCORE)
        self.assertEqual(eng._game_state.winner, P1.value)

    def test_conquer_winning_point_needs_every_battlefield(self) -> None:
        # §466.1.b.2: only one battlefield scored → no winning point, draw instead.
        eng = _engine()
        eng._game_state.player_1_library = ["Some Card"]
        eng._game_state.player_1_hand = []
        before = len(eng._game_state.player_1_hand)
        eng._game_state.player_1_score = VICTORY_SCORE - 1
        gained = eng.award_bf_point(P1, "battlefield_1", via="conquer")
        self.assertFalse(gained)  # point withheld
        self.assertEqual(eng._game_state.player_1_score, VICTORY_SCORE - 1)
        self.assertIsNone(eng._game_state.winner)
        after = len(eng._game_state.player_1_hand or [])
        self.assertEqual(after, before + 1)  # drew a card instead

    def test_conquer_winning_point_with_all_battlefields_wins(self) -> None:
        # Scored the other battlefield earlier this turn, now Conquer the last.
        eng = _engine()
        eng._game_state.player_1_score = VICTORY_SCORE - 1
        eng._game_state.scored_bfs_this_turn = {"battlefield_2"}
        gained = eng.award_bf_point(P1, "battlefield_1", via="conquer")
        self.assertTrue(gained)
        self.assertEqual(eng._game_state.player_1_score, VICTORY_SCORE)
        self.assertEqual(eng._game_state.winner, P1.value)

    def test_non_final_conquer_is_unrestricted(self) -> None:
        # Far from victory: a single-battlefield Conquer just gains a point.
        eng = _engine()
        gained = eng.award_bf_point(P1, "battlefield_1", via="conquer")
        self.assertTrue(gained)
        self.assertEqual(eng._game_state.player_1_score, 1)


if __name__ == "__main__":
    unittest.main()
