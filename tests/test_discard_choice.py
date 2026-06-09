"""Tests for DISCARD_1 / DISCARD_1_DRAW_1 as FORCED choice effects: the player
picks WHICH hand card to discard (play:choose_effect_target:h-<i>, no pass),
then draws. Zaun Warrens ("when you conquer here, discard 1, then draw 1") is
the live user of DISCARD_1_DRAW_1."""

from __future__ import annotations

import random
import unittest
from unittest import mock

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine import abilities as A
from riftbound_engine.engine import GameState, PlayedUnit, build_deck_from_id


def _engine_with_ability(card: str, effects: tuple[str, ...], hand: list[str], library: list[str]):
    """A started engine, P1 active, with one taxonomy ability stubbed onto a
    unit P1 controls so we can drive the effect codes directly via _emit."""
    gs = GameState()
    gs.started = True
    gs.is_mulligan_done = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    gs.current_player = RequiredTo.PLAYER_1
    # Decks/battlefields set so start() clears the setup gates and returns
    # action-turn options instead of re-entering deck selection.
    gs.player_1_deck = build_deck_from_id("ezreal_prodigal_explorer")
    gs.player_2_deck = build_deck_from_id("irelia_nates")
    gs.battlefield_1 = "A"
    gs.battlefield_2 = "B"
    gs.player_1_hand = list(hand)
    gs.player_1_library = list(library)
    gs.player_1_units = [PlayedUnit(card=card, location="base", exhausted=False)]
    eng = GameEngine(game_state=gs)
    stub = (A.Ability(triggers=("WHEN_YOU_PLAY_ME",), active_effects=effects),)
    patch = mock.patch.object(
        A, "triggered_abilities_for", side_effect=lambda n: stub if n == card else ()
    )
    return eng, patch


def _fire(eng, patch):
    """Emit the unit's ON_PLAY_UNIT trigger and drain it onto the chain, then
    resolve until a choice pauses (or the chain drains)."""
    from riftbound_engine.triggers import GameEvent

    gs = eng._game_state
    with patch:
        eng._emit(GameEvent(kind="ON_PLAY_UNIT", controller="player_1", source="player_1:0"))
        eng._drain_triggers()
        chain = gs.pending_chain
        while chain and chain.items and gs.pending_effect_choice is None:
            eng._execute_chain_item(chain.items.pop(0))


class DiscardChoiceTests(unittest.TestCase):
    def test_forced_choice_offers_hand_cards_no_pass(self) -> None:
        eng, patch = _engine_with_ability(
            "Tester", ("DISCARD_1_DRAW_1",), hand=["Scuttle Crab", "Lonely Poro"], library=["En Garde"]
        )
        _fire(eng, patch)
        choice = eng._game_state.pending_effect_choice
        self.assertIsNotNone(choice)
        self.assertEqual(choice.options, ["h-0", "h-1"])
        self.assertFalse(choice.optional)  # forced — no decline
        out = eng.start()
        self.assertEqual(
            out.player_1_options,
            ["play:choose_effect_target:h-0", "play:choose_effect_target:h-1"],
        )
        self.assertNotIn("play:choose_effect_target:pass", out.player_1_options)

    def test_pick_discards_that_card_and_draws(self) -> None:
        eng, patch = _engine_with_ability(
            "Tester", ("DISCARD_1_DRAW_1",), hand=["Scuttle Crab", "Lonely Poro"], library=["En Garde"]
        )
        _fire(eng, patch)
        eng.apply_action("play:choose_effect_target:h-0", RequiredTo.PLAYER_1)  # discard Scuttle Crab
        gs = eng._game_state
        self.assertIn("Scuttle Crab", gs.player_1_trash)
        self.assertEqual(gs.player_1_hand, ["Lonely Poro", "En Garde"])  # kept + drew
        self.assertEqual(gs.player_1_library, [])
        self.assertIsNone(gs.pending_effect_choice)

    def test_pass_rejected_on_forced_choice(self) -> None:
        eng, patch = _engine_with_ability(
            "Tester", ("DISCARD_1_DRAW_1",), hand=["Scuttle Crab"], library=["En Garde"]
        )
        _fire(eng, patch)
        with self.assertRaises(ValueError):
            eng.apply_action("play:choose_effect_target:pass", RequiredTo.PLAYER_1)
        # still pending
        self.assertIsNotNone(eng._game_state.pending_effect_choice)

    def test_empty_hand_combined_code_still_draws(self) -> None:
        eng, patch = _engine_with_ability(
            "Tester", ("DISCARD_1_DRAW_1",), hand=[], library=["En Garde", "Cleave"]
        )
        _fire(eng, patch)
        gs = eng._game_state
        # Nothing to discard ⇒ no choice opened, but "then draw 1" still
        # happens via on_empty: you skip the discard and draw.
        self.assertIsNone(gs.pending_effect_choice)
        self.assertEqual(gs.player_1_hand, ["En Garde"])  # drew the top card
        self.assertEqual(gs.player_1_library, ["Cleave"])

    def test_split_codes_draw_even_when_hand_emptied(self) -> None:
        # [DISCARD_1, DRAW_1]: discard the only card (forced choice), then the
        # instant DRAW_1 still runs afterward — so the draw happens even though
        # the discard emptied the hand.
        eng, patch = _engine_with_ability(
            "Tester", ("DISCARD_1", "DRAW_1"), hand=["Scuttle Crab"], library=["En Garde"]
        )
        _fire(eng, patch)
        self.assertEqual(eng._game_state.pending_effect_choice.options, ["h-0"])
        eng.apply_action("play:choose_effect_target:h-0", RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertIn("Scuttle Crab", gs.player_1_trash)
        self.assertEqual(gs.player_1_hand, ["En Garde"])  # discarded one, drew one
        self.assertIsNone(gs.pending_effect_choice)


if __name__ == "__main__":
    unittest.main()
