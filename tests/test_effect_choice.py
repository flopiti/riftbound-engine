"""Tests for the pending-effect-choice flow: a resolving triggered ability
that needs a player decision (Abandoned Hall's "may give a unit here +1
might this turn") pauses the game, collapses the chooser's options to
``play:choose_effect_target:<token|pass>``, applies the pick, and expires
with the turn."""

from __future__ import annotations

import random
import unittest
from unittest import mock

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine import abilities as A
from riftbound_engine.engine import PlayedUnit
from riftbound_engine.triggers import GameEvent

# Hermetic stand-ins for the authored taxonomy (the suite runs with the real
# taxonomy file disabled — see conftest.py).
_TAXONOMY = {
    "Abandoned Hall": (
        A.Ability(
            triggers=("WHEN_PLAYER_PLAYS_SPELL",),
            active_effects=("MAY_GIVE_UNIT_HERE_+1M",),
        ),
    ),
    "Ravenbloom Student": (
        A.Ability(triggers=("WHEN_YOU_PLAY_SPELL",), active_effects=("GIVE_ME_+1",)),
    ),
}


def _patched_taxonomy():
    return mock.patch.object(
        A, "triggered_abilities_for", side_effect=lambda n: _TAXONOMY.get(n, ())
    )


def _engine_at_hall(p1_units: list[PlayedUnit]) -> GameEngine:
    """A minimal mid-game engine: Abandoned Hall in slot 1, given P1 units,
    P1 active, setup flags forced past ABCD so play: actions dispatch."""
    eng = GameEngine(rng=random.Random(1))
    gs = eng._game_state
    gs.battlefield_1 = "Abandoned Hall"
    gs.current_player = RequiredTo.PLAYER_1
    gs.player_1_units = p1_units
    gs.player_2_units = []
    gs.started = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    return eng


def _fire_spell_trigger(eng: GameEngine, controller: str = "player_1") -> None:
    """Emit ON_PLAY_SPELL and resolve chain items until a choice pauses (or
    the chain drains)."""
    gs = eng._game_state
    with _patched_taxonomy():
        eng._emit(
            GameEvent(kind="ON_PLAY_SPELL", controller=controller, data={"card": "En Garde"})
        )
        eng._drain_triggers()
        chain = gs.pending_chain
        while chain and chain.items and gs.pending_effect_choice is None:
            eng._execute_chain_item(chain.items.pop(0))
        # Mimic _resolve_chain's close so a fully-drained chain doesn't linger.
        if gs.pending_chain is not None and not gs.pending_chain.items:
            gs.pending_chain = None


class EffectChoiceTests(unittest.TestCase):
    def test_no_units_here_fizzles_without_pausing(self) -> None:
        eng = _engine_at_hall([PlayedUnit(card="Scuttle Crab", location="base", exhausted=False)])
        _fire_spell_trigger(eng)
        self.assertIsNone(eng._game_state.pending_effect_choice)

    def test_no_units_here_never_reaches_the_chain(self) -> None:
        # Rule 402.3: with no unit at the Hall, the "may give a unit here +1"
        # ability has no legal choice, so it is dropped as it would go on the
        # chain — it never becomes a Chain Item, opens NO reaction window, and
        # shows nothing. (Previously it went on the chain and fizzled at
        # resolution, briefly offering an empty reaction window.)
        eng = _engine_at_hall([PlayedUnit(card="Scuttle Crab", location="base", exhausted=False)])
        gs = eng._game_state
        with _patched_taxonomy():
            eng._emit(
                GameEvent(kind="ON_PLAY_SPELL", controller="player_1", data={"card": "En Garde"})
            )
            eng._drain_triggers()
        self.assertIsNone(gs.pending_chain, "inert trigger must not create a chain item")
        self.assertIsNone(gs.pending_effect_choice)

    def test_unit_here_still_opens_the_chain(self) -> None:
        # Control case: with a unit at the Hall there IS a legal choice, so the
        # ability goes on the chain as normal (then pauses for the pick).
        eng = _engine_at_hall(
            [PlayedUnit(card="Ravenbloom Student", location="battlefield_1", exhausted=False)]
        )
        gs = eng._game_state
        with _patched_taxonomy():
            eng._emit(
                GameEvent(kind="ON_PLAY_SPELL", controller="player_1", data={"card": "En Garde"})
            )
            eng._drain_triggers()
        self.assertIsNotNone(gs.pending_chain)
        self.assertTrue(gs.pending_chain.items)

    def test_pause_enumerates_controllers_units_here(self) -> None:
        eng = _engine_at_hall(
            [
                PlayedUnit(card="Ravenbloom Student", location="battlefield_1", exhausted=False),
                PlayedUnit(card="Scuttle Crab", location="battlefield_1", exhausted=False),
                PlayedUnit(card="Lonely Poro", location="base", exhausted=False),
            ]
        )
        _fire_spell_trigger(eng)
        choice = eng._game_state.pending_effect_choice
        self.assertIsNotNone(choice)
        self.assertEqual(choice.actor, RequiredTo.PLAYER_1)
        self.assertEqual(choice.options, ["p1-0", "p1-1"])  # not the base unit
        self.assertEqual(choice.source_card, "Abandoned Hall")

    def test_single_unit_still_asks_because_may(self) -> None:
        eng = _engine_at_hall(
            [PlayedUnit(card="Ravenbloom Student", location="battlefield_1", exhausted=False)]
        )
        _fire_spell_trigger(eng)
        self.assertIsNotNone(eng._game_state.pending_effect_choice)

    def test_pick_buffs_only_the_chosen_unit_and_clears(self) -> None:
        eng = _engine_at_hall(
            [
                PlayedUnit(card="Ravenbloom Student", location="battlefield_1", exhausted=False),
                PlayedUnit(card="Scuttle Crab", location="battlefield_1", exhausted=False),
            ]
        )
        _fire_spell_trigger(eng)
        eng.apply_action("play:choose_effect_target:p1-1", RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertEqual(gs.player_1_units[0].bonus_might, 0)
        self.assertEqual(gs.player_1_units[1].bonus_might, 1)
        self.assertIsNone(gs.pending_effect_choice)

    def test_pass_declines_and_clears(self) -> None:
        eng = _engine_at_hall(
            [PlayedUnit(card="Ravenbloom Student", location="battlefield_1", exhausted=False)]
        )
        _fire_spell_trigger(eng)
        eng.apply_action("play:choose_effect_target:pass", RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertEqual(gs.player_1_units[0].bonus_might, 0)
        self.assertIsNone(gs.pending_effect_choice)

    def test_wrong_actor_and_bad_token_rejected_state_intact(self) -> None:
        eng = _engine_at_hall(
            [PlayedUnit(card="Ravenbloom Student", location="battlefield_1", exhausted=False)]
        )
        _fire_spell_trigger(eng)
        with self.assertRaises(ValueError):
            eng.apply_action("play:choose_effect_target:p1-0", RequiredTo.PLAYER_2)
        with self.assertRaises(ValueError):
            eng.apply_action("play:choose_effect_target:p2-0", RequiredTo.PLAYER_1)
        self.assertIsNotNone(eng._game_state.pending_effect_choice)

    def test_options_collapse_to_choice_for_the_chooser(self) -> None:
        # A REAL game driven to the action turn (so start() reaches the
        # action-turn options builder), with the Hall + a unit injected.
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
        eng.apply_action("mulligan_resolve:player_2:", RequiredTo.PLAYER_2)
        gs = eng._game_state
        gs.battlefield_1 = "Abandoned Hall"
        gs.player_1_units = [
            PlayedUnit(card="Ravenbloom Student", location="battlefield_1", exhausted=False)
        ]
        _fire_spell_trigger(eng)
        self.assertIsNotNone(gs.pending_effect_choice)
        out = eng.start()
        self.assertEqual(
            out.player_1_options,
            ["play:choose_effect_target:p1-0", "play:choose_effect_target:pass"],
        )
        self.assertEqual(out.player_2_options, [])

    def test_non_active_chooser_is_let_through(self) -> None:
        # P2 cast the spell on P1's turn — the Hall's "they" is P2, who must
        # be allowed to answer even though they're not the active player.
        eng = _engine_at_hall([])
        gs = eng._game_state
        gs.player_2_units = [
            PlayedUnit(card="Scuttle Crab", location="battlefield_1", exhausted=False)
        ]
        _fire_spell_trigger(eng, controller="player_2")
        choice = gs.pending_effect_choice
        self.assertIsNotNone(choice)
        self.assertEqual(choice.actor, RequiredTo.PLAYER_2)
        eng.apply_action("play:choose_effect_target:p2-0", RequiredTo.PLAYER_2)
        self.assertEqual(gs.player_2_units[0].bonus_might, 1)

    def test_buff_expires_at_end_of_turn(self) -> None:
        # Scuttle Crab has no stub ability, so the chain fully drains and the
        # turn can end right after the choice resolves.
        eng = _engine_at_hall(
            [PlayedUnit(card="Scuttle Crab", location="battlefield_1", exhausted=False)]
        )
        _fire_spell_trigger(eng)
        eng.apply_action("play:choose_effect_target:p1-0", RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertEqual(gs.player_1_units[0].bonus_might, 1)
        eng._advance_turn()
        self.assertEqual(gs.player_1_units[0].bonus_might, 0)


if __name__ == "__main__":
    unittest.main()
