"""[Shield] gives a unit +Might WHILE IT IS DEFENDING (in a showdown over a
battlefield it controls — the attacker is whoever moved in). Shield comes from
the printed keyword, from attached equipment (SHIELD_N, stacks), and from
this-turn grants (bonus_shield). It flows through effective_unit_might, so it
boosts the combat budget and lethal thresholds too — but only for the defender,
only at the contested battlefield, and only during the showdown/combat."""

import unittest
from unittest import mock

from riftbound_engine import abilities as _abilities
from riftbound_engine.abilities import Ability
from riftbound_engine.csv_data import card_might_of
from riftbound_engine.engine import (
    GameEngine,
    GameState,
    PendingCombat,
    PendingShowdown,
    PlayedGear,
    PlayedUnit,
    RequiredTo,
)

SHIELDER = "Stalwart Poro"  # printed [Shield] = +1, Might 2
BASE = card_might_of(SHIELDER) or 0


def _eng(**over):
    gs = GameState(
        player_1_units=[PlayedUnit(card=SHIELDER, location="battlefield_1", uid=1)],
        battlefield_1_controller=RequiredTo.PLAYER_1,
    )
    for k, v in over.items():
        setattr(gs, k, v)
    return GameEngine(game_state=gs)


def _showdown(initiator):
    return PendingShowdown(battlefield="battlefield_1", initiator=initiator)


class ShieldDefendingTests(unittest.TestCase):
    def test_no_shield_outside_showdown(self):
        eng = _eng()
        self.assertEqual(eng.effective_unit_might("player_1", 0), BASE)

    def test_defender_gets_shield(self):
        # P2 moved in (initiator/attacker); P1 holds battlefield_1 → defends.
        eng = _eng(pending_showdown=_showdown(RequiredTo.PLAYER_2))
        self.assertEqual(eng.effective_unit_might("player_1", 0), BASE + 1)

    def test_attacker_gets_no_shield(self):
        # P1 itself initiated → it's the attacker, Shield does nothing.
        eng = _eng(pending_showdown=_showdown(RequiredTo.PLAYER_1))
        self.assertEqual(eng.effective_unit_might("player_1", 0), BASE)

    def test_shield_inert_at_other_battlefield(self):
        eng = _eng()
        eng._game_state.pending_showdown = PendingShowdown(
            battlefield="battlefield_2", initiator=RequiredTo.PLAYER_2
        )
        self.assertEqual(eng.effective_unit_might("player_1", 0), BASE)

    def test_combat_budget_includes_defender_shield(self):
        eng = _eng(pending_showdown=_showdown(RequiredTo.PLAYER_2))
        self.assertEqual(
            eng.might_at_battlefield(RequiredTo.PLAYER_1, "battlefield_1"), BASE + 1
        )

    def test_pending_combat_step_uses_initiator(self):
        # After the showdown becomes combat, the defender still keeps Shield via
        # PendingCombat.initiator (the attacker).
        eng = _eng()
        eng._game_state.pending_combat = PendingCombat(
            battlefield="battlefield_1",
            player_1_might=0,
            player_2_might=0,
            initiator=RequiredTo.PLAYER_2,
        )
        self.assertEqual(eng.effective_unit_might("player_1", 0), BASE + 1)

    def test_granted_bonus_shield_stacks_while_defending(self):
        eng = _eng(pending_showdown=_showdown(RequiredTo.PLAYER_2))
        eng._game_state.player_1_units[0].bonus_shield = 2
        # printed 1 + granted 2 = +3 while defending.
        self.assertEqual(eng.effective_unit_might("player_1", 0), BASE + 3)
        # ...but nothing when not defending.
        eng._game_state.pending_showdown = None
        self.assertEqual(eng.effective_unit_might("player_1", 0), BASE)

    def test_equipment_shield_stacks(self):
        eng = _eng(pending_showdown=_showdown(RequiredTo.PLAYER_2))
        eng._game_state.player_1_gears = [
            PlayedGear(card="Cloth Armor", attached_uid=1, attached_to="player_1:0")
        ]
        gear_ab = (Ability(effect_text=True, passive_effects=("SHIELD_2",)),)
        with mock.patch.object(
            _abilities,
            "triggered_abilities_for",
            side_effect=lambda n: gear_ab if n == "Cloth Armor" else (),
        ):
            # printed 1 + equipment SHIELD_2 = +3 while defending.
            self.assertEqual(eng.effective_unit_might("player_1", 0), BASE + 3)


if __name__ == "__main__":
    unittest.main()
