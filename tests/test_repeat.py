"""Tests for the [Repeat] keyword: after a spell's selection round finalizes,
the caster is asked whether to pay the additional Repeat cost and select
again. Costs come from the structured ``Repeat Cost`` CSV column."""

from __future__ import annotations

import unittest

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine.csv_data import card_repeat_cost, card_has_repeat
from riftbound_engine.shortcuts import compute_repeat_intents, execution_steps
from riftbound_engine.engine import (
    GameState,
    PendingShowdown,
    PlayedUnit,
    Rune,
    build_deck_from_id,
)


def _drive_to_action_turn() -> tuple[GameEngine, object]:
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
    eng.apply_action("mulligan_resolve:player_1:", RequiredTo.PLAYER_1)
    ready = eng.apply_action("mulligan_resolve:player_2:", RequiredTo.PLAYER_2)
    return eng, ready


def _bank_energy(eng: GameEngine, actor: RequiredTo, n: int) -> None:
    eng.add_energy(actor, n)


def _give_hand(eng: GameEngine, actor: RequiredTo, cards: list[str]) -> None:
    if actor == RequiredTo.PLAYER_1:
        eng._game_state.player_1_hand = list(cards)
    else:
        eng._game_state.player_2_hand = list(cards)


class RepeatCostDataTests(unittest.TestCase):
    def test_costs_parsed_from_column(self) -> None:
        self.assertEqual(
            card_repeat_cost("Piercing Light"),
            {"energy": 2, "power": {"Fury": 1}, "any_power": 0},
        )
        self.assertEqual(
            card_repeat_cost("Danger Zone"),
            {"energy": 1, "power": {}, "any_power": 1},
        )
        self.assertTrue(card_has_repeat("Blood Rush"))

    def test_non_repeat_and_excluded_costs_are_none(self) -> None:
        self.assertIsNone(card_repeat_cost("Ravenbloom Student"))  # not a spell
        self.assertIsNone(card_repeat_cost("Square Up"))  # discard cost (excluded)
        self.assertIsNone(card_repeat_cost("Curtain Call"))  # multi-option (excluded)


class RepeatFlowTests(unittest.TestCase):
    def test_repeat_offered_after_cast_and_no_finalizes(self) -> None:
        # Downstage Dramatics: Reaction, "Draw 1", Repeat 2 energy, no targets.
        eng, _ = _drive_to_action_turn()
        eng.start()
        _give_hand(eng, RequiredTo.PLAYER_1, ["Downstage Dramatics"])
        _bank_energy(eng, RequiredTo.PLAYER_1, 10)  # base 2 + repeat 2 + slack
        eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
        gs = eng._game_state
        # No target requirement → straight to the repeat decision.
        self.assertIsNotNone(gs.pending_spell_repeat)
        self.assertEqual(gs.pending_spell_repeat.card, "Downstage Dramatics")
        out = eng.start()
        # Decline is the only flat option; the WAY TO PAY is a pre-costed
        # picker chip (a repeat intent), exactly like the initial cast.
        self.assertEqual(out.player_1_options, ["play:choose_repeat:no"])
        self.assertEqual(out.player_2_options, [])
        intents = compute_repeat_intents(eng, RequiredTo.PLAYER_1)
        self.assertEqual(len(intents), 1)
        self.assertTrue(intents[0].combos)  # at least one way to pay
        # Decline → spell on the chain, decision cleared, no repeat rounds.
        eng.apply_action("play:choose_repeat:no", RequiredTo.PLAYER_1)
        self.assertIsNone(gs.pending_spell_repeat)
        self.assertIsNotNone(gs.pending_chain)
        self.assertEqual(gs.pending_chain.items[0].card, "Downstage Dramatics")
        self.assertEqual(gs.pending_chain.items[0].repeat_targets, [])

    def test_yes_repeats_once_then_pushes(self) -> None:
        eng, _ = _drive_to_action_turn()
        eng.start()
        _give_hand(eng, RequiredTo.PLAYER_1, ["Downstage Dramatics"])
        _bank_energy(eng, RequiredTo.PLAYER_1, 6)  # 2 base, then 2 for the repeat
        eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
        gs = eng._game_state
        energy_before = eng.player_energy(RequiredTo.PLAYER_1)  # 6 - 2 base = 4
        self.assertEqual(energy_before, 4)
        eng.apply_action("play:choose_repeat:yes", RequiredTo.PLAYER_1)
        # Repeat cost (2 energy) charged. [Repeat] is single-use, and this
        # spell has no targets, so after the one repeat the spell goes straight
        # onto the chain — NOT re-offered.
        self.assertEqual(eng.player_energy(RequiredTo.PLAYER_1), 2)
        self.assertIsNone(gs.pending_spell_repeat)
        item = gs.pending_chain.items[0]
        self.assertEqual(len(item.repeat_targets), 1)  # 2 resolutions total
        # A second repeat is never offered — there's no pending decision.
        with self.assertRaises(ValueError):
            eng.apply_action("play:choose_repeat:yes", RequiredTo.PLAYER_1)

    def test_yes_unaffordable_is_rejected_and_not_offered(self) -> None:
        eng, _ = _drive_to_action_turn()
        eng.start()
        _give_hand(eng, RequiredTo.PLAYER_1, ["Downstage Dramatics"])
        _bank_energy(eng, RequiredTo.PLAYER_1, 2)  # exactly the base cost
        eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
        out = eng.start()
        # Can't afford the 2-energy repeat → yes not offered, no still is.
        self.assertNotIn("play:choose_repeat:yes", out.player_1_options)
        self.assertIn("play:choose_repeat:no", out.player_1_options)
        with self.assertRaises(ValueError):
            eng.apply_action("play:choose_repeat:yes", RequiredTo.PLAYER_1)

    def test_payment_intent_bundles_rune_steps_and_commit(self) -> None:
        # When the repeat isn't affordable from the pool, the payment chip's
        # chain bundles the rune steps + the commit (the "same shortcut payment
        # way" as the initial cast) — no manual rune tapping.
        eng, _ = _drive_to_action_turn()
        eng.start()
        _give_hand(eng, RequiredTo.PLAYER_1, ["Downstage Dramatics"])
        # Base cost 2 paid at cast, leaving 1; the 2-energy repeat needs runes.
        _bank_energy(eng, RequiredTo.PLAYER_1, 3)
        eng._game_state.player_1_runes = [Rune(domain="Mind", exhausted=False) for _ in range(3)]
        eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
        self.assertEqual(eng.player_energy(RequiredTo.PLAYER_1), 1)

        intents = compute_repeat_intents(eng, RequiredTo.PLAYER_1)
        self.assertEqual(len(intents), 1)
        combo = intents[0].combos[0]
        chain = [a for _, a in execution_steps(combo)]
        # Ends with the commit; includes at least one rune exhaust to make the
        # missing Energy.
        self.assertEqual(chain[-1], "play:choose_repeat:yes")
        self.assertTrue(any(a.startswith("play:exhaust_rune:") for a in chain[:-1]))

        # Applying the bundled chain charges the cost and repeats once; since
        # [Repeat] is single-use (and this spell has no targets), the spell is
        # then pushed onto the chain rather than re-offering the decision.
        for a in chain:
            eng.apply_action(a, RequiredTo.PLAYER_1)
        self.assertIsNone(eng._game_state.pending_spell_repeat)
        item = eng._game_state.pending_chain.items[0]
        self.assertEqual(len(item.repeat_targets), 1)

    def test_wrong_actor_rejected(self) -> None:
        eng, _ = _drive_to_action_turn()
        eng.start()
        _give_hand(eng, RequiredTo.PLAYER_1, ["Downstage Dramatics"])
        _bank_energy(eng, RequiredTo.PLAYER_1, 6)
        eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
        with self.assertRaises(ValueError):
            eng.apply_action("play:choose_repeat:no", RequiredTo.PLAYER_2)

    def test_cannot_end_turn_while_repeat_pending(self) -> None:
        eng, _ = _drive_to_action_turn()
        eng.start()
        _give_hand(eng, RequiredTo.PLAYER_1, ["Downstage Dramatics"])
        _bank_energy(eng, RequiredTo.PLAYER_1, 6)
        eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
        with self.assertRaises(ValueError):
            eng.apply_action("play:end_turn", RequiredTo.PLAYER_1)

    def test_repeat_offered_for_spell_cast_in_a_showdown(self) -> None:
        # Regression: an [Action]/[Reaction] [Repeat] spell cast DURING a
        # showdown must still get its repeat prompt — the showdown menu must
        # not grab the clock before the repeat decision is answered.
        gs = GameState()
        gs.started = True
        gs.is_mulligan_done = True
        gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
        gs.current_player = RequiredTo.PLAYER_1
        gs.player_1_deck = build_deck_from_id("ezreal_prodigal_explorer")
        gs.player_2_deck = build_deck_from_id("irelia_nates")
        gs.battlefield_1 = "Abandoned Hall"
        gs.battlefield_2 = "B"
        gs.player_1_energy = 9
        gs.player_1_hand = ["Frigid Touch"]  # Reaction, "Give a unit -2 might", Repeat 2E
        gs.player_1_units = [PlayedUnit(card="Ravenbloom Student", location="battlefield_1", exhausted=False)]
        gs.player_2_units = [PlayedUnit(card="Scuttle Crab", location="battlefield_1", exhausted=False)]
        gs.pending_showdown = PendingShowdown(
            battlefield="battlefield_1", initiator=RequiredTo.PLAYER_1,
            focus=RequiredTo.PLAYER_1, locked=True,
        )
        eng = GameEngine(game_state=gs)

        eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
        out = eng.start()
        eng.apply_action(out.player_1_options[0], RequiredTo.PLAYER_1)  # pick a target
        self.assertIsNotNone(gs.pending_spell_repeat)
        # Decline flat option + a payment intent (the showdown menu must NOT
        # have grabbed the clock).
        self.assertEqual(eng.start().player_1_options, ["play:choose_repeat:no"])
        self.assertTrue(compute_repeat_intents(eng, RequiredTo.PLAYER_1))

    def test_non_repeat_spell_unaffected(self) -> None:
        # Discipline has no Repeat cost → straight to the chain, no decision.
        eng, _ = _drive_to_action_turn()
        eng.start()
        _give_hand(eng, RequiredTo.PLAYER_1, ["Discipline"])
        _bank_energy(eng, RequiredTo.PLAYER_1, 10)
        # Discipline targets a unit; give P1 one so the requirement is meetable.
        eng._game_state.player_1_units = [
            PlayedUnit(card="Ravenbloom Student", location="base", exhausted=False)
        ]
        eng.apply_action("play:play_spell:0", RequiredTo.PLAYER_1)
        gs = eng._game_state
        # Discipline requires a target pick → spell choice, not repeat.
        self.assertIsNone(gs.pending_spell_repeat)
        self.assertIsNotNone(gs.pending_spell_choice)


if __name__ == "__main__":
    unittest.main()
