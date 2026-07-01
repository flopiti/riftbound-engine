"""End-to-end checks that the SCORING chain is fully wired into real play, and
that the Victory win-condition fires through it.

These drive the public ``apply_action`` flow (move_unit + pass_showdown) rather
than calling ``award_bf_point`` directly, so they cover links 1-3 together:
moving onto a battlefield opens a showdown; resolving it Establishes Control
(sets ``battlefield_X_controller``) and Scores a Conquer; the point flows
through ``add_score`` → ``_check_victory``.
"""

from __future__ import annotations

import random
import unittest

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine.engine import VICTORY_SCORE, PlayedUnit

P1 = RequiredTo.PLAYER_1
P2 = RequiredTo.PLAYER_2


def _engine_midgame() -> GameEngine:
    eng = GameEngine(rng=random.Random(1))
    gs = eng._game_state
    gs.current_player = P1
    gs.started = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    return eng


class ConquerChainTests(unittest.TestCase):
    def test_uncontested_move_in_takes_control_and_scores(self) -> None:
        eng = _engine_midgame()
        gs = eng._game_state
        gs.player_1_units = [
            PlayedUnit(card="Garen", location="base", exhausted=False, uid=1)
        ]
        eng.apply_action("play:move_unit:0:battlefield_1", P1)  # opens showdown
        self.assertIsNotNone(gs.pending_showdown)
        eng.apply_action("play:pass_showdown", P1)
        eng.apply_action("play:pass_showdown", P2)
        # Control established + Conquer point scored, showdown closed.
        self.assertEqual(gs.battlefield_1_controller, P1)
        self.assertEqual(gs.player_1_score, 1)
        self.assertIsNone(gs.pending_showdown)


class VictoryThroughPlayTests(unittest.TestCase):
    def test_conquer_to_victory_wins_via_play(self) -> None:
        # One point from victory; conquering the LAST unscored battlefield this
        # turn satisfies the Winning Point rule, so the Conquer wins the game.
        eng = _engine_midgame()
        gs = eng._game_state
        gs.player_1_score = VICTORY_SCORE - 1
        gs.scored_bfs_this_turn = {"battlefield_2"}  # already scored the other BF
        gs.player_1_units = [
            PlayedUnit(card="Garen", location="base", exhausted=False, uid=1)
        ]
        eng.apply_action("play:move_unit:0:battlefield_1", P1)
        eng.apply_action("play:pass_showdown", P1)
        eng.apply_action("play:pass_showdown", P2)
        self.assertEqual(gs.player_1_score, VICTORY_SCORE)
        self.assertEqual(gs.winner, P1.value)

    def test_single_bf_conquer_at_winning_point_draws_instead(self) -> None:
        # §466.1.b.2 enforced through real play: one from victory, conquering a
        # SINGLE battlefield (not every battlefield this turn) withholds the
        # Winning Point — the player draws a card and does NOT win. Control is
        # still established (a Score occurred).
        eng = _engine_midgame()
        gs = eng._game_state
        gs.player_1_score = VICTORY_SCORE - 1
        gs.scored_bfs_this_turn = set()  # no battlefield scored yet this turn
        gs.player_1_library = ["Card A", "Card B"]
        gs.player_1_hand = []
        gs.player_1_units = [
            PlayedUnit(card="Garen", location="base", exhausted=False, uid=1)
        ]
        eng.apply_action("play:move_unit:0:battlefield_1", P1)
        eng.apply_action("play:pass_showdown", P1)
        eng.apply_action("play:pass_showdown", P2)
        self.assertEqual(gs.battlefield_1_controller, P1)  # control still taken
        self.assertEqual(gs.player_1_score, VICTORY_SCORE - 1)  # point withheld
        self.assertEqual(len(gs.player_1_hand), 1)  # drew a card instead
        self.assertIsNone(gs.winner)  # no win


if __name__ == "__main__":
    unittest.main()
