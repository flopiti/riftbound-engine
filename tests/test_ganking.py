"""[Ganking] — "I can move from battlefield to battlefield." Normally a unit may
only move base ↔ battlefield; Ganking (printed, or granted by equipment like
Boots of Swiftness) additionally permits battlefield → battlefield.
"""

import unittest

from riftbound_engine.engine import (
    GameEngine,
    PlayedGear,
    PlayedUnit,
    RequiredStep,
    RequiredTo,
)


def _drive_to_action_turn(engine: GameEngine):
    first = engine.start()
    engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
    engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_2)
    fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)
    fifth = engine.apply_action(
        action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1
    )
    engine.apply_action(
        action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2
    )
    engine.apply_action(action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:", actor=RequiredTo.PLAYER_1)
    return engine.apply_action(
        action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:", actor=RequiredTo.PLAYER_2
    )


class GankingMoveTests(unittest.TestCase):
    def _live(self, unit_card, *, boots=False):
        """player_1's action turn with one ready unit at battlefield_1;
        battlefield_2 is player_1's own (so a BF→BF move just relocates, no
        showdown). Optionally attach Boots of Swiftness (grants [Ganking])."""
        rolls = iter([6, 2])
        eng = GameEngine(dice_roller=lambda: next(rolls))
        ready = _drive_to_action_turn(eng)
        assert ready.required_action.name == RequiredStep.ACTION_TURN
        gs = eng._game_state
        uid = gs.next_unit_uid
        gs.next_unit_uid = uid + 1
        gs.player_1_units = [
            PlayedUnit(card=unit_card, location="battlefield_1", exhausted=False, uid=uid)
        ]
        gs.player_1_gears = (
            [PlayedGear(card="Boots of Swiftness", location="battlefield_1", attached_uid=uid)]
            if boots
            else []
        )
        gs.battlefield_2_controller = RequiredTo.PLAYER_1  # own → relocate, no showdown
        return eng

    def test_printed_ganking_can_jump_battlefields(self):
        eng = self._live("Vi, Destructive")  # printed [Ganking]
        out = eng.start()
        self.assertIn("play:move_unit:0:battlefield_2", out.player_1_options)
        eng.apply_action(action="play:move_unit:0:battlefield_2", actor=RequiredTo.PLAYER_1)
        self.assertEqual(eng._game_state.player_1_units[0].location, "battlefield_2")

    def test_non_ganking_cannot_jump_battlefields(self):
        eng = self._live("Plundering Poro")  # no [Ganking]
        out = eng.start()
        self.assertNotIn("play:move_unit:0:battlefield_2", out.player_1_options)
        self.assertIn("play:move_unit:0:base", out.player_1_options)  # base still ok
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:move_unit:0:battlefield_2", actor=RequiredTo.PLAYER_1)

    def test_boots_of_swiftness_grants_ganking(self):
        eng = self._live("Plundering Poro", boots=True)  # granted [Ganking]
        self.assertTrue(eng.unit_has_ganking("player_1", 0))
        out = eng.start()
        self.assertIn("play:move_unit:0:battlefield_2", out.player_1_options)
        eng.apply_action(action="play:move_unit:0:battlefield_2", actor=RequiredTo.PLAYER_1)
        self.assertEqual(eng._game_state.player_1_units[0].location, "battlefield_2")


if __name__ == "__main__":
    unittest.main()
