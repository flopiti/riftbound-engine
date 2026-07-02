"""Defiant Dance (sfd-196): "Give a unit +2 might this turn and another unit
-2 might this turn." Requirement ``ANY UNIT (1)|ANY UNIT (1)`` → two picks;
the first chosen unit gets +2 Might, the second gets -2.

This is the worked example for the "implement an effect, test-gated, then demo
it in a live game" loop: the test drives the engine to the exact state where
the effect is used (two units on the board, the spell in hand) and asserts it.
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
    if name.strip().lower() == "defiant dance":
        return (SimpleNamespace(active_effects=("GIVE_UNIT_+2_AND_ANOTHER_-2",)),)
    return ()


class DefiantDanceTests(unittest.TestCase):
    def test_first_target_plus2_second_minus2(self) -> None:
        with patch(
            "riftbound_engine.abilities.triggered_abilities_for", side_effect=_fake_abilities
        ):
            eng = _drive_to_action_turn()
            eng.start()
            gs = eng._game_state
            gs.player_1_hand = ["Defiant Dance"]
            eng.add_energy(RequiredTo.PLAYER_1, 10)
            gs.player_1_power = {"Calm": 9, "Chaos": 9}
            gs.player_1_units = [
                PlayedUnit(card="Ravenbloom Student", location="base", exhausted=False),
                PlayedUnit(card="Scuttle Crab", location="base", exhausted=False),
            ]
            eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
            # Two picks, in order: first unit gets +2, second gets -2.
            eng.apply_action("play:choose_spell_targets:p1-0", RequiredTo.PLAYER_1)
            eng.apply_action("play:choose_spell_targets:p1-1", RequiredTo.PLAYER_1)
            # Resolve the chain (both players pass priority).
            eng.apply_action("play:pass_priority", RequiredTo.PLAYER_1)
            eng.apply_action("play:pass_priority", RequiredTo.PLAYER_2)

            self.assertEqual(gs.player_1_units[0].bonus_might, 2)
            self.assertEqual(gs.player_1_units[1].bonus_might, -2)


if __name__ == "__main__":
    unittest.main()
