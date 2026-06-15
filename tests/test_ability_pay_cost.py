"""Energy/Power costs on abilities (beyond EXHAUST_THIS).

Two integration points, both gated on the player being able to pay:
  * a TRIGGERED "you may pay X to get the effect" (drain-time PendingAbilityCost)
    — e.g. Treasure Hoard "when you conquer here, you may pay 1 energy to play a
    Gold token"; and
  * an ACTIVATED ability whose cost includes energy/power — e.g. The Syren
    "1 energy, exhaust: …".

The cost codes parse via csv_data.parse_pay_cost: PAY_<N>_ENERGY (energy),
PAY_<N>P (any-domain power), PAY_<N><LETTER> (power of the card's own domain)."""

import unittest
from unittest import mock

from riftbound_engine import abilities as A
from riftbound_engine.abilities import Ability
from riftbound_engine.csv_data import parse_pay_cost
from riftbound_engine.engine import (
    GameEngine,
    GameState,
    PendingAbilityPayment,
    PlayedGear,
    PlayedUnit,
    RequiredStep,
    RequiredTo,
)
from riftbound_engine.triggers import TriggeredEffect


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


class ParsePayCostTests(unittest.TestCase):
    def test_energy(self):
        req, unsup = parse_pay_cost(("PAY_1_ENERGY",), ("Colorless",))
        self.assertEqual(req, {"energy": 1, "power": {}, "any_power": 0})
        self.assertEqual(unsup, ())

    def test_any_power(self):
        req, _ = parse_pay_cost(("PAY_4P",), ("Colorless",))
        self.assertEqual(req["any_power"], 4)

    def test_colored_rune_uses_card_domain(self):
        # PAY_1R on a Fury card → 1 Fury Power (the colour letter is ignored).
        req, _ = parse_pay_cost(("PAY_1R",), ("Fury",))
        self.assertEqual(req["power"], {"Fury": 1})

    def test_colored_rune_on_colorless_falls_back_to_any(self):
        req, _ = parse_pay_cost(("PAY_1Y",), ("Colorless",))
        self.assertEqual(req["any_power"], 1)
        self.assertEqual(req["power"], {})

    def test_exhaust_is_skipped_not_unsupported(self):
        req, unsup = parse_pay_cost(("EXHAUST_THIS", "PAY_1_ENERGY"), ("Chaos",))
        self.assertEqual(req["energy"], 1)
        self.assertEqual(unsup, ())

    def test_unsupported_costs_reported(self):
        _, unsup = parse_pay_cost(("KILL_THIS", "PAY_1_ENERGY"), ("Order",))
        self.assertEqual(unsup, ("KILL_THIS",))


def _started_state(**kw):
    return GameState(
        started=True, abcd_a_done=True, abcd_b_done=True, abcd_c_done=True, abcd_d_done=True,
        current_player=RequiredTo.PLAYER_1, **kw,
    )


class TriggeredMayPayTests(unittest.TestCase):
    """Drain-time "you may pay 1 energy" gate (Treasure Hoard shape)."""

    def _engine(self, energy):
        gs = _started_state(player_1_energy=energy, battlefield_1="Treasure Hoard")
        eng = GameEngine(game_state=gs)
        eng._trigger_queue = [
            TriggeredEffect(
                controller="player_1", source="battlefield_1",
                trigger="WHEN_CONQUER_HERE", event_kind="ON_CONQUER",
                effects=("PLAY_GOLD_EXHAUSTED",), costs=("PAY_1_ENERGY",),
                label="Treasure Hoard — WHEN_CONQUER_HERE",
            )
        ]
        return eng

    def test_gate_opens_when_affordable(self):
        eng = self._engine(energy=1)
        eng._drain_triggers()
        self.assertIsNotNone(eng._game_state.pending_ability_cost)
        self.assertEqual(eng._game_state.pending_ability_cost.costs, ("PAY_1_ENERGY",))

    def test_no_gate_when_unaffordable(self):
        eng = self._engine(energy=0)
        eng._drain_triggers()
        # Can't pay → the "may pay" ability is dropped, no decision, no effect.
        self.assertIsNone(eng._game_state.pending_ability_cost)
        self.assertIsNone(eng._game_state.pending_chain)

    def test_paying_charges_energy_and_pushes_effect(self):
        eng = self._engine(energy=2)
        eng._drain_triggers()
        eng.apply_action(action="play:choose_ability_cost:yes", actor=RequiredTo.PLAYER_1)
        self.assertEqual(eng._game_state.player_1_energy, 1)  # 2 - 1 paid
        self.assertIsNotNone(eng._game_state.pending_chain)  # effect on the chain

    def test_declining_charges_nothing(self):
        eng = self._engine(energy=2)
        eng._drain_triggers()
        eng.apply_action(action="play:choose_ability_cost:no", actor=RequiredTo.PLAYER_1)
        self.assertEqual(eng._game_state.player_1_energy, 2)  # untouched
        self.assertIsNone(eng._game_state.pending_chain)


# "1 energy, exhaust: give a unit +3 might" — activated, energy + exhaust cost.
SYREN_LIKE = (Ability(costs=("PAY_1_ENERGY", "EXHAUST_THIS"), active_effects=("GIVE_UNIT_+3M",)),)


class ActivateWithEnergyTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(
            A, "triggered_abilities_for",
            side_effect=lambda n: SYREN_LIKE if n == "Heart of Dark Ice" else (),
        )
        p.start(); self.addCleanup(p.stop)

    def _engine(self, energy):
        gs = _started_state(
            player_1_energy=energy,
            player_1_units=[PlayedUnit(card="Plundering Poro", location="base", exhausted=False, uid=1)],
            player_1_gears=[PlayedGear(card="Heart of Dark Ice", location="base", exhausted=False)],
        )
        return GameEngine(game_state=gs)

    def test_activate_charges_energy_and_exhausts(self):
        eng = self._engine(energy=1)
        eng.apply_action(action="play:activate:gear:player_1:0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertTrue(gs.player_1_gears[0].exhausted)
        self.assertEqual(gs.player_1_energy, 0)  # 1 - 1 paid
        self.assertIsNotNone(gs.pending_spell_choice)

    def test_cannot_activate_without_energy(self):
        eng = self._engine(energy=0)
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:activate:gear:player_1:0", actor=RequiredTo.PLAYER_1)
        # Cost not paid → gear stays ready.
        self.assertFalse(eng._game_state.player_1_gears[0].exhausted)


class AnyPowerChoiceTests(unittest.TestCase):
    """"N Power of any type" — the player picks which domain to spend when the
    pool spans more than one domain (Power Nexus shape: pay 4 any → score)."""

    def _engine(self, pool):
        gs = _started_state(battlefield_1="Power Nexus", player_1_power=dict(pool))
        eng = GameEngine(game_state=gs)
        eng._trigger_queue = [
            TriggeredEffect(
                controller="player_1", source="battlefield_1",
                trigger="WHEN_HOLD_HERE", event_kind="ON_HOLD",
                effects=("SCORE_1_POINT",), costs=("PAY_4P",),
                label="Power Nexus — WHEN_HOLD_HERE",
            )
        ]
        return eng

    def test_opens_picker_when_multiple_domains_have_surplus(self):
        # 4 Fury + 4 Order, pay 4 any → real choice of which to drain.
        eng = self._engine({"Fury": 4, "Order": 4})
        eng._drain_triggers()
        eng.apply_action(action="play:choose_ability_cost:yes", actor=RequiredTo.PLAYER_1)
        self.assertIsNotNone(eng._game_state.pending_ability_payment)
        self.assertEqual(eng._game_state.pending_ability_payment.remaining, 4)

    def test_player_picks_domains_then_effect_resolves(self):
        eng = self._engine({"Fury": 4, "Order": 4})
        eng._drain_triggers()
        eng.apply_action(action="play:choose_ability_cost:yes", actor=RequiredTo.PLAYER_1)
        # Pay 3 Fury + 1 Order (player's choice).
        for dom in ("Fury", "Fury", "Fury", "Order"):
            eng.apply_action(action=f"play:pay_ability_power:{dom}", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertIsNone(gs.pending_ability_payment)  # fully paid
        self.assertEqual(gs.player_1_power, {"Fury": 1, "Order": 3})  # spent what they chose
        self.assertIsNotNone(gs.pending_chain)  # SCORE_1_POINT effect on the chain

    def test_rejects_paying_a_domain_with_no_power(self):
        eng = self._engine({"Fury": 4, "Order": 4})
        eng._drain_triggers()
        eng.apply_action(action="play:choose_ability_cost:yes", actor=RequiredTo.PLAYER_1)
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:pay_ability_power:Mind", actor=RequiredTo.PLAYER_1)

    def test_no_picker_when_single_domain(self):
        # Only Fury in pool → no choice; auto-spent, effect resolves immediately.
        eng = self._engine({"Fury": 5})
        eng._drain_triggers()
        eng.apply_action(action="play:choose_ability_cost:yes", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertIsNone(gs.pending_ability_payment)
        self.assertEqual(gs.player_1_power, {"Fury": 1})  # 5 - 4 paid
        self.assertIsNotNone(gs.pending_chain)

    def test_no_picker_when_total_equals_cost(self):
        # 2 Fury + 2 Order, pay 4 → every Power must be spent, no real choice.
        eng = self._engine({"Fury": 2, "Order": 2})
        eng._drain_triggers()
        eng.apply_action(action="play:choose_ability_cost:yes", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertIsNone(gs.pending_ability_payment)
        self.assertEqual(gs.player_1_power, {})  # all spent
        self.assertIsNotNone(gs.pending_chain)

    def test_options_offer_one_pick_per_domain_with_power(self):
        # Drive a real game so start() reaches action-turn options, then inject a
        # pending any-power payment and confirm the picker options surface.
        rolls = iter([6, 2])
        eng = GameEngine(dice_roller=lambda: next(rolls))
        ready = _drive_to_action_turn(eng)
        assert ready.required_action.name == RequiredStep.ACTION_TURN
        gs = eng._game_state
        gs.player_1_power = {"Fury": 4, "Order": 4}
        gs.pending_ability_payment = PendingAbilityPayment(
            actor=RequiredTo.PLAYER_1, kind="triggered", source="battlefield_1",
            effects=("SCORE_1_POINT",), remaining=4,
            trigger="WHEN_HOLD_HERE", event_kind="ON_HOLD", label="Power Nexus",
        )
        out = eng.start()
        self.assertEqual(
            sorted(out.player_1_options),
            ["play:pay_ability_power:Fury", "play:pay_ability_power:Order"],
        )
        self.assertEqual(out.player_2_options, [])


if __name__ == "__main__":
    unittest.main()
