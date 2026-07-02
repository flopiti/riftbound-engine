"""The Syren (gear): "1 energy, exhaust: Move a friendly unit at a battlefield
to its base." (effect code MOVE_FRIENDLY_UNIT_BF_TO_BASE).

Activating it spends 1 energy + exhausts the gear (the cost), the controller
picks one of their units AT a battlefield (the derived "FRIENDLY UNIT (1[BF])"
target requirement — destination is fixed at base, so there is NO destination
pick), and on resolution that unit moves to base. A friendly unit at base is
not a legal target, so the activation isn't even offered when no friendly unit
is at a battlefield."""

import unittest
from unittest import mock

from riftbound_engine import abilities as A
from riftbound_engine.abilities import Ability
from riftbound_engine.engine import (
    GameEngine,
    GameState,
    PlayedGear,
    PlayedUnit,
    RequiredStep,
    RequiredTo,
)

# "1 energy, exhaust: move a friendly unit at a battlefield to its base."
SYREN = (
    Ability(
        costs=("PAY_1_ENERGY", "EXHAUST_THIS"),
        active_effects=("MOVE_FRIENDLY_UNIT_BF_TO_BASE",),
    ),
)


def _fake(name):
    return SYREN if name == "The Syren" else ()


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


class SyrenMoveTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A, "triggered_abilities_for", side_effect=_fake)
        p.start(); self.addCleanup(p.stop)

    def _live_engine(self, *, unit_location="battlefield_1", gear_exhausted=False, energy=3):
        """A real game driven to player_1's action turn, with a ready The Syren
        gear and a single friendly unit placed at ``unit_location``."""
        rolls = iter([6, 2])
        eng = GameEngine(dice_roller=lambda: next(rolls))
        ready = _drive_to_action_turn(eng)
        assert ready.required_action.name == RequiredStep.ACTION_TURN
        gs = eng._game_state
        uid = gs.next_unit_uid
        gs.next_unit_uid = uid + 1
        gs.player_1_units = [
            PlayedUnit(card="Plundering Poro", location=unit_location, exhausted=False, uid=uid)
        ]
        gs.player_1_gears = [PlayedGear(card="The Syren", location="base", exhausted=gear_exhausted)]
        gs.player_1_energy = energy
        return eng

    def test_activate_offered_with_a_battlefield_unit(self):
        out = self._live_engine(unit_location="battlefield_1").start()
        self.assertIn("play:activate:gear:player_1:0", out.player_1_options)

    def test_not_offered_when_unit_only_at_base(self):
        # No friendly unit at a battlefield ⇒ no legal target ⇒ no activation.
        out = self._live_engine(unit_location="base").start()
        self.assertNotIn("play:activate:gear:player_1:0", out.player_1_options)

    def test_not_offered_when_gear_exhausted(self):
        out = self._live_engine(gear_exhausted=True).start()
        self.assertNotIn("play:activate:gear:player_1:0", out.player_1_options)

    def test_not_offered_without_energy(self):
        out = self._live_engine(energy=0).start()
        self.assertNotIn("play:activate:gear:player_1:0", out.player_1_options)

    def test_full_activation_moves_unit_to_base(self):
        eng = self._live_engine(unit_location="battlefield_1")
        energy_before = eng._game_state.player_1_energy
        eng.apply_action(action="play:activate:gear:player_1:0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertTrue(gs.player_1_gears[0].exhausted)            # exhaust paid
        self.assertEqual(gs.player_1_energy, energy_before - 1)    # 1 energy paid
        out = eng.start()
        pick = next(o for o in out.player_1_options if o.startswith("play:choose_spell_targets:"))
        eng.apply_action(action=pick, actor=RequiredTo.PLAYER_1)
        # Effect is on the chain; resolve it (both players pass priority).
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_2)
        unit = eng._game_state.player_1_units[0]
        self.assertEqual(unit.location, "base")     # moved off the battlefield
        self.assertFalse(unit.exhausted)            # a free move — unit not exhausted

    def test_cannot_activate_on_opponents_turn(self):
        eng = self._live_engine()
        eng._game_state.current_player = RequiredTo.PLAYER_2
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:activate:gear:player_1:0", actor=RequiredTo.PLAYER_1)


if __name__ == "__main__":
    unittest.main()
