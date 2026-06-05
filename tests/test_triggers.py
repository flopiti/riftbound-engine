"""Tests for the trigger/event system: pure matching, the taxonomy loader,
effect execution, and the full emit → chain → resolve path end to end."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine import abilities as A
from riftbound_engine import effects as E
from riftbound_engine.csv_data import card_type_of, csv_cards
from riftbound_engine.engine import PlayedUnit, Rune
from riftbound_engine.triggers import (
    ON_CONQUER,
    ON_DEATH,
    ON_PLAY_UNIT,
    GameEvent,
    TriggeredEffect,
    trigger_matches,
)


# --------------------------------------------------------------------------- #
# Pure matching — no engine state.
# --------------------------------------------------------------------------- #
class TriggerMatchingTests(unittest.TestCase):
    def test_self_scope_matches_only_the_event_source(self) -> None:
        ev = GameEvent(kind=ON_PLAY_UNIT, controller="player_1", source="player_1:0")
        self.assertTrue(
            trigger_matches("WHEN_YOU_PLAY_ME", ev, owner_controller="player_1", owner_ref="player_1:0")
        )
        # A different unit (not the source) must not fire a SELF trigger.
        self.assertFalse(
            trigger_matches("WHEN_YOU_PLAY_ME", ev, owner_controller="player_1", owner_ref="player_1:1")
        )

    def test_friendly_scope_keys_off_controller(self) -> None:
        ev = GameEvent(kind=ON_PLAY_UNIT, controller="player_1", source="player_1:0")
        self.assertTrue(
            trigger_matches("WHEN_YOU_PLAY_UNIT", ev, owner_controller="player_1", owner_ref="player_1:5")
        )
        self.assertFalse(
            trigger_matches("WHEN_YOU_PLAY_UNIT", ev, owner_controller="player_2", owner_ref="player_2:0")
        )

    def test_enemy_scope_is_the_opposite_of_friendly(self) -> None:
        ev = GameEvent(kind=ON_PLAY_UNIT, controller="player_1", source="player_1:0")
        self.assertTrue(
            trigger_matches("WHEN_OPPONENT_PLAYS_UNIT", ev, owner_controller="player_2", owner_ref="player_2:0")
        )
        self.assertFalse(
            trigger_matches("WHEN_OPPONENT_PLAYS_UNIT", ev, owner_controller="player_1", owner_ref="player_1:1")
        )

    def test_here_scope_keys_off_battlefield_location(self) -> None:
        ev = GameEvent(kind=ON_CONQUER, controller="player_1", battlefield="battlefield_1")
        self.assertTrue(
            trigger_matches("WHEN_CONQUER_HERE", ev, owner_controller="player_2", owner_location="battlefield_1")
        )
        self.assertFalse(
            trigger_matches("WHEN_CONQUER_HERE", ev, owner_controller="player_1", owner_location="base")
        )

    def test_any_scope_fires_regardless(self) -> None:
        ev = GameEvent(kind=ON_DEATH, controller="player_2", source="player_2:0")
        self.assertTrue(
            trigger_matches("IF_UNIT_DIE_COMBAT", ev, owner_controller="player_1", owner_ref="player_1:0")
        )

    def test_unknown_and_mismatched_codes_do_not_match(self) -> None:
        ev = GameEvent(kind=ON_PLAY_UNIT, controller="player_1", source="player_1:0")
        self.assertFalse(trigger_matches("NOT_A_REAL_CODE", ev, owner_controller="player_1"))
        # Right scope, wrong event kind.
        self.assertFalse(trigger_matches("WHEN_I_CONQUER", ev, owner_controller="player_1"))


# --------------------------------------------------------------------------- #
# Taxonomy loader: id-keyed JSON → name-keyed lookup.
# --------------------------------------------------------------------------- #
class AbilitiesLoaderTests(unittest.TestCase):
    def tearDown(self) -> None:
        A.reset_caches()

    def test_loads_abilities_keyed_by_card_name(self) -> None:
        # Pick a real CSV id/name pair so the id→name bridge is exercised.
        id_to_name = A._id_to_name()
        self.assertTrue(id_to_name, "expected the CSV to provide ID→Name mappings")
        some_id, some_name = next(iter(id_to_name.items()))

        taxonomy = {
            "assignments": {
                some_id: {
                    "abilities": [
                        {
                            "triggers": ["WHEN_YOU_PLAY_ME"],
                            "activeEffects": ["DRAW_1"],
                            "conditions": [],
                            "costs": [],
                            "activationSpeeds": [],
                            "passiveEffects": [],
                        }
                    ]
                }
            }
        }
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(taxonomy, f)
            # patch.dict restores the prior environment (incl. conftest's
            # hermetic default) automatically on exit.
            with mock.patch.dict("os.environ", {"RIFTBOUND_TAXONOMY_PATH": path}):
                A.reset_caches()
                abilities = A.triggered_abilities_for(some_name)
            self.assertEqual(len(abilities), 1)
            self.assertEqual(abilities[0].triggers, ("WHEN_YOU_PLAY_ME",))
            self.assertEqual(abilities[0].active_effects, ("DRAW_1",))
        finally:
            os.remove(path)

    def test_missing_file_yields_no_abilities(self) -> None:
        with mock.patch.dict("os.environ", {"RIFTBOUND_TAXONOMY_PATH": "/nonexistent/taxonomy.json"}):
            A.reset_caches()
            self.assertEqual(A.triggered_abilities_for("anything"), ())


# --------------------------------------------------------------------------- #
# Effect registry / executor.
# --------------------------------------------------------------------------- #
class EffectExecutorTests(unittest.TestCase):
    def test_draw_1_moves_a_card_from_library_to_hand(self) -> None:
        engine = GameEngine()
        gs = engine._game_state
        gs.player_1_hand = ["A"]
        gs.player_1_library = ["B", "C"]
        ctx = E.EffectContext(
            engine=engine, controller=RequiredTo.PLAYER_1, source=None, code="DRAW_1"
        )
        self.assertTrue(E.execute_effect(ctx))
        self.assertEqual(gs.player_1_hand, ["A", "B"])
        self.assertEqual(gs.player_1_library, ["C"])

    def test_unimplemented_code_returns_false_without_raising(self) -> None:
        engine = GameEngine()
        ctx = E.EffectContext(
            engine=engine, controller=RequiredTo.PLAYER_1, source=None, code="TOTALLY_MADE_UP"
        )
        self.assertFalse(E.execute_effect(ctx))

    def test_self_buff_increments_bonus_might(self) -> None:
        engine = GameEngine()
        engine._game_state.player_1_units = [PlayedUnit(card="X", location="base")]
        ctx = E.EffectContext(
            engine=engine, controller=RequiredTo.PLAYER_1, source="player_1:0", code="GIVE_ME_+2M"
        )
        self.assertTrue(E.execute_effect(ctx))
        self.assertEqual(engine._game_state.player_1_units[0].bonus_might, 2)


# --------------------------------------------------------------------------- #
# Drain ordering (APNAP) — active player's triggers resolve last (LIFO).
# --------------------------------------------------------------------------- #
class DrainOrderingTests(unittest.TestCase):
    def test_active_player_trigger_goes_to_bottom_of_chain(self) -> None:
        engine = GameEngine()
        engine._game_state.current_player = RequiredTo.PLAYER_1
        engine._trigger_queue = [
            TriggeredEffect(
                controller="player_1", source=None, trigger="T", event_kind="K",
                effects=("DRAW_1",), label="mine",
            ),
            TriggeredEffect(
                controller="player_2", source=None, trigger="T", event_kind="K",
                effects=("DRAW_1",), label="theirs",
            ),
        ]
        engine._drain_triggers()
        chain = engine._game_state.pending_chain
        self.assertIsNotNone(chain)
        # Opponent's trigger ends up on top (index 0, resolves first); the
        # active player's is below it.
        self.assertEqual(chain.items[0].label, "theirs")
        self.assertEqual(chain.items[1].label, "mine")
        self.assertEqual(engine._trigger_queue, [])


# --------------------------------------------------------------------------- #
# End-to-end: play a unit whose ON_PLAY trigger draws a card.
# --------------------------------------------------------------------------- #
def _drive_to_action_turn() -> tuple[GameEngine, object]:
    rolls = iter([6, 2])
    engine = GameEngine(dice_roller=lambda: next(rolls))
    first = engine.start()
    second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
    third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
    fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)
    fifth = engine.apply_action(action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
    engine.apply_action(action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
    engine.apply_action(action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:", actor=RequiredTo.PLAYER_1)
    ready = engine.apply_action(
        action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:", actor=RequiredTo.PLAYER_2
    )
    return engine, ready


def _bank(engine: GameEngine, actor: RequiredTo) -> None:
    engine.runes_for(actor).clear()
    engine.runes_for(actor).extend(Rune(domain="Fury") for _ in range(20))
    engine.add_energy(actor, 99)
    seen = {d for c in csv_cards() for d in __import__("riftbound_engine.csv_data", fromlist=["card_domains_of"]).card_domains_of(c.name) if d}
    for d in sorted(seen):
        engine.add_power(actor, d, 99)


class TriggeredAbilityEndToEndTests(unittest.TestCase):
    def test_on_play_trigger_goes_on_chain_and_resolves_to_draw(self) -> None:
        engine, ready = _drive_to_action_turn()
        _bank(engine, RequiredTo.PLAYER_1)
        ready = engine.start()
        hand = list(ready.game_state.player_1_hand or [])
        unit_idx = next((i for i, c in enumerate(hand) if card_type_of(c) == "Unit"), None)
        if unit_idx is None:
            self.skipTest("dealt hand contains no Unit-type cards")
        unit_card = hand[unit_idx]

        # Force exactly this unit to carry WHEN_YOU_PLAY_ME → DRAW_1.
        stub = (A.Ability(triggers=("WHEN_YOU_PLAY_ME",), active_effects=("DRAW_1",)),)
        with mock.patch.object(
            A, "triggered_abilities_for", side_effect=lambda n: stub if n == unit_card else ()
        ):
            engine.apply_action(action=f"play:play_unit:{unit_idx}", actor=RequiredTo.PLAYER_1)
            settled = engine.apply_action(
                action="play:choose_location:base", actor=RequiredTo.PLAYER_1
            )
            # The triggered ability is now on the chain as an effect item.
            chain = settled.game_state.pending_chain
            self.assertIsNotNone(chain, "playing the unit should open a chain for its trigger")
            top = chain.items[0]
            self.assertIsNotNone(top.effect)
            self.assertIn("DRAW_1", top.effect.effects)

            hand_len_before_resolve = len(engine._game_state.player_1_hand or [])

            # Both players pass priority → the top item resolves and DRAW_1 runs.
            engine.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
            after = engine.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_2)

        self.assertIsNone(after.game_state.pending_chain, "chain should close after resolving")
        self.assertEqual(
            len(engine._game_state.player_1_hand or []),
            hand_len_before_resolve + 1,
            "DRAW_1 should have drawn a card on resolution",
        )
        # The feed recorded the effect resolving.
        texts = " ".join(e.text for e in after.game_state.event_feed)
        self.assertIn("DRAW_1", texts)


if __name__ == "__main__":
    unittest.main()
