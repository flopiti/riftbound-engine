"""Activated abilities — "exhaust: do X" on an in-play permanent (no trigger).

Heart of Dark Ice (gear): "exhaust: Give a unit +3 might this turn." Activating
it exhausts the gear (the cost), the controller picks a unit (reusing the spell
target flow), and the effect resolves on the chain. Scope: EXHAUST_THIS-only
abilities; energy-cost ones (The Syren) are not handled yet."""

import unittest
from unittest import mock

from riftbound_engine import abilities as A
from riftbound_engine.abilities import Ability
from riftbound_engine.csv_data import card_might_of
from riftbound_engine.engine import (
    GameEngine,
    GameState,
    PlayedGear,
    PlayedUnit,
    RequiredStep,
    RequiredTo,
)

# "exhaust: give a unit +3 might" — activated (cost + effect, no trigger).
HEART = (Ability(costs=("EXHAUST_THIS",), active_effects=("GIVE_UNIT_+3M",)),)


def _fake(name):
    return HEART if name == "Heart of Dark Ice" else ()


def _drive_to_action_turn(engine: GameEngine):
    """Run the standard start sequence so player_1 sits on their action turn."""
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


class ActivateTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A, "triggered_abilities_for", side_effect=_fake)
        p.start(); self.addCleanup(p.stop)

    def _live_engine(self, *, gear_exhausted=False):
        """A real game driven to player_1's action turn, with a Heart of Dark Ice
        gear and a Plundering Poro injected onto player_1's board."""
        rolls = iter([6, 2])
        eng = GameEngine(dice_roller=lambda: next(rolls))
        ready = _drive_to_action_turn(eng)
        assert ready.required_action.name == RequiredStep.ACTION_TURN
        gs = eng._game_state
        uid = gs.next_unit_uid
        gs.next_unit_uid = uid + 1
        gs.player_1_units = [PlayedUnit(card="Plundering Poro", location="base", exhausted=False, uid=uid)]
        gs.player_1_gears = [PlayedGear(card="Heart of Dark Ice", location="base", exhausted=gear_exhausted)]
        return eng

    def _stub_engine(self, gear_exhausted=False):
        """Minimal hand-built state for direct apply_action tests (no start())."""
        gs = GameState(
            started=True, abcd_a_done=True, abcd_b_done=True, abcd_c_done=True, abcd_d_done=True,
            current_player=RequiredTo.PLAYER_1,
            player_1_units=[PlayedUnit(card="Plundering Poro", location="base", exhausted=False, uid=1)],
            player_1_gears=[PlayedGear(card="Heart of Dark Ice", location="base", exhausted=gear_exhausted)],
        )
        return GameEngine(game_state=gs)

    def test_activate_option_is_offered(self):
        out = self._live_engine().start()
        self.assertIn("play:activate:gear:player_1:0", out.player_1_options)

    def test_no_option_when_gear_exhausted(self):
        out = self._live_engine(gear_exhausted=True).start()
        self.assertNotIn("play:activate:gear:player_1:0", out.player_1_options)

    def test_activate_exhausts_gear_and_opens_target_pick(self):
        eng = self._stub_engine()
        eng.apply_action(action="play:activate:gear:player_1:0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertTrue(gs.player_1_gears[0].exhausted)  # cost paid
        self.assertIsNotNone(gs.pending_spell_choice)
        self.assertEqual(gs.pending_spell_choice.activation_source, "gear:player_1:0")

    def test_full_activation_buffs_the_chosen_unit(self):
        eng = self._live_engine()
        eng.apply_action(action="play:activate:gear:player_1:0", actor=RequiredTo.PLAYER_1)
        out = eng.start()
        pick = next(o for o in out.player_1_options if o.startswith("play:choose_spell_targets:"))
        eng.apply_action(action=pick, actor=RequiredTo.PLAYER_1)
        # Effect is on the chain; resolve it (both players pass priority).
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_2)
        unit = eng._game_state.player_1_units[0]
        self.assertEqual(unit.bonus_might, 3)  # +3 Might landed on the chosen unit

    def test_cannot_activate_on_opponents_turn(self):
        eng = self._stub_engine()
        eng._game_state.current_player = RequiredTo.PLAYER_2
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:activate:gear:player_1:0", actor=RequiredTo.PLAYER_1)


if __name__ == "__main__":
    unittest.main()
