"""A [Repeat] round lands on the chain as its OWN effect item.

When a spell has implemented effect codes, each paid [Repeat] round is pushed
onto the chain as a separate effect item (resolving one after the other),
rather than being folded into the base spell item.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine.engine import PlayedUnit


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


def _fake_abilities(name: str):
    # Give Bellows Breath an implemented effect so repeats split into items.
    if name.strip().lower() == "bellows breath":
        return (SimpleNamespace(active_effects=("DEAL_1_TO_3_UNITS_SAME_LOC",)),)
    return ()


class RepeatChainSplitTests(unittest.TestCase):
    def test_repeat_lands_as_separate_chain_item(self) -> None:
        with patch(
            "riftbound_engine.abilities.triggered_abilities_for", side_effect=_fake_abilities
        ):
            eng = _drive_to_action_turn()
            eng.start()
            gs = eng._game_state
            gs.player_1_hand = ["Bellows Breath"]
            eng.add_energy(RequiredTo.PLAYER_1, 10)
            gs.player_1_power = {"Mind": 9}
            gs.player_1_units = [
                PlayedUnit(card="Ravenbloom Student", location="base", exhausted=False)
            ]
            eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
            eng.apply_action("play:choose_spell_targets:p1-0", RequiredTo.PLAYER_1)  # base round
            eng.apply_action("play:choose_repeat:yes", RequiredTo.PLAYER_1)
            eng.apply_action("play:choose_spell_targets:p1-0", RequiredTo.PLAYER_1)  # repeat round

            items = gs.pending_chain.items
            self.assertEqual(len(items), 2)
            # Base spell item — repeats are NOT folded into it.
            self.assertEqual(items[0].card, "Bellows Breath")
            self.assertEqual(items[0].repeat_targets, [])
            # Separate repeat EFFECT item, resolving after the base.
            self.assertIsNone(items[1].card)
            self.assertIsNotNone(items[1].effect)
            self.assertEqual(items[1].label, "Bellows Breath (repeat)")

    def test_repeat_without_effect_codes_stays_single_item(self) -> None:
        # No implemented effect → legacy single-item behavior. Bellows Breath IS
        # implemented in the real taxonomy now, so force the hermetic no-ability
        # case (matching test_repeat_lands_as_separate_chain_item's approach).
        with patch(
            "riftbound_engine.abilities.triggered_abilities_for",
            side_effect=lambda name: (),
        ):
            eng = _drive_to_action_turn()
            eng.start()
            gs = eng._game_state
            gs.player_1_hand = ["Bellows Breath"]
            eng.add_energy(RequiredTo.PLAYER_1, 10)
            gs.player_1_power = {"Mind": 9}
            gs.player_1_units = [
                PlayedUnit(card="Ravenbloom Student", location="base", exhausted=False)
            ]
            eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
            eng.apply_action("play:choose_spell_targets:p1-0", RequiredTo.PLAYER_1)
            eng.apply_action("play:choose_repeat:yes", RequiredTo.PLAYER_1)
            eng.apply_action("play:choose_spell_targets:p1-0", RequiredTo.PLAYER_1)
            items = gs.pending_chain.items
            self.assertEqual(len(items), 1)  # folded into the single spell item
            self.assertEqual(items[0].card, "Bellows Breath")
            self.assertEqual(len(items[0].repeat_targets), 1)


if __name__ == "__main__":
    unittest.main()
