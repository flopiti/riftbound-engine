"""Tests for the Spell Choice Requirement parser/evaluator.

Covers every ``ANY UNIT`` codename we started with, the EQUIPMENT
never-valid rule, the min-0 always-offer rule, the AND/OR pipe-run tree
grammar, and the end-to-end ``spell_playable`` path against real card names
from ``riftbound_cards.csv``.

Unit-name fixtures (Might values are read from the CSV by name):
    Watchful Sentry   = 1 Might
    Chemtech Enforcer = 2 Might
    Flame Chompers    = 3 Might
    Blazing Scorcher  = 5 Might
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field

from riftbound_engine.engine import (
    GameEngine,
    GameState,
    PendingShowdown,
    PlayedUnit,
    RequiredTo,
    Rune,
    build_deck_from_id,
)
from riftbound_engine.requirements import (
    enumerate_unit_target_sets,
    parse_phrase,
    parse_tree,
    requirement_satisfiable,
    selectable_unit_requirement,
    spell_playable,
    target_set_satisfies,
)

M1 = "Watchful Sentry"
M2 = "Chemtech Enforcer"
M3 = "Flame Chompers"
M5 = "Blazing Scorcher"


# --- lightweight stand-ins for the engine's GameState/PlayedUnit ----------
@dataclass
class FakeUnit:
    card: str
    location: str = "base"
    exhausted: bool = False


@dataclass
class FakeShowdown:
    initiator: str  # "player_1" / "player_2"
    battlefield: str


@dataclass
class FakeState:
    player_1_units: list = field(default_factory=list)
    player_2_units: list = field(default_factory=list)
    pending_showdown: object | None = None


def state(p1=None, p2=None, showdown=None) -> FakeState:
    return FakeState(
        player_1_units=list(p1 or []),
        player_2_units=list(p2 or []),
        pending_showdown=showdown,
    )


class AnyUnitCountTests(unittest.TestCase):
    def test_any_unit_1_needs_at_least_one_unit(self):
        self.assertFalse(requirement_satisfiable("ANY UNIT (1)", state()))
        self.assertTrue(requirement_satisfiable("ANY UNIT (1)", state(p1=[FakeUnit(M1)])))
        # Either controller's unit counts ("ANY").
        self.assertTrue(requirement_satisfiable("ANY UNIT (1)", state(p2=[FakeUnit(M1)])))

    def test_any_unit_2_needs_two_units(self):
        self.assertFalse(requirement_satisfiable("ANY UNIT (2)", state(p1=[FakeUnit(M1)])))
        self.assertTrue(
            requirement_satisfiable("ANY UNIT (2)", state(p1=[FakeUnit(M1)], p2=[FakeUnit(M2)]))
        )

    def test_any_unit_range_uses_minimum(self):
        # (1-2) has minimum 1 ⇒ one unit suffices, empty board does not.
        self.assertFalse(requirement_satisfiable("ANY UNIT (1-2)", state()))
        self.assertTrue(requirement_satisfiable("ANY UNIT (1-2)", state(p1=[FakeUnit(M1)])))


class MinZeroAlwaysOfferTests(unittest.TestCase):
    """(n) / (0-N) have minimum 0 ⇒ always offered, even on an empty board."""

    def test_n_sum_always_offered(self):
        self.assertTrue(requirement_satisfiable("ANY UNIT (n)[SUM <= 4M]", state()))

    def test_n_bf_always_offered(self):
        self.assertTrue(requirement_satisfiable("ANY UNIT (n)[BF]", state()))

    def test_zero_to_three_same_loc_always_offered(self):
        self.assertTrue(requirement_satisfiable("ANY UNIT (0-3)[SAME_LOC]", state()))


class PerUnitFilterTests(unittest.TestCase):
    def test_bf_requires_unit_at_a_battlefield(self):
        self.assertFalse(
            requirement_satisfiable("ANY UNIT (1[BF])", state(p1=[FakeUnit(M1, "base")]))
        )
        self.assertTrue(
            requirement_satisfiable("ANY UNIT (1[BF])", state(p1=[FakeUnit(M1, "battlefield_1")]))
        )

    def test_base_requires_unit_at_base(self):
        self.assertFalse(
            requirement_satisfiable("ANY UNIT (1[BASE])", state(p1=[FakeUnit(M1, "battlefield_2")]))
        )
        self.assertTrue(
            requirement_satisfiable("ANY UNIT (1[BASE])", state(p1=[FakeUnit(M1, "base")]))
        )

    def test_exhausted_requires_exhausted_unit(self):
        self.assertFalse(
            requirement_satisfiable("ANY UNIT (1[EXHAUSTED])", state(p1=[FakeUnit(M1, exhausted=False)]))
        )
        self.assertTrue(
            requirement_satisfiable("ANY UNIT (1[EXHAUSTED])", state(p1=[FakeUnit(M1, exhausted=True)]))
        )

    def test_might_and_location_combined_le3m_bf(self):
        # <=3M BF: must be at a battlefield AND have <= 3 Might.
        self.assertFalse(  # 5 Might at BF — too big
            requirement_satisfiable("ANY UNIT (1[<=3M BF])", state(p1=[FakeUnit(M5, "battlefield_1")]))
        )
        self.assertFalse(  # 3 Might but at base — not at BF
            requirement_satisfiable("ANY UNIT (1[<=3M BF])", state(p1=[FakeUnit(M3, "base")]))
        )
        self.assertTrue(  # 3 Might at BF — ok
            requirement_satisfiable("ANY UNIT (1[<=3M BF])", state(p1=[FakeUnit(M3, "battlefield_1")]))
        )

    def test_might_and_location_combined_bf_le2m(self):
        # BF <= 2M: token order reversed but same meaning.
        self.assertFalse(
            requirement_satisfiable("ANY UNIT (1[BF <= 2M])", state(p1=[FakeUnit(M3, "battlefield_1")]))
        )
        self.assertTrue(
            requirement_satisfiable("ANY UNIT (1[BF <= 2M])", state(p1=[FakeUnit(M2, "battlefield_1")]))
        )


class AttackingTests(unittest.TestCase):
    def test_attacking_only_during_showdown_for_initiator(self):
        # Units exist but no showdown ⇒ none is "attacking".
        units = [FakeUnit(M3, "battlefield_1")]
        self.assertFalse(requirement_satisfiable("ANY UNIT (1[ATTACKING])", state(p1=units)))
        # Showdown initiated by player_1 at battlefield_1 ⇒ its unit there attacks.
        sd = FakeShowdown(initiator="player_1", battlefield="battlefield_1")
        self.assertTrue(
            requirement_satisfiable("ANY UNIT (1[ATTACKING])", state(p1=units, showdown=sd))
        )
        # A unit belonging to the non-initiator does not count.
        self.assertFalse(
            requirement_satisfiable(
                "ANY UNIT (1[ATTACKING])",
                state(p2=[FakeUnit(M3, "battlefield_1")], showdown=sd),
            )
        )


class EquipmentNeverValidTests(unittest.TestCase):
    def test_equipment_phrase_is_never_satisfiable(self):
        # Even with plenty of units, EQUIPMENT (unmodelled) blocks the card.
        s = state(p1=[FakeUnit(M1), FakeUnit(M2)], p2=[FakeUnit(M3)])
        self.assertFalse(
            requirement_satisfiable("ANY UNIT (1) EQUIPMENT (1) [SAME_CONT]", s)
        )


class GroupConstraintTests(unittest.TestCase):
    """SAME_LOC / SUM only bind once the minimum count is >= 2 (synthetic)."""

    def test_same_loc_needs_min_units_sharing_a_location(self):
        diff = state(p1=[FakeUnit(M1, "base"), FakeUnit(M2, "battlefield_1")])
        same = state(p1=[FakeUnit(M1, "battlefield_1"), FakeUnit(M2, "battlefield_1")])
        self.assertFalse(requirement_satisfiable("ANY UNIT (2)[SAME_LOC]", diff))
        self.assertTrue(requirement_satisfiable("ANY UNIT (2)[SAME_LOC]", same))

    def test_sum_might_cap(self):
        too_big = state(p1=[FakeUnit(M3), FakeUnit(M3)])  # 3+3 = 6 > 4
        ok = state(p1=[FakeUnit(M2), FakeUnit(M2)])  # 2+2 = 4 <= 4
        self.assertFalse(requirement_satisfiable("ANY UNIT (2)[SUM <= 4M]", too_big))
        self.assertTrue(requirement_satisfiable("ANY UNIT (2)[SUM <= 4M]", ok))


class TreeAndUnknownTests(unittest.TestCase):
    def test_blank_requirement_is_satisfiable(self):
        self.assertTrue(requirement_satisfiable("", state()))
        self.assertTrue(requirement_satisfiable(None, state()))

    def test_unknown_selector_defaults_to_satisfiable(self):
        # Not yet handled (no FRIENDLY/ENEMY logic) ⇒ don't block the card.
        self.assertTrue(requirement_satisfiable("FRIENDLY UNIT (1)", state()))

    def test_and_within_group(self):
        raw = "ANY UNIT (1[BF])|ANY UNIT (1)"  # AND
        self.assertFalse(  # only a base unit → BF phrase fails
            requirement_satisfiable(raw, state(p1=[FakeUnit(M1, "base")]))
        )
        self.assertTrue(
            requirement_satisfiable(raw, state(p1=[FakeUnit(M1, "battlefield_1")]))
        )

    def test_or_between_groups(self):
        raw = "ANY UNIT (2)||||ANY UNIT (1)"  # group OR
        # One unit: group1 (needs 2) fails, group2 (needs 1) ok ⇒ OR true.
        self.assertTrue(requirement_satisfiable(raw, state(p1=[FakeUnit(M1)])))
        # No units: both fail ⇒ false.
        self.assertFalse(requirement_satisfiable(raw, state()))

    def test_pipe_run_lengths_parse_into_groups(self):
        tree = parse_tree("ANY UNIT (1)|ANY UNIT (1)|||ANY UNIT (1)")
        self.assertEqual(len(tree.groups), 2)  # split on the |||
        self.assertEqual(len(tree.groups[0].phrases), 2)
        self.assertEqual(tree.connectors, ["AND"])


class PhraseParseTests(unittest.TestCase):
    def test_count_and_filters(self):
        p = parse_phrase("ANY UNIT (1[<=3M BF])")
        self.assertIsNotNone(p.unit)
        self.assertEqual((p.unit.min_count, p.unit.max_count), (1, 1))
        self.assertTrue(p.unit.require_battlefield)
        self.assertEqual(p.unit.might_max, 3)

    def test_n_is_unbounded_min_zero(self):
        p = parse_phrase("ANY UNIT (n)[SUM <= 4M]")
        self.assertEqual(p.unit.min_count, 0)
        self.assertIsNone(p.unit.max_count)
        self.assertEqual(p.unit.sum_might_max, 4)

    def test_equipment_marks_impossible(self):
        p = parse_phrase("ANY UNIT (1) EQUIPMENT (1) [SAME_CONT]")
        self.assertTrue(p.unit.impossible)


class SpellPlayableEndToEndTests(unittest.TestCase):
    """Exercise the CSV lookup + evaluation through real card names."""

    def test_cleave_any_unit_1(self):
        self.assertFalse(spell_playable(state(), "Cleave"))
        self.assertTrue(spell_playable(state(p1=[FakeUnit(M1)]), "Cleave"))

    def test_disintegrate_any_unit_1_bf(self):
        self.assertFalse(spell_playable(state(p1=[FakeUnit(M1, "base")]), "Disintegrate"))
        self.assertTrue(spell_playable(state(p1=[FakeUnit(M1, "battlefield_1")]), "Disintegrate"))

    def test_fox_fire_min_zero_always_playable(self):
        self.assertTrue(spell_playable(state(), "Fox-Fire"))

    def test_angle_shot_equipment_never_playable(self):
        self.assertFalse(spell_playable(state(p1=[FakeUnit(M1)]), "Angle Shot"))


class SelectableRequirementTests(unittest.TestCase):
    def test_single_min1_phrase_is_selectable(self):
        self.assertIsNotNone(selectable_unit_requirement("ANY UNIT (1)"))
        self.assertIsNotNone(selectable_unit_requirement("ANY UNIT (1-2)"))
        self.assertIsNotNone(selectable_unit_requirement("ANY UNIT (1[BF])"))

    def test_min0_and_blank_and_unknown_are_not_selectable(self):
        self.assertIsNone(selectable_unit_requirement(""))
        self.assertIsNone(selectable_unit_requirement(None))
        self.assertIsNone(selectable_unit_requirement("ANY UNIT (n)[SUM <= 4M]"))
        self.assertIsNone(selectable_unit_requirement("ANY UNIT (0-3)[SAME_LOC]"))
        self.assertIsNone(selectable_unit_requirement("FRIENDLY UNIT (1)"))
        self.assertIsNone(selectable_unit_requirement("ANY UNIT (1) EQUIPMENT (1) [SAME_CONT]"))

    def test_multi_phrase_tree_is_not_selectable_yet(self):
        self.assertIsNone(selectable_unit_requirement("ANY UNIT (1)|ANY UNIT (1)"))


class EnumerateAndValidateTests(unittest.TestCase):
    def test_enumerate_single_pick(self):
        req = selectable_unit_requirement("ANY UNIT (1)")
        s = state(p1=[FakeUnit(M1)], p2=[FakeUnit(M2)])
        sets = enumerate_unit_target_sets(req, s)
        self.assertEqual(sorted(sets), [(("player_1", 0),), (("player_2", 0),)])

    def test_enumerate_range_gives_size1_and_size2(self):
        req = selectable_unit_requirement("ANY UNIT (1-2)")
        s = state(p1=[FakeUnit(M1), FakeUnit(M2)])
        sets = enumerate_unit_target_sets(req, s)
        sizes = sorted(len(t) for t in sets)
        self.assertEqual(sizes, [1, 1, 2])  # two singles + one pair

    def test_validate_rejects_bad_and_accepts_good(self):
        req = selectable_unit_requirement("ANY UNIT (1[BF])")
        s = state(p1=[FakeUnit(M1, "battlefield_1"), FakeUnit(M2, "base")])
        self.assertTrue(target_set_satisfies(req, s, [("player_1", 0)]))  # at BF
        self.assertFalse(target_set_satisfies(req, s, [("player_1", 1)]))  # at base
        self.assertFalse(target_set_satisfies(req, s, [("player_1", 9)]))  # missing


def _started_engine(hand, p2hand=(), p1=None, p2=None) -> GameEngine:
    """A minimal engine parked in the action turn (decks set so start()
    clears the setup gates), with BOTH players fully funded so either can
    cast (the opponent needs resources to respond with a Reaction)."""
    gs = GameState()
    gs.started = True
    gs.is_mulligan_done = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    gs.current_player = RequiredTo.PLAYER_1
    gs.player_1_deck = build_deck_from_id("ezreal_prodigal_explorer")
    gs.player_2_deck = build_deck_from_id("irelia_nates")
    gs.battlefield_1 = "A"
    gs.battlefield_2 = "B"
    gs.player_1_hand = list(hand)
    gs.player_2_hand = list(p2hand)
    full_power = {d: 9 for d in ("Fury", "Mind", "Calm", "Body", "Chaos", "Order")}
    gs.player_1_energy = gs.player_2_energy = 9
    gs.player_1_power = dict(full_power)
    gs.player_2_power = dict(full_power)
    gs.player_1_runes = [Rune(domain="Fury") for _ in range(6)]
    gs.player_2_runes = [Rune(domain="Fury") for _ in range(6)]
    gs.player_1_units = list(p1 or [])
    gs.player_2_units = list(p2 or [])
    return GameEngine(game_state=gs)


class SpellChoiceFlowTests(unittest.TestCase):
    def test_cast_enters_pending_then_commit_records_target(self):
        eng = _started_engine(
            ["Cleave"],
            p1=[PlayedUnit(card=M1, location="base")],
            p2=[PlayedUnit(card=M2, location="battlefield_1")],
        )
        # Cleave is offered while units exist.
        self.assertIn("play:play_spell:0", eng.start().player_1_options)

        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        # Parked for target selection — NOT yet on the spell stack.
        self.assertIsNotNone(gs.pending_spell_choice)
        self.assertEqual(gs.pending_spell_choice.card, "Cleave")
        self.assertEqual(gs.player_1_spells, [])

        out = eng.start()
        self.assertEqual(
            sorted(out.player_1_options),
            ["play:choose_spell_targets:p1-0", "play:choose_spell_targets:p2-0"],
        )
        self.assertEqual(out.player_2_options, [])  # opponent has nothing to do

        eng.apply_action(
            action="play:choose_spell_targets:p2-0", actor=RequiredTo.PLAYER_1
        )
        gs = eng._game_state
        # Targets locked → the spell is cast: it shows in the pile/overlay
        # IMMEDIATELY and opens the chain (priority on the caster).
        self.assertIsNone(gs.pending_spell_choice)
        self.assertEqual(len(gs.player_1_spells), 1)
        self.assertEqual(gs.player_1_spells[0].card, "Cleave")
        self.assertEqual(gs.player_1_spells[0].targets, ["player_2:0"])
        self.assertIsNotNone(gs.pending_chain)
        self.assertEqual(gs.pending_chain.priority, RequiredTo.PLAYER_1)
        self.assertEqual([i.card for i in gs.pending_chain.items], ["Cleave"])

        # Both players pass → chain closes; the spell stays in the pile.
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_2)
        gs = eng._game_state
        self.assertIsNone(gs.pending_chain)
        self.assertEqual(len(gs.player_1_spells), 1)
        self.assertEqual(gs.player_1_spells[0].card, "Cleave")

    def test_min_zero_spell_shows_immediately_then_chain_closes(self):
        eng = _started_engine(["Fox-Fire"])
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        # No target pick → cast immediately: in the pile AND on the chain.
        self.assertIsNone(gs.pending_spell_choice)
        self.assertEqual([s.card for s in gs.player_1_spells], ["Fox-Fire"])
        self.assertEqual([i.card for i in gs.pending_chain.items], ["Fox-Fire"])
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_2)
        self.assertIsNone(eng._game_state.pending_chain)
        self.assertEqual([s.card for s in eng._game_state.player_1_spells], ["Fox-Fire"])

    def test_range_spell_enumerates_single_and_pair(self):
        eng = _started_engine(
            ["Falling Star"],
            p1=[PlayedUnit(card=M1, location="base"), PlayedUnit(card=M2, location="base")],
        )
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        opts = eng.start().player_1_options
        self.assertIn("play:choose_spell_targets:p1-0", opts)
        self.assertIn("play:choose_spell_targets:p1-1", opts)
        self.assertIn("play:choose_spell_targets:p1-0,p1-1", opts)

    def test_invalid_target_pick_rejected(self):
        eng = _started_engine(
            ["Falling Star"], p1=[PlayedUnit(card=M1, location="base")]
        )
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        with self.assertRaises(ValueError):
            eng.apply_action(
                action="play:choose_spell_targets:p1-9", actor=RequiredTo.PLAYER_1
            )
        # Still pending — the bad pick didn't commit or clear.
        self.assertIsNotNone(eng._game_state.pending_spell_choice)

    def test_other_plays_blocked_while_choosing(self):
        eng = _started_engine(
            ["Cleave", "Fox-Fire"], p1=[PlayedUnit(card=M1, location="base")]
        )
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        # Can't cast another spell mid-choice.
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)


class ChainPriorityTests(unittest.TestCase):
    """The priority stack opened whenever a spell is played."""

    def test_cast_opens_chain_with_priority_to_caster(self):
        eng = _started_engine(["Fox-Fire"])
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertIsNotNone(gs.pending_chain)
        self.assertEqual(gs.pending_chain.priority, RequiredTo.PLAYER_1)
        self.assertEqual(gs.pending_chain.consecutive_passes, 0)
        # Caster's menu collapses to react-or-pass; opponent has nothing.
        out = eng.start()
        self.assertIn("play:pass_priority", out.player_1_options)
        self.assertEqual(out.player_2_options, [])

    def test_pass_hands_priority_to_opponent_then_resolves(self):
        eng = _started_engine(["Fox-Fire"])
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertEqual(gs.pending_chain.priority, RequiredTo.PLAYER_2)
        self.assertEqual(gs.pending_chain.consecutive_passes, 1)
        # Opponent now holds priority in the option list.
        self.assertIn("play:pass_priority", eng.start().player_2_options)
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_2)
        self.assertIsNone(eng._game_state.pending_chain)  # both passed → resolved

    def test_wrong_player_cannot_pass(self):
        eng = _started_engine(["Fox-Fire"])
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        # P2 doesn't hold priority yet.
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_2)

    def test_opponent_may_respond_with_reaction_only(self):
        # P1 casts, passes; P2 (priority) may play a [Reaction] but not a
        # non-Reaction spell.
        eng = _started_engine(["Fox-Fire"], p2hand=["Shakedown", "Falling Comet"])
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        opts = eng.start().player_2_options
        # Shakedown is [Reaction]; Falling Comet is not.
        self.assertIn("play:play_spell:0", opts)  # Shakedown at index 0
        self.assertNotIn("play:play_spell:1", opts)  # Falling Comet gated out
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:play_spell:1", actor=RequiredTo.PLAYER_2)

    def test_reaction_resets_pass_count_and_stacks(self):
        eng = _started_engine(["Fox-Fire"], p2hand=["Shakedown"])
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_2)  # react
        gs = eng._game_state
        self.assertEqual([i.card for i in gs.pending_chain.items], ["Shakedown", "Fox-Fire"])
        self.assertEqual(gs.pending_chain.priority, RequiredTo.PLAYER_2)
        self.assertEqual(gs.pending_chain.consecutive_passes, 0)  # reset on add
        # Resolve: both pass → both spells land in their casters' piles.
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_2)
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertIsNone(gs.pending_chain)
        self.assertEqual([s.card for s in gs.player_1_spells], ["Fox-Fire"])
        self.assertEqual([s.card for s in gs.player_2_spells], ["Shakedown"])

    def test_cannot_end_turn_or_move_while_chain_open(self):
        eng = _started_engine(
            ["Fox-Fire"], p1=[PlayedUnit(card=M1, location="base", exhausted=False)]
        )
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_1)
        with self.assertRaises(ValueError):
            eng.apply_action(
                action="play:move_unit:0:battlefield_1", actor=RequiredTo.PLAYER_1
            )


class ContestedCombatRegressionTests(unittest.TestCase):
    """Regression: resolving a showdown where BOTH players have units at the
    battlefield must open PendingCombat without crashing. This contested path
    (the advanced auto-pilot's stop condition) was unreferenced by any test
    and had a latent NameError (`RequiredTo` vs the local `RT` alias)."""

    def test_contested_showdown_opens_combat(self):
        eng = _started_engine(
            [],
            p1=[PlayedUnit(card=M1, location="battlefield_1", exhausted=True)],  # Might 1
            p2=[PlayedUnit(card=M2, location="battlefield_1", exhausted=True)],  # Might 2
        )
        eng._game_state.pending_showdown = PendingShowdown(
            battlefield="battlefield_1", initiator=RequiredTo.PLAYER_1
        )
        # Initiator passes, then opponent — both units present ⇒ contested ⇒
        # PendingCombat with each side's total Might as its damage budget.
        eng.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_2)
        pc = eng._game_state.pending_combat
        self.assertIsNotNone(pc)
        self.assertEqual(pc.battlefield, "battlefield_1")
        self.assertEqual(pc.player_1_might, 1)
        self.assertEqual(pc.player_2_might, 2)
        self.assertIsNone(eng._game_state.pending_showdown)

    def test_non_active_player_can_assign_damage(self):
        # current_player is player_1; the DEFENDER (player_2, non-active) must
        # be allowed to assign damage too, or a contested combat can never
        # resolve. P1 has only 1 Might (can't kill the 2-Might enemy → kills
        # nothing); P2 has 2 Might (must kill the 1-Might enemy).
        eng = _started_engine(
            [],
            p1=[PlayedUnit(card=M1, location="battlefield_1", exhausted=True)],  # Might 1
            p2=[PlayedUnit(card=M2, location="battlefield_1", exhausted=True)],  # Might 2
        )
        eng._game_state.pending_showdown = PendingShowdown(
            battlefield="battlefield_1", initiator=RequiredTo.PLAYER_1
        )
        eng.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_2)
        self.assertIsNotNone(eng._game_state.pending_combat)
        # P1 can't afford the 2-Might unit → "kill nothing" is the only legal
        # assignment for them. P2 is FORCED to kill the 1-Might unit (leftover
        # would otherwise be enough to kill it). The non-active player (P2)
        # must be accepted as actor.
        eng.apply_action(action="play:assign_damage:", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:assign_damage:0", actor=RequiredTo.PLAYER_2)
        self.assertIsNone(eng._game_state.pending_combat)
        # P1's M1 unit died and went to P1's trash; P2's M2 survived.
        self.assertEqual([u.card for u in eng._game_state.player_1_units], [])
        self.assertEqual(eng._game_state.player_1_trash, [M1])
        self.assertEqual([u.card for u in eng._game_state.player_2_units], [M2])
        self.assertEqual(eng._game_state.player_2_trash, [])

    def test_assign_damage_rejects_kill_nothing_when_a_kill_is_forced(self):
        # P2 has 2 Might vs a single 1-Might enemy — leaving it alive wastes
        # 2 damage that must be assigned as lethal. "kill nothing" is illegal.
        eng = _started_engine(
            [],
            p1=[PlayedUnit(card=M1, location="battlefield_1", exhausted=True)],
            p2=[PlayedUnit(card=M2, location="battlefield_1", exhausted=True)],
        )
        eng._game_state.pending_showdown = PendingShowdown(
            battlefield="battlefield_1", initiator=RequiredTo.PLAYER_1
        )
        eng.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_2)
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:assign_damage:", actor=RequiredTo.PLAYER_2)

    def test_assign_damage_options_enumerate_maximal_kill_sets(self):
        # 5 damage vs two 3-Might enemies: you can kill exactly ONE (whichever),
        # never both (needs 6) and never neither (leftover 5 ≥ 3). With 6
        # damage you must kill BOTH (leftover after one kill is 3 ≥ 3).
        from riftbound_engine.engine import PendingCombat
        eng = _started_engine(
            [],
            p2=[
                PlayedUnit(card=M3, location="battlefield_1", exhausted=True),  # Might 3
                PlayedUnit(card=M3, location="battlefield_1", exhausted=True),  # Might 3
            ],
        )
        gs = eng._game_state
        gs.pending_combat = PendingCombat(
            battlefield="battlefield_1",
            player_1_might=5,
            player_2_might=0,
            player_2_targets=[],  # P2 has no units → auto-commit nothing
        )
        opts = set(eng._assign_damage_options(RequiredTo.PLAYER_1))
        self.assertEqual(opts, {"play:assign_damage:0", "play:assign_damage:1"})

        gs.pending_combat = PendingCombat(
            battlefield="battlefield_1",
            player_1_might=6,
            player_2_might=0,
            player_2_targets=[],
        )
        opts = set(eng._assign_damage_options(RequiredTo.PLAYER_1))
        self.assertEqual(opts, {"play:assign_damage:0,1"})


if __name__ == "__main__":
    unittest.main()
