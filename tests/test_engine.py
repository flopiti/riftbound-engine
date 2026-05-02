import unittest

from riftbound_engine import GameEngine, GameState, RequiredTo, build_deck_from_id


class GameEngineTests(unittest.TestCase):
    def test_counter_starts_at_zero(self) -> None:
        engine = GameEngine()
        self.assertEqual(engine.game_state, GameState(counter=0))

    def test_start_requires_player_1_to_choose_deck_first(self) -> None:
        engine = GameEngine()
        result = engine.start()

        self.assertEqual(result.required_action.name, "choose_deck")
        self.assertEqual(result.required_action.actor, RequiredTo.PLAYER_1)
        self.assertGreaterEqual(len(result.player_1_options), 2)

    def test_deck_selection_is_sequential_player_1_then_player_2(self) -> None:
        engine = GameEngine()
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)

        self.assertEqual(second.required_action.name, "choose_deck")
        self.assertEqual(second.required_action.actor, RequiredTo.PLAYER_2)
        self.assertIsNotNone(second.game_state.player_1_deck)
        self.assertIsNone(second.game_state.player_2_deck)

    def test_after_both_decks_selected_first_turn_choice_is_prompted(self) -> None:
        engine = GameEngine()
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)

        self.assertEqual(third.required_action.name, "choose_first_turn")
        self.assertIn(third.required_action.actor, (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2))
        self.assertEqual(len(third.game_state.player_1_deck.battlefields), 3)
        self.assertEqual(len(third.game_state.player_2_deck.battlefields), 3)
        self.assertEqual(len(third.game_state.player_1_deck.cards), 39)
        self.assertEqual(len(third.game_state.player_2_deck.cards), 39)
        self.assertEqual(third.game_state.counter, 0)
        self.assertFalse(third.game_state.started)
        self.assertEqual(third.game_state.first_turn_choice, third.required_action.actor)
        self.assertIsNone(third.game_state.first_turn)

    def test_deck_builder_enforces_required_shape(self) -> None:
        deck = build_deck_from_id("ember_vanguard")
        self.assertEqual(len(deck.battlefields), 3)
        self.assertTrue(deck.chosen_champion)
        self.assertTrue(deck.legend)
        self.assertEqual(len(deck.cards), 39)
        self.assertLessEqual(max(deck.cards.count(card) for card in set(deck.cards)), 3)
        self.assertEqual(len(deck.runes), 12)
        self.assertTrue(all(r.domain == "Calm" for r in deck.runes))

    def test_tide_wardens_runes_are_six_fury_six_body(self) -> None:
        deck = build_deck_from_id("tide_wardens")
        self.assertLessEqual(max(deck.cards.count(card) for card in set(deck.cards)), 3)
        self.assertEqual(len(deck.runes), 12)
        domains = [r.domain for r in deck.runes]
        self.assertEqual(domains.count("Fury"), 6)
        self.assertEqual(domains.count("Body"), 6)

    def test_game_with_both_decks_selected_is_ready(self) -> None:
        engine = GameEngine(
            game_state=GameState(
                counter=0,
                started=False,
                current_player=RequiredTo.PLAYER_1,
                player_1_deck=build_deck_from_id("ember_vanguard"),
                player_2_deck=build_deck_from_id("tide_wardens"),
            )
        )
        result = engine.start()
        self.assertEqual(result.required_action.name, "choose_first_turn")
        self.assertIn(result.required_action.actor, (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2))
        self.assertEqual(result.game_state.counter, 0)
        self.assertEqual(result.game_state.first_turn_choice, result.required_action.actor)
        self.assertIsNone(result.game_state.first_turn)

    def test_first_turn_choice_rerolls_on_tie(self) -> None:
        rolls = iter([4, 4, 2, 6])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)

        self.assertEqual(third.required_action.actor, RequiredTo.PLAYER_2)
        self.assertEqual(third.game_state.first_turn_choice, RequiredTo.PLAYER_2)

    def test_first_turn_selection_completes_setup(self) -> None:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_2", actor=RequiredTo.PLAYER_1)

        self.assertIsNotNone(fourth.required_action)
        self.assertEqual(fourth.required_action.name, "choose_battlefields")
        self.assertEqual(fourth.required_action.actor, RequiredTo.BOTH)
        self.assertEqual(fourth.game_state.first_turn, RequiredTo.PLAYER_2)
        self.assertEqual(fourth.game_state.counter, 0)
        self.assertFalse(fourth.game_state.started)

    def test_both_battlefields_are_required_before_setup_complete(self) -> None:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)

        self.assertEqual(fourth.required_action.name, "choose_battlefields")
        self.assertEqual(fourth.required_action.actor, RequiredTo.BOTH)
        self.assertEqual(len(fourth.player_1_options), 3)
        self.assertEqual(len(fourth.player_2_options), 3)

        fifth = engine.apply_action(action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        self.assertEqual(fifth.required_action.name, "choose_battlefields")
        self.assertEqual(fifth.game_state.battlefield_1, fourth.player_1_options[0])
        self.assertEqual(fifth.game_state.player_1_base, fourth.player_1_options[0])
        self.assertIsNone(fifth.game_state.battlefield_2)
        self.assertIsNone(fifth.game_state.player_2_base)

        sixth = engine.apply_action(action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        self.assertEqual(sixth.required_action.name, "choose_mulligan")
        self.assertEqual(sixth.required_action.actor, RequiredTo.BOTH)
        self.assertEqual(len(sixth.player_1_options), 4)
        self.assertEqual(len(sixth.player_2_options), 4)
        self.assertEqual(sixth.game_state.battlefield_2, fifth.player_2_options[0])
        self.assertEqual(sixth.game_state.player_2_base, fifth.player_2_options[0])
        self.assertFalse(sixth.game_state.is_mulligan_done)
        self.assertEqual(sixth.game_state.counter, 0)
        self.assertFalse(sixth.game_state.started)

        seventh = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:",
            actor=RequiredTo.PLAYER_1,
        )
        self.assertEqual(seventh.required_action.name, "choose_mulligan")
        self.assertTrue(seventh.game_state.mulligan_player_1_resolved)
        self.assertFalse(seventh.game_state.mulligan_player_2_resolved)
        self.assertEqual(len(seventh.game_state.player_1_hand), 4)
        self.assertEqual(len(seventh.game_state.player_1_library), 35)
        self.assertIsNone(seventh.game_state.player_2_hand)

        eighth = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:",
            actor=RequiredTo.PLAYER_2,
        )
        self.assertIsNone(eighth.required_action)
        self.assertTrue(eighth.game_state.is_mulligan_done)
        self.assertEqual(len(eighth.game_state.player_1_library), 35)
        self.assertEqual(len(eighth.game_state.player_2_library), 35)
        self.assertEqual(len(eighth.game_state.player_1_hand), 4)
        self.assertEqual(len(eighth.game_state.player_2_hand), 4)
        self.assertEqual(eighth.game_state.counter, 1)
        self.assertTrue(eighth.game_state.started)
