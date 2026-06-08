"""Tests for the brute-force branch search (riftbound_engine.search)."""

from __future__ import annotations

import copy
import unittest

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine.search import (
    bfs_search,
    evaluate_predicate,
    goal_distance,
    open_play_sides,
    state_view,
)


class ZoneClauseTests(unittest.TestCase):
    """`units` clauses can target non-board zones (hand / library / trash)."""

    def _view(self, **over) -> dict:
        base = {
            "player_1_units": [],
            "player_2_units": [],
            "player_1_hand": [],
            "player_2_hand": [],
            "player_1_library": [],
            "player_2_library": [],
            "player_1_trash": [],
            "player_2_trash": [],
        }
        base.update(over)
        return base

    def test_card_in_hand_matches(self) -> None:
        v = self._view(player_1_hand=["Thousand-Tailed Watcher", "Gust"])
        clause = {"units": {"controller": "player_1", "at": "hand", "name": "thousand tailed watcher"}}
        self.assertTrue(evaluate_predicate(clause, v))
        self.assertEqual(goal_distance(clause, v), 0.0)

    def test_card_not_in_hand_fails(self) -> None:
        v = self._view(player_1_hand=["Gust"], player_2_hand=["Thousand-Tailed Watcher"])
        clause = {"units": {"controller": "player_1", "at": "hand", "name": "thousand tailed watcher"}}
        self.assertFalse(evaluate_predicate(clause, v))

    def test_hand_goal_drawable_from_library_is_close(self) -> None:
        # In library at depth 1 → "end a turn to draw it" → distance 1.0 + 0.05.
        v = self._view(player_1_library=["A", "Thousand-Tailed Watcher"])
        clause = {"units": {"controller": "player_1", "at": "hand", "name": "thousand tailed watcher"}}
        self.assertAlmostEqual(goal_distance(clause, v), 1.05)

    def test_hand_goal_in_play_is_far(self) -> None:
        # Already on the board (needs a bounce back to hand) → distance 3.0,
        # so the search won't "helpfully" play a card the goal wants kept.
        v = self._view(
            player_1_units=[{"card": "Thousand-Tailed Watcher", "location": "base", "exhausted": True}]
        )
        clause = {"units": {"controller": "player_1", "at": "hand", "name": "thousand tailed watcher"}}
        self.assertEqual(goal_distance(clause, v), 3.0)

    def test_trash_zone_match(self) -> None:
        v = self._view(player_2_trash=["Gust"])
        self.assertTrue(evaluate_predicate({"units": {"controller": "player_2", "at": "trash", "name": "gust"}}, v))


class OpenPlaySidesTests(unittest.TestCase):
    """A nameless, board-targeting unit goal must keep that side's unit
    plays/moves un-pruned, or it's unreachable."""

    def test_nameless_opponent_board_goal_opens_that_side(self) -> None:
        goal = {
            "all": [
                {"units": {"controller": "player_1", "at": "hand", "name": "thousand tailed watcher"}},
                {"units": {"controller": "player_2", "at": "battlefield", "min": 2}},
            ]
        }
        self.assertEqual(open_play_sides(goal), {"player_2"})

    def test_named_only_goal_opens_no_side(self) -> None:
        # Every unit clause names a card → name-pruning is enough; no side opened.
        goal = {"units": {"controller": "player_1", "at": "battlefield", "name": "scuttle"}}
        self.assertEqual(open_play_sides(goal), set())

    def test_nameless_no_controller_opens_both(self) -> None:
        self.assertEqual(open_play_sides({"units": {"at": "battlefield", "min": 1}}), {"player_1", "player_2"})

    def test_nonboard_nameless_clause_does_not_open(self) -> None:
        # "2 cards in P2's hand" doesn't require playing units onto the board.
        self.assertEqual(open_play_sides({"units": {"controller": "player_2", "at": "hand", "min": 2}}), set())


def _drive_to_action_turn() -> GameEngine:
    rolls = iter([6, 2])
    eng = GameEngine(dice_roller=lambda: next(rolls))
    f = eng.start()
    eng.apply_action(action=f"choose_deck:{f.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
    s = eng.start()
    eng.apply_action(action=f"choose_deck:{s.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
    fo = eng.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)
    fi = eng.apply_action(action=f"choose_battlefield_1:{fo.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
    eng.apply_action(action=f"choose_battlefield_2:{fi.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
    eng.apply_action(action="mulligan_resolve:player_1:", actor=RequiredTo.PLAYER_1)
    eng.apply_action(action="mulligan_resolve:player_2:", actor=RequiredTo.PLAYER_2)
    eng.start()
    return eng


class PredicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.view = {
            "current_player": "player_1",
            "player_1_score": 2,
            "player_1_units": [{"card": "Scuttle Crab", "location": "battlefield_1", "exhausted": False}],
            "player_2_units": [],
        }

    def test_field_comparisons(self) -> None:
        self.assertTrue(evaluate_predicate({"field": "player_1_score", "op": ">=", "value": 1}, self.view))
        self.assertFalse(evaluate_predicate({"field": "player_1_score", "op": ">", "value": 5}, self.view))
        self.assertTrue(evaluate_predicate({"field": "current_player", "op": "==", "value": "player_1"}, self.view))

    def test_units_matcher(self) -> None:
        self.assertTrue(evaluate_predicate({"units": {"controller": "player_1", "at": "battlefield_1"}}, self.view))
        self.assertFalse(evaluate_predicate({"units": {"controller": "player_1", "at": "base"}}, self.view))
        self.assertTrue(evaluate_predicate({"units": {"controller": "player_1", "name": "scuttle"}}, self.view))
        self.assertFalse(evaluate_predicate({"units": {"controller": "player_2", "min": 1}}, self.view))

    def test_boolean_combinators(self) -> None:
        p = {"all": [
            {"field": "player_1_score", "op": ">=", "value": 2},
            {"any": [{"units": {"controller": "player_1", "at": "base"}},
                     {"units": {"controller": "player_1", "at": "battlefield_1"}}]},
            {"not": {"units": {"controller": "player_2", "min": 1}}},
        ]}
        self.assertTrue(evaluate_predicate(p, self.view))

    def test_empty_predicate_is_true(self) -> None:
        self.assertTrue(evaluate_predicate({}, self.view))
        self.assertFalse(evaluate_predicate({"bogus": 1}, self.view))

    def test_units_at_battlefield_shorthand(self) -> None:
        view = {
            "player_1_units": [
                {"card": "Plundering Poro", "location": "battlefield_2", "exhausted": False},
                {"card": "Lonely Poro", "location": "base", "exhausted": False},
            ],
            "player_2_units": [],
        }
        # "battlefield" matches either battlefield_1/2
        self.assertTrue(evaluate_predicate({"units": {"name": "plundering", "at": "battlefield"}}, view))
        # Lonely Poro is at base → not on a battlefield
        self.assertFalse(evaluate_predicate({"units": {"name": "lonely", "at": "battlefield"}}, view))
        # list form
        self.assertTrue(evaluate_predicate({"units": {"name": "lonely", "at": ["base", "battlefield_1"]}}, view))
        # AND: both poros somewhere (one on bf, one at base) ⇒ true
        self.assertTrue(
            evaluate_predicate(
                {"all": [{"units": {"name": "plundering"}}, {"units": {"name": "lonely"}}]}, view
            )
        )
        # but "both on a battlefield" ⇒ false (lonely is at base)
        self.assertFalse(
            evaluate_predicate(
                {"all": [
                    {"units": {"name": "plundering", "at": "battlefield"}},
                    {"units": {"name": "lonely", "at": "battlefield"}},
                ]},
                view,
            )
        )

    def test_field_aliases_resolve(self) -> None:
        # A goal-compiler that writes "turn" instead of "total_turn_number"
        # must still resolve, not silently fail.
        view = {"total_turn_number": 3, "current_player": "player_1"}
        self.assertTrue(evaluate_predicate({"field": "turn", "op": "==", "value": 3}, view))
        self.assertTrue(evaluate_predicate({"field": "active_player", "op": "==", "value": "player_1"}, view))


class BfsSearchTests(unittest.TestCase):
    def test_finds_path_to_player_1_unit_in_play(self) -> None:
        eng = _drive_to_action_turn()
        start = copy.deepcopy(eng.game_state)
        predicate = {"units": {"controller": "player_1", "min": 1}}
        result = bfs_search(start, predicate, max_depth=12, node_budget=20000)
        self.assertTrue(result.found, f"search failed: {result.reason}")
        self.assertGreater(len(result.moves), 0)
        # Replaying the found moves on a fresh fork reaches the goal — proving
        # the path is real and legal.
        replay = GameEngine(game_state=copy.deepcopy(start))
        replay.start()
        for move in result.moves:
            for actor, action in move.steps:
                replay.apply_action(action=action, actor=actor)
        self.assertGreaterEqual(len(replay.game_state.player_1_units), 1)

    def test_already_satisfied_returns_empty_path(self) -> None:
        eng = _drive_to_action_turn()
        start = copy.deepcopy(eng.game_state)
        result = bfs_search(start, {"field": "current_player", "op": "==", "value": "player_1"}, max_depth=4)
        self.assertTrue(result.found)
        self.assertEqual(result.moves, [])

    def test_unreachable_within_budget_reports_not_found(self) -> None:
        eng = _drive_to_action_turn()
        start = copy.deepcopy(eng.game_state)
        # Impossible: a player can't reach score 99 in a couple plies.
        result = bfs_search(start, {"field": "player_1_score", "op": ">=", "value": 99}, max_depth=3, node_budget=300)
        self.assertFalse(result.found)


if __name__ == "__main__":
    unittest.main()
