import unittest

from riftbound_engine import GameEngine, GameState, RequiredTo


class GameEngineTests(unittest.TestCase):
    def test_counter_starts_at_zero(self) -> None:
        engine = GameEngine()
        self.assertEqual(engine.game_state, GameState(counter=0))

    def test_start_increments_counter_when_possible(self) -> None:
        engine = GameEngine()
        result = engine.start()

        self.assertEqual(result.game_state, GameState(counter=1))
        self.assertEqual(result.player_1_options, [])
        self.assertEqual(result.player_2_options, [])

    def test_when_increment_is_not_possible_options_are_required_for_player_1(self) -> None:
        engine = GameEngine(game_state=GameState(counter=1), max_counter=1)
        result = engine.start(required_to=RequiredTo.PLAYER_1)

        self.assertEqual(result.game_state, GameState(counter=1))
        self.assertEqual(result.player_1_options, ["resolve_counter_block"])
        self.assertEqual(result.player_2_options, [])

    def test_when_increment_is_not_possible_options_are_required_for_player_2(self) -> None:
        engine = GameEngine(game_state=GameState(counter=1), max_counter=1)
        result = engine.start(required_to=RequiredTo.PLAYER_2)

        self.assertEqual(result.game_state, GameState(counter=1))
        self.assertEqual(result.player_1_options, [])
        self.assertEqual(result.player_2_options, ["resolve_counter_block"])

    def test_when_increment_is_not_possible_options_can_be_required_for_both_players(self) -> None:
        engine = GameEngine(game_state=GameState(counter=1), max_counter=1)
        result = engine.start(required_to=RequiredTo.BOTH)

        self.assertEqual(result.game_state, GameState(counter=1))
        self.assertEqual(result.player_1_options, ["resolve_counter_block"])
        self.assertEqual(result.player_2_options, ["resolve_counter_block"])
