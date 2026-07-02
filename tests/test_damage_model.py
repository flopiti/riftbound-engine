"""Persistent-damage model (Riftbound rules 142/143, 417 Deal, 418 Heal,
712-715 Bonus Damage, 323.4/323.5 cleanup) and the five damage effects:

  DEAL_3_DAMAGE, DEAL_6_TO_2_UNITS, DEAL_1_TO_3_UNITS_SAME_LOC,
  DEAL_1_DAMAGE_TO_UNITS_HERE (Frozen Fortress, Beginning Phase),
  SPELLS_ABILITIES_DEALING_DMG_DEAL_1_BONUS (Void Gate Bonus Damage).

Units use a fake (might-0) card with ``bonus_might`` to set an exact Might
threshold, so the lethal math is explicit and independent of the card CSV.
"""

import unittest
from unittest import mock

from riftbound_engine import abilities as A
from riftbound_engine import effects
from riftbound_engine.abilities import Ability
from riftbound_engine.engine import GameEngine, GameState, PlayedUnit, RequiredTo

VOID_GATE = (Ability(passive_effects=("SPELLS_ABILITIES_DEALING_DMG_DEAL_1_BONUS",)),)
FROZEN = (Ability(passive_effects=("DEAL_1_DAMAGE_TO_UNITS_HERE",)),)


def _abilities_for(name):
    if name == "Void Gate":
        return VOID_GATE
    if name == "Frozen Fortress":
        return FROZEN
    return ()


def _started(**kw) -> GameState:
    return GameState(
        started=True, abcd_a_done=True, abcd_b_done=True, abcd_c_done=True, abcd_d_done=True, **kw
    )


def _u(might: int, location: str = "battlefield_1", uid: int = 1) -> PlayedUnit:
    # Fake card has printed Might 0; bonus_might sets the effective Might.
    return PlayedUnit(card="Dummy", location=location, exhausted=False, bonus_might=might, uid=uid)


class DamageMarkingTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A, "triggered_abilities_for", side_effect=_abilities_for)
        p.start()
        self.addCleanup(p.stop)

    def test_deal_marks_damage_and_is_not_instantly_lethal(self):
        eng = GameEngine(game_state=_started(player_1_units=[_u(5)]))
        eng.deal_damage("player_1:0", 3)
        # Marked, but below Might (5) — survives until/unless more lands.
        self.assertEqual(eng._game_state.player_1_units[0].damage, 3)
        self.assertEqual(eng._lethal_sweep(), 0)
        self.assertEqual(len(eng._game_state.player_1_units), 1)

    def test_damage_accumulates_across_instances_then_lethal(self):
        eng = GameEngine(game_state=_started(player_1_units=[_u(5)]))
        eng.deal_damage("player_1:0", 3)
        eng.deal_damage("player_1:0", 2)  # total 5 >= Might 5
        self.assertEqual(eng._game_state.player_1_units[0].damage, 5)
        killed = eng._lethal_sweep()
        self.assertEqual(killed, 1)
        self.assertEqual(eng._game_state.player_1_units, [])
        self.assertEqual(eng._game_state.player_1_trash, ["Dummy"])

    def test_invalid_damage_amount_is_not_dealt(self):
        eng = GameEngine(game_state=_started(player_1_units=[_u(5)]))
        self.assertEqual(eng.deal_damage("player_1:0", 0), 0)
        self.assertEqual(eng._game_state.player_1_units[0].damage, 0)

    def test_heal_clears_marked_damage(self):
        eng = GameEngine(game_state=_started(player_1_units=[_u(5)]))
        eng.deal_damage("player_1:0", 4)
        eng._heal_all_damage()
        self.assertEqual(eng._game_state.player_1_units[0].damage, 0)


class BonusDamageTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A, "triggered_abilities_for", side_effect=_abilities_for)
        p.start()
        self.addCleanup(p.stop)

    def test_void_gate_adds_one_bonus_to_deals_here(self):
        # Unit (Might 4) at Void Gate. A 3-damage Deal becomes 4 with the +1
        # Bonus Damage → exactly lethal.
        eng = GameEngine(
            game_state=_started(battlefield_1="Void Gate", player_1_units=[_u(4)])
        )
        dealt = eng.deal_damage("player_1:0", 3)
        self.assertEqual(dealt, 4)  # 3 + 1 Bonus
        self.assertEqual(eng._lethal_sweep(), 1)

    def test_no_bonus_without_void_gate(self):
        eng = GameEngine(game_state=_started(battlefield_1="Plain", player_1_units=[_u(4)]))
        self.assertEqual(eng.deal_damage("player_1:0", 3), 3)
        self.assertEqual(eng._lethal_sweep(), 0)

    def test_bonus_only_applies_to_units_at_that_battlefield(self):
        # Same Void Gate at BF1, but the unit sits at BF2 → no bonus.
        eng = GameEngine(
            game_state=_started(battlefield_1="Void Gate", player_1_units=[_u(4, location="battlefield_2")])
        )
        self.assertEqual(eng.deal_damage("player_1:0", 3), 3)


class DealEffectTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A, "triggered_abilities_for", side_effect=_abilities_for)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, eng, code, targets):
        effects.execute_effect(
            effects.EffectContext(
                engine=eng, controller=RequiredTo.PLAYER_1, source=None, code=code, targets=tuple(targets)
            )
        )

    def test_deal_3_damage_kills_3_might_target(self):
        eng = GameEngine(game_state=_started(player_2_units=[_u(3, uid=2)]))
        self._run(eng, "DEAL_3_DAMAGE", ["player_2:0"])
        self.assertEqual(eng._lethal_sweep(), 1)
        self.assertEqual(eng._game_state.player_2_units, [])

    def test_deal_6_to_2_units_hits_each_for_6(self):
        eng = GameEngine(
            game_state=_started(player_2_units=[_u(6, uid=2), _u(7, location="battlefield_2", uid=3)])
        )
        self._run(eng, "DEAL_6_TO_2_UNITS", ["player_2:0", "player_2:1"])
        # First unit (Might 6) takes 6 → lethal; second (Might 7) takes 6 → survives.
        self.assertEqual(eng._game_state.player_2_units[0].damage, 6)
        self.assertEqual(eng._game_state.player_2_units[1].damage, 6)
        eng._lethal_sweep()
        cards_left = [(u.bonus_might) for u in eng._game_state.player_2_units]
        self.assertEqual(cards_left, [7])

    def test_deal_1_to_3_units_same_loc(self):
        eng = GameEngine(
            game_state=_started(
                player_2_units=[_u(1, uid=2), _u(1, uid=3), _u(2, uid=4)]
            )
        )
        self._run(eng, "DEAL_1_TO_3_UNITS_SAME_LOC", ["player_2:0", "player_2:1", "player_2:2"])
        eng._lethal_sweep()
        # The two 1-Might units die; the 2-Might one (took 1) survives.
        self.assertEqual([u.bonus_might for u in eng._game_state.player_2_units], [2])

    def test_deal_3_plus_void_gate_overkills_4_might(self):
        eng = GameEngine(
            game_state=_started(battlefield_1="Void Gate", player_2_units=[_u(4, uid=2)])
        )
        self._run(eng, "DEAL_3_DAMAGE", ["player_2:0"])  # 3 + 1 bonus = 4 = Might
        self.assertEqual(eng._lethal_sweep(), 1)


class FrozenFortressTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(A, "triggered_abilities_for", side_effect=_abilities_for)
        p.start()
        self.addCleanup(p.stop)

    def test_beginning_phase_deals_1_to_each_unit_here(self):
        eng = GameEngine(
            game_state=_started(
                battlefield_1="Frozen Fortress",
                player_1_units=[_u(1, uid=1)],            # dies (1 dmg >= 1 Might)
                player_2_units=[_u(2, uid=2), _u(1, location="battlefield_2", uid=3)],  # BF2 unit untouched
            )
        )
        eng._apply_beginning_phase_bf_damage()
        # P1's 1-Might unit at the Fortress is dead; P2's 2-Might survives (1 dmg);
        # P2's unit at BF2 is untouched.
        self.assertEqual(eng._game_state.player_1_units, [])
        self.assertEqual(len(eng._game_state.player_2_units), 2)
        self.assertEqual(eng._game_state.player_2_units[0].damage, 1)
        self.assertEqual(eng._game_state.player_2_units[1].damage, 0)


if __name__ == "__main__":
    unittest.main()
