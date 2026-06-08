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
    PlayedGear,
    PlayedUnit,
    RequiredTo,
    Rune,
    build_deck_from_id,
)
from riftbound_engine.requirements import (
    battlefield_picks,
    gear_picks,
    location_picks,
    spell_picks,
    trash_picks,
    enumerate_unit_target_sets,
    moved_unit_refs,
    parse_phrase,
    parse_tree,
    plan_feasible,
    requirement_satisfiable,
    selectable_unit_requirement,
    spell_playable,
    spell_target_plan,
    target_set_satisfies,
)

M0 = "Scuttle Crab"  # 0 Might — lethal threshold is max(0, 1) = 1
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
        # Selectors we don't model yet (ABILITY, …) ⇒ don't block the card.
        # (ANY SPELL / GEAR / BATTLEFIELD / TRASH are now handled.)
        self.assertTrue(requirement_satisfiable("ENEMY ABILITY", state()))

    def test_and_within_group(self):
        # AND within a group forces a DISTINCT pick per phrase: a BF unit for
        # the first phrase AND a separate unit for the second.
        raw = "ANY UNIT (1[BF])|ANY UNIT (1)"  # AND
        self.assertFalse(  # only a base unit → BF phrase fails outright
            requirement_satisfiable(raw, state(p1=[FakeUnit(M1, "base")]))
        )
        self.assertFalse(  # a single BF unit can't satisfy BOTH picks
            requirement_satisfiable(raw, state(p1=[FakeUnit(M1, "battlefield_1")]))
        )
        self.assertTrue(  # a BF unit plus a second distinct unit ⇒ ok
            requirement_satisfiable(
                raw, state(p1=[FakeUnit(M1, "battlefield_1"), FakeUnit(M2, "base")])
            )
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
        self.assertIsNone(selectable_unit_requirement("ANY SPELL"))  # unknown selector
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
        # Meditation is a [Reaction] with no target requirement (keeps this
        # test about priority, not target selection); Falling Comet is not a
        # Reaction.
        eng = _started_engine(["Fox-Fire"], p2hand=["Meditation", "Falling Comet"])
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        opts = eng.start().player_2_options
        # Meditation is [Reaction]; Falling Comet is not.
        self.assertIn("play:play_spell:0", opts)  # Meditation at index 0
        self.assertNotIn("play:play_spell:1", opts)  # Falling Comet gated out
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:play_spell:1", actor=RequiredTo.PLAYER_2)

    def test_reaction_resets_pass_count_and_stacks(self):
        # Meditation: [Reaction] with no target requirement, so it stacks on
        # the chain immediately without a target-selection step.
        eng = _started_engine(["Fox-Fire"], p2hand=["Meditation"])
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_2)  # react
        gs = eng._game_state
        self.assertEqual([i.card for i in gs.pending_chain.items], ["Meditation", "Fox-Fire"])
        self.assertEqual(gs.pending_chain.priority, RequiredTo.PLAYER_2)
        self.assertEqual(gs.pending_chain.consecutive_passes, 0)  # reset on add
        # Resolve LIFO: both pass → ONLY the top (Meditation) resolves; the
        # chain shrinks to [Fox-Fire] and priority returns to its owner (P1).
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_2)
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertEqual([i.card for i in gs.pending_chain.items], ["Fox-Fire"])
        self.assertEqual(gs.pending_chain.priority, RequiredTo.PLAYER_1)
        self.assertEqual(gs.pending_chain.consecutive_passes, 0)
        # Both pass again → Fox-Fire (now the top) resolves, chain closes.
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_2)
        gs = eng._game_state
        self.assertIsNone(gs.pending_chain)
        self.assertEqual([s.card for s in gs.player_1_spells], ["Fox-Fire"])
        self.assertEqual([s.card for s in gs.player_2_spells], ["Meditation"])

    def test_chain_resolves_one_item_at_a_time_lifo(self):
        # Build a 3-deep chain and verify it resolves top-first, with priority
        # handed to the OWNER of each newly-revealed top item.
        from riftbound_engine.engine import PendingChain, ChainItem
        eng = _started_engine([])
        gs = eng._game_state
        # Top → bottom: C(p1), B(p2), A(p1).
        gs.pending_chain = PendingChain(
            items=[
                ChainItem(actor=RequiredTo.PLAYER_1, card="C"),
                ChainItem(actor=RequiredTo.PLAYER_2, card="B"),
                ChainItem(actor=RequiredTo.PLAYER_1, card="A"),
            ],
            priority=RequiredTo.PLAYER_1,
            consecutive_passes=2,
        )
        eng._resolve_chain()  # both passed → resolve top C
        self.assertEqual([i.card for i in gs.pending_chain.items], ["B", "A"])
        self.assertEqual(gs.pending_chain.priority, RequiredTo.PLAYER_2)  # B's owner
        self.assertEqual(gs.pending_chain.consecutive_passes, 0)
        gs.pending_chain.consecutive_passes = 2
        eng._resolve_chain()  # resolve top B
        self.assertEqual([i.card for i in gs.pending_chain.items], ["A"])
        self.assertEqual(gs.pending_chain.priority, RequiredTo.PLAYER_1)  # A's owner
        gs.pending_chain.consecutive_passes = 2
        eng._resolve_chain()  # resolve last item A → chain closes
        self.assertIsNone(gs.pending_chain)

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


class ShowdownMusterTests(unittest.TestCase):
    """Moving a unit onto a battlefield you don't control opens an UNLOCKED
    showdown: the initiator may keep mustering more units onto that BF before
    passing, until a card is played (which locks the fight)."""

    def test_initiator_can_muster_multiple_units_before_passing(self):
        eng = _started_engine(
            [],
            p1=[
                PlayedUnit(card=M1, location="base", exhausted=False),
                PlayedUnit(card=M2, location="base", exhausted=False),
            ],
        )
        # First move opens the showdown (BF1 uncontrolled).
        eng.apply_action(action="play:move_unit:0:battlefield_1", actor=RequiredTo.PLAYER_1)
        sd = eng._game_state.pending_showdown
        self.assertIsNotNone(sd)
        self.assertEqual(sd.initiator, RequiredTo.PLAYER_1)
        self.assertFalse(sd.locked)
        # The engine offers the initiator a move to bring the SECOND unit in.
        out = eng.start()
        self.assertIn("play:move_unit:1:battlefield_1", out.player_1_options)
        self.assertEqual(out.player_2_options, [])
        # Muster the second unit — previously this was rejected outright.
        eng.apply_action(action="play:move_unit:1:battlefield_1", actor=RequiredTo.PLAYER_1)
        bf1 = sorted(
            u.card for u in eng._game_state.player_1_units if u.location == "battlefield_1"
        )
        self.assertEqual(bf1, sorted([M1, M2]))
        # Same showdown, still unlocked (no card played yet).
        self.assertIs(eng._game_state.pending_showdown, sd)
        self.assertFalse(eng._game_state.pending_showdown.locked)

    def test_locked_showdown_blocks_further_mustering(self):
        eng = _started_engine(
            [],
            p1=[
                PlayedUnit(card=M1, location="base", exhausted=False),
                PlayedUnit(card=M2, location="base", exhausted=False),
            ],
        )
        eng.apply_action(action="play:move_unit:0:battlefield_1", actor=RequiredTo.PLAYER_1)
        # Simulate "a card was played" → the fight has started.
        eng._game_state.pending_showdown.locked = True
        with self.assertRaises(ValueError):
            eng.apply_action(
                action="play:move_unit:1:battlefield_1", actor=RequiredTo.PLAYER_1
            )

    def test_opponent_cannot_muster_into_initiators_showdown(self):
        eng = _started_engine(
            [],
            p1=[PlayedUnit(card=M1, location="base", exhausted=False)],
            p2=[PlayedUnit(card=M2, location="base", exhausted=False)],
        )
        eng.apply_action(action="play:move_unit:0:battlefield_1", actor=RequiredTo.PLAYER_1)
        # P2 is not the initiator → cannot move units into the showdown.
        with self.assertRaises(ValueError):
            eng.apply_action(
                action="play:move_unit:0:battlefield_1", actor=RequiredTo.PLAYER_2
            )


class MultiPhrasePlanTests(unittest.TestCase):
    """spell_target_plan: how many picks a requirement forces, and which."""

    def test_single_phrase_is_a_one_pick_plan(self):
        self.assertEqual(len(spell_target_plan("ANY UNIT (1)")), 1)
        self.assertEqual(len(spell_target_plan("ANY UNIT (1[BF])")), 1)

    def test_and_within_group_is_multi_pick(self):
        plan = spell_target_plan("ANY UNIT (1[BF])|ANY UNIT (1)")  # Piercing Light
        self.assertEqual(len(plan), 2)
        self.assertTrue(plan[0].require_battlefield)  # first pick is BF-only
        self.assertFalse(plan[1].require_battlefield)  # second is any unit

    def test_and_between_groups_is_multi_pick(self):
        # Icathian Rain: six AND'd groups → six picks.
        raw = "|||".join(["ANY UNIT (1)"] * 6)
        self.assertEqual(len(spell_target_plan(raw)), 6)

    def test_or_anywhere_yields_no_plan(self):
        self.assertEqual(spell_target_plan("ANY UNIT (2)||||ANY UNIT (1)"), [])
        self.assertEqual(spell_target_plan("ANY UNIT (1)||ANY UNIT (1)"), [])

    def test_unknown_and_min0_phrases_are_skipped(self):
        # Fading Memories: the GEAR phrase forces no pick, only the unit does.
        self.assertEqual(len(spell_target_plan("ANY UNIT (1[BF])|GEAR")), 1)
        # All-min0 / unknown → no picks at all.
        self.assertEqual(spell_target_plan("ANY UNIT (n)[SUM <= 4M]"), [])
        self.assertEqual(spell_target_plan("ANY SPELL"), [])  # unknown selector


class DistinctTargetGateTests(unittest.TestCase):
    """The gate must require a DISTINCT unit per pick for multi-phrase spells."""

    def test_two_picks_need_two_distinct_units(self):
        raw = "ANY UNIT (1)|ANY UNIT (1)"  # Defiant Dance
        self.assertFalse(requirement_satisfiable(raw, state(p1=[FakeUnit(M1)])))
        self.assertTrue(
            requirement_satisfiable(raw, state(p1=[FakeUnit(M1), FakeUnit(M2)]))
        )

    def test_bf_then_any_needs_a_second_unit_off_the_chosen_one(self):
        raw = "ANY UNIT (1[BF])|ANY UNIT (1)"  # Piercing Light
        # Only one unit, at a battlefield: the BF phrase eats it, the any
        # phrase has nothing distinct left ⇒ not playable.
        self.assertFalse(
            requirement_satisfiable(raw, state(p1=[FakeUnit(M1, "battlefield_1")]))
        )
        # A BF unit plus a base unit ⇒ a valid disjoint assignment exists.
        self.assertTrue(
            requirement_satisfiable(
                raw, state(p1=[FakeUnit(M1, "battlefield_1"), FakeUnit(M2, "base")])
            )
        )

    def test_plan_feasible_direct(self):
        plan = spell_target_plan("ANY UNIT (1)|ANY UNIT (1)")
        self.assertFalse(plan_feasible(plan, state(p1=[FakeUnit(M1)])))
        self.assertTrue(plan_feasible(plan, state(p1=[FakeUnit(M1), FakeUnit(M2)])))


class ExcludeParamTests(unittest.TestCase):
    def test_enumerate_skips_excluded_refs(self):
        req = selectable_unit_requirement("ANY UNIT (1)")
        s = state(p1=[FakeUnit(M1), FakeUnit(M2)])
        sets = enumerate_unit_target_sets(req, s, exclude={("player_1", 0)})
        self.assertEqual(sets, [(("player_1", 1),)])

    def test_validate_rejects_excluded_ref(self):
        req = selectable_unit_requirement("ANY UNIT (1)")
        s = state(p1=[FakeUnit(M1), FakeUnit(M2)])
        self.assertFalse(
            target_set_satisfies(req, s, [("player_1", 0)], exclude={("player_1", 0)})
        )
        self.assertTrue(
            target_set_satisfies(req, s, [("player_1", 1)], exclude={("player_1", 0)})
        )


class MultiPhraseChoiceFlowTests(unittest.TestCase):
    """End-to-end: a multi-phrase spell asks for one pick per phrase in turn."""

    def test_two_any_unit_picks_resolve_sequentially(self):
        # Defiant Dance = "ANY UNIT (1)|ANY UNIT (1)" → two distinct picks.
        eng = _started_engine(
            ["Defiant Dance"],
            p1=[PlayedUnit(card=M1, location="base"), PlayedUnit(card=M2, location="base")],
        )
        self.assertIn("play:play_spell:0", eng.start().player_1_options)

        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertIsNotNone(gs.pending_spell_choice)
        self.assertEqual(gs.pending_spell_choice.chosen, [])

        # First phrase: either unit may be picked.
        out = eng.start()
        self.assertEqual(
            sorted(out.player_1_options),
            ["play:choose_spell_targets:p1-0", "play:choose_spell_targets:p1-1"],
        )

        # Pick the first unit — still parked, now choosing the second phrase.
        eng.apply_action(action="play:choose_spell_targets:p1-0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertIsNotNone(gs.pending_spell_choice)
        self.assertEqual(gs.pending_spell_choice.chosen, [["p1-0"]])
        self.assertEqual(gs.player_1_spells, [])  # not cast yet

        # Second phrase only offers the remaining (distinct) unit.
        out = eng.start()
        self.assertEqual(out.player_1_options, ["play:choose_spell_targets:p1-1"])

        # Pick it → spell is now cast with BOTH targets, chain opens.
        eng.apply_action(action="play:choose_spell_targets:p1-1", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertIsNone(gs.pending_spell_choice)
        self.assertEqual(len(gs.player_1_spells), 1)
        self.assertEqual(gs.player_1_spells[0].card, "Defiant Dance")
        self.assertEqual(gs.player_1_spells[0].targets, ["player_1:0", "player_1:1"])
        self.assertEqual([i.card for i in gs.pending_chain.items], ["Defiant Dance"])

    def test_bf_phrase_constrains_first_pick(self):
        # Piercing Light = "ANY UNIT (1[BF])|ANY UNIT (1)".
        eng = _started_engine(
            ["Piercing Light"],
            p1=[
                PlayedUnit(card=M1, location="battlefield_1"),  # only BF unit
                PlayedUnit(card=M2, location="base"),
            ],
        )
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        # First phrase is BF-only → just the battlefield unit.
        self.assertEqual(
            eng.start().player_1_options, ["play:choose_spell_targets:p1-0"]
        )
        eng.apply_action(action="play:choose_spell_targets:p1-0", actor=RequiredTo.PLAYER_1)
        # Second phrase: any remaining unit (the base one).
        self.assertEqual(
            eng.start().player_1_options, ["play:choose_spell_targets:p1-1"]
        )
        eng.apply_action(action="play:choose_spell_targets:p1-1", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        # Piercing Light has [Repeat] → it asks whether to repeat after the
        # picks; decline so it lands on the chain with this round's targets.
        self.assertIsNotNone(gs.pending_spell_repeat)
        eng.apply_action(action="play:choose_repeat:no", actor=RequiredTo.PLAYER_1)
        self.assertEqual(gs.player_1_spells[0].targets, ["player_1:0", "player_1:1"])

    def test_multi_phrase_blocked_without_enough_distinct_units(self):
        # Only one unit on board: Defiant Dance needs two distinct ⇒ not offered.
        eng = _started_engine(["Defiant Dance"], p1=[PlayedUnit(card=M1, location="base")])
        self.assertNotIn("play:play_spell:0", eng.start().player_1_options)
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)

    def test_gear_phrase_adds_a_gear_pick_after_the_unit(self):
        # Fading Memories = "ANY UNIT (1[BF])|GEAR" (AND): now needs a BF unit
        # AND a gear. With both on the board the flow is unit-pick then
        # gear-pick, and both land in the recorded targets.
        eng = _started_engine(
            ["Fading Memories"], p1=[PlayedUnit(card=M1, location="battlefield_1")]
        )
        eng._game_state.player_1_gears.append(PlayedGear(card="Boots of Swiftness"))
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertIsNotNone(gs.pending_spell_choice)
        # Unit pick first — still parked (gear pick pending).
        eng.apply_action(action="play:choose_spell_targets:p1-0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertIsNotNone(gs.pending_spell_choice)
        self.assertIn("play:choose_spell_gear:g1-0", eng.start().player_1_options)
        # Gear pick resolves the spell.
        eng.apply_action(action="play:choose_spell_gear:g1-0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertIsNone(gs.pending_spell_choice)
        self.assertEqual(gs.player_1_spells[0].targets, ["player_1:0", "gear:g1-0"])

    def test_gear_required_spell_is_unplayable_with_no_gear(self):
        # No gear on the board ⇒ the AND requirement can't be met ⇒ not offered.
        eng = _started_engine(
            ["Fading Memories"], p1=[PlayedUnit(card=M1, location="battlefield_1")]
        )
        self.assertNotIn("play:play_spell:0", eng.start().player_1_options)


class ShowdownFocusTests(unittest.TestCase):
    """FOCUS is the showdown's priority: the holder may play Action/Reaction
    spells or pass focus; two consecutive focus passes resolve the fight, and
    playing a spell resets the focus-pass counter."""

    def test_two_consecutive_focus_passes_resolve_showdown(self):
        eng = _started_engine(
            [],
            p1=[PlayedUnit(card=M1, location="base", exhausted=False)],
        )
        eng.apply_action(action="play:move_unit:0:battlefield_1", actor=RequiredTo.PLAYER_1)
        # Initiator (focus) passes → focus to P2 (1 pass). Opponent passes →
        # 2 consecutive → resolve. Only P1 has a unit → P1 conquers.
        eng.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        self.assertEqual(eng._game_state.pending_showdown.focus_holder, RequiredTo.PLAYER_2)
        eng.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_2)
        self.assertIsNone(eng._game_state.pending_showdown)
        self.assertEqual(eng._game_state.battlefield_1_controller, RequiredTo.PLAYER_1)

    def test_playing_a_spell_locks_and_resets_focus_passes(self):
        eng = _started_engine(
            ["Confront"],  # [Action], 2 energy, no target requirement
            p1=[
                PlayedUnit(card=M1, location="base", exhausted=False),
                PlayedUnit(card=M2, location="base", exhausted=False),
            ],
        )
        eng._game_state.player_1_energy = 2
        eng.apply_action(action="play:move_unit:0:battlefield_1", actor=RequiredTo.PLAYER_1)
        # Pass focus once (counter = 1), then it comes back via opponent... but
        # instead the initiator (still focus before passing) plays a spell:
        out = eng.start()
        self.assertIn("play:play_spell:0", out.player_1_options)
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        sd = eng._game_state.pending_showdown
        self.assertTrue(sd.locked)
        self.assertEqual(sd.focus_passes, 0)
        # Casting opened a chain; mustering is now locked out.
        self.assertIsNotNone(eng._game_state.pending_chain)
        with self.assertRaises(ValueError):
            eng.apply_action(
                action="play:move_unit:1:battlefield_1", actor=RequiredTo.PLAYER_1
            )

    def test_defender_holding_focus_may_pass(self):
        eng = _started_engine(
            [],
            p1=[PlayedUnit(card=M1, location="base", exhausted=False)],
        )
        eng.apply_action(action="play:move_unit:0:battlefield_1", actor=RequiredTo.PLAYER_1)
        eng.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        # Focus now with P2 (the non-active defender) — they're offered pass.
        out = eng.start()
        self.assertEqual(out.player_1_options, [])
        self.assertIn("play:pass_showdown", out.player_2_options)
        self.assertEqual(out.required_action.actor, RequiredTo.PLAYER_2)


class FriendlyEnemySideTests(unittest.TestCase):
    """FRIENDLY / ENEMY scopes resolve relative to the casting player."""

    def test_parse_sets_side(self):
        self.assertEqual(parse_phrase("FRIENDLY UNIT (1)").unit.side, "friendly")
        self.assertEqual(parse_phrase("ENEMY UNIT (1[BF])").unit.side, "enemy")
        self.assertIsNone(parse_phrase("ANY UNIT (1)").unit.side)

    def test_move_selectors_are_handled_as_movable_units(self):
        # MOVE FRIENDLY/ENEMY UNIT now parses as a unit selector (with the
        # move flag) so the card is gated on a matching unit existing; the
        # destination pick is collected separately.
        p = parse_phrase("MOVE FRIENDLY UNIT (1)")
        self.assertFalse(p.unknown)
        self.assertIsNotNone(p.unit)
        self.assertTrue(p.unit.move)
        self.assertEqual(p.unit.side, "friendly")
        self.assertEqual(parse_phrase("MOVE ENEMY UNIT (1)").unit.side, "enemy")

    def test_trash_selectors_are_handled(self):
        # Trash-zone targeting now parses into a TrashRequirement with side,
        # unit-only, and an optional energy cap. MOVE-from-trash still needs a
        # destination namespace we don't model, so it stays deferred.
        u = parse_phrase("FRIENDLY TRASH UNIT (1)")
        self.assertFalse(u.unknown)
        self.assertEqual(u.trash.side, "friendly")
        self.assertTrue(u.trash.unit_only)
        self.assertEqual(parse_phrase("FRIENDLY TRASH (1[<= 2E])").trash.energy_max, 2)
        self.assertEqual(parse_phrase("FRIENDLY TRASH (0-2)").trash.min_count, 0)
        self.assertTrue(parse_phrase("MOVE FRIENDLY TRASH UNIT (1)").unknown)

    def test_move_requirement_is_gated_on_a_matching_unit(self):
        # MOVE gates exactly like the underlying selector: a matching unit must
        # exist (the free destination always exists, so it adds no constraint).
        self.assertTrue(
            requirement_satisfiable(
                "MOVE FRIENDLY UNIT (1)", state(p1=[FakeUnit(M1)]), caster="player_1"
            )
        )
        self.assertFalse(
            requirement_satisfiable(
                "MOVE FRIENDLY UNIT (1)", state(p2=[FakeUnit(M1)]), caster="player_1"
            )
        )
        self.assertTrue(
            requirement_satisfiable(
                "MOVE ENEMY UNIT (1)", state(p2=[FakeUnit(M1)]), caster="player_1"
            )
        )

    def test_moved_unit_refs_picks_out_move_phrases(self):
        # A MOVE phrase's picked units are reported (they each need a
        # destination); a non-MOVE phrase's picks are not.
        self.assertEqual(
            moved_unit_refs("MOVE FRIENDLY UNIT (1)", [["p1-0"]]), [("player_1", 0)]
        )
        self.assertEqual(moved_unit_refs("FRIENDLY UNIT (1)", [["p1-0"]]), [])

    def test_battlefield_phrase_is_handled(self):
        self.assertFalse(parse_phrase("BATTLEFIELD").unknown)
        self.assertIsNotNone(parse_phrase("BATTLEFIELD").battlefield)
        self.assertFalse(parse_phrase("BATTLEFIELD").battlefield.where_friendly)
        self.assertTrue(
            parse_phrase("BATTLEFIELD[WHERE_FRIENDLY_UNITS]").battlefield.where_friendly
        )

    def test_plain_battlefield_is_always_satisfiable(self):
        self.assertTrue(requirement_satisfiable("BATTLEFIELD", state(), caster="player_1"))

    def test_where_friendly_units_needs_a_friendly_battlefield_unit(self):
        on_bf = state(p1=[FakeUnit(M1, "battlefield_1")])
        self.assertTrue(
            requirement_satisfiable(
                "BATTLEFIELD[WHERE_FRIENDLY_UNITS]", on_bf, caster="player_1"
            )
        )
        # Caster's unit is at base, not a battlefield → not satisfiable.
        at_base = state(p1=[FakeUnit(M1, "base")])
        self.assertFalse(
            requirement_satisfiable(
                "BATTLEFIELD[WHERE_FRIENDLY_UNITS]", at_base, caster="player_1"
            )
        )
        # The opponent has a battlefield unit, but that's not the caster's.
        enemy_bf = state(p2=[FakeUnit(M1, "battlefield_1")])
        self.assertFalse(
            requirement_satisfiable(
                "BATTLEFIELD[WHERE_FRIENDLY_UNITS]", enemy_bf, caster="player_1"
            )
        )

    def test_battlefield_picks_counts_phrases(self):
        self.assertEqual(len(battlefield_picks("BATTLEFIELD")), 1)
        self.assertEqual(len(battlefield_picks("FRIENDLY UNIT (1[BASE])|BATTLEFIELD")), 1)
        self.assertEqual(len(battlefield_picks("FRIENDLY UNIT (1)")), 0)

    def test_gear_phrase_is_handled(self):
        self.assertFalse(parse_phrase("GEAR").unknown)
        self.assertEqual(parse_phrase("GEAR").gear.min_count, 1)
        self.assertEqual(parse_phrase("GEAR (0-1)").gear.min_count, 0)

    def test_gear_requirement_gated_on_a_gear_existing(self):
        # GEAR (min 1) needs a gear on the board; either player's counts.
        on_board = state(); on_board.player_2_gears = [FakeUnit("Boots of Swiftness")]
        self.assertTrue(requirement_satisfiable("GEAR", on_board, caster="player_1"))
        self.assertFalse(requirement_satisfiable("GEAR", state(), caster="player_1"))
        # GEAR (0-1) is optional → always satisfiable.
        self.assertTrue(requirement_satisfiable("GEAR (0-1)", state(), caster="player_1"))

    def test_gear_picks_only_counts_forced_gear_phrases(self):
        self.assertEqual(len(gear_picks("GEAR")), 1)
        self.assertEqual(len(gear_picks("ANY UNIT (1[BF])|GEAR")), 1)
        self.assertEqual(len(gear_picks("GEAR (0-1)")), 0)  # optional, no forced pick

    def test_trash_unit_requirement_gated_on_a_unit_in_trash(self):
        with_unit = state()
        with_unit.player_1_trash = ["Lonely Poro"]  # a Unit
        self.assertTrue(
            requirement_satisfiable("FRIENDLY TRASH UNIT (1)", with_unit, caster="player_1")
        )
        gear_only = state()
        gear_only.player_1_trash = ["Boots of Swiftness"]  # a Gear, not a Unit
        self.assertFalse(
            requirement_satisfiable("FRIENDLY TRASH UNIT (1)", gear_only, caster="player_1")
        )
        # Enemy scope reads the OPPONENT's trash, not the caster's.
        self.assertFalse(
            requirement_satisfiable("ENEMY TRASH UNIT (1)", with_unit, caster="player_1")
        )

    def test_trash_picks_only_counts_forced_trash_phrases(self):
        self.assertEqual(len(trash_picks("FRIENDLY TRASH UNIT (1)")), 1)
        self.assertEqual(len(trash_picks("FRIENDLY TRASH (0-2)")), 0)  # optional

    def test_spell_phrase_gated_on_a_matching_chain_spell(self):
        from types import SimpleNamespace as NS

        def chain_state(items):
            ci = [NS(actor=NS(value=a), card=c) for a, c in items]
            base = state()
            base.pending_chain = NS(items=ci)
            return base

        # ENEMY SPELL needs an OPPONENT's spell on the chain.
        opp = chain_state([("player_2", "En Garde")])
        self.assertTrue(requirement_satisfiable("ENEMY SPELL", opp, caster="player_1"))
        self.assertFalse(requirement_satisfiable("ENEMY SPELL", opp, caster="player_2"))
        # Empty chain ⇒ nothing to target.
        self.assertFalse(requirement_satisfiable("ANY SPELL", state(), caster="player_1"))

    def test_spell_cost_filter_caps_target_energy_and_power(self):
        self.assertEqual(parse_phrase("ANY SPELL (<=4E AND <=1P)").spell.energy_max, 4)
        self.assertEqual(parse_phrase("ANY SPELL (<=4E AND <=1P)").spell.power_max, 1)

    def test_spell_picks_skips_or_and_ability(self):
        self.assertEqual(len(spell_picks("ANY SPELL")), 1)
        self.assertEqual(len(spell_picks("FRIENDLY UNIT (1)|ANY SPELL")), 1)
        # OR'd requirement (and the ABILITY half we don't model) forces no pick.
        self.assertEqual(
            len(spell_picks("ENEMY SPELL[CHOOSE_FRIENDLY]||ENEMY ABILITY[CHOOSE_FRIENDLY]")), 0
        )

    def test_location_phrase_is_handled_and_always_satisfiable(self):
        self.assertIsNotNone(parse_phrase("LOCATION").location)
        self.assertTrue(requirement_satisfiable("LOCATION", state(), caster="player_1"))
        # The optional ENEMY UNITS half forces no pick; only LOCATION does.
        self.assertEqual(len(location_picks("ENEMY UNITS (n) [SUM <= 8M]|LOCATION")), 1)


class EquipmentTests(unittest.TestCase):
    def test_card_is_equipment_and_cost_parse(self):
        from riftbound_engine.csv_data import card_equip_cost, card_is_equipment

        self.assertTrue(card_is_equipment("Serrated Dirk"))
        self.assertFalse(card_is_equipment("Lonely Poro"))
        self.assertEqual(card_equip_cost("Serrated Dirk"), {"energy": 0, "power": {"Fury": 1}, "any_power": 0})
        self.assertEqual(
            card_equip_cost("Skyfall of Areion"), {"energy": 1, "power": {"Fury": 1}, "any_power": 0}
        )
        # "1 rune of any type" → any_power; exotic clauses ignored.
        self.assertEqual(card_equip_cost("Spinning Axe")["any_power"], 1)

    def test_equip_is_offered_as_an_intent_and_action_pays(self):
        from riftbound_engine.engine import PlayedGear, PlayedUnit
        from riftbound_engine.shortcuts import compute_equip_intents

        eng = _started_engine([], p1=[PlayedUnit(card=M1, location="base")])
        gs = eng._game_state
        gs.player_1_gears.append(PlayedGear(card="Serrated Dirk"))  # [Equip] 1 Fury
        gs.player_1_power = {"Fury": 1}
        # Equip is surfaced as a pre-costed intent chip, NOT a flat option.
        self.assertEqual(
            [o for o in eng.start().player_1_options if o.startswith("play:equip")], []
        )
        intents = compute_equip_intents(eng, RequiredTo.PLAYER_1)
        self.assertTrue(any(i.card_name.startswith("Equip Serrated Dirk") for i in intents))
        # The raw action still validates, pays, and attaches.
        eng.apply_action(action="play:equip:0:player_1:0", actor=RequiredTo.PLAYER_1)
        self.assertEqual(gs.player_1_gears[0].attached_to, "player_1:0")
        self.assertEqual(eng.player_power(RequiredTo.PLAYER_1).get("Fury", 0), 0)

    def test_equip_intent_chain_recycles_a_rune_to_pay(self):
        from riftbound_engine.engine import PlayedGear, PlayedUnit, Rune
        from riftbound_engine.shortcuts import compute_equip_intents

        eng = _started_engine([], p1=[PlayedUnit(card=M1, location="base")])
        gs = eng._game_state
        gs.player_1_gears.append(PlayedGear(card="Serrated Dirk"))
        gs.player_1_power = {}
        gs.player_1_runes = [Rune(domain="Fury")]  # ready Fury rune, no banked power
        gs.player_1_rune_library = []  # recycle appends the spent rune here
        intents = compute_equip_intents(eng, RequiredTo.PLAYER_1)
        self.assertEqual(len(intents), 1)
        combo = intents[0].combos[0]
        from riftbound_engine.shortcuts import execution_steps

        for _, act in execution_steps(combo):
            eng.apply_action(action=act, actor=RequiredTo.PLAYER_1)
        self.assertEqual(gs.player_1_gears[0].attached_to, "player_1:0")
        self.assertEqual(len(gs.player_1_runes), 0)  # the rune was recycled to pay

    def test_friendly_matches_only_the_casters_units(self):
        s = state(p1=[FakeUnit(M1)], p2=[FakeUnit(M2)])
        self.assertTrue(requirement_satisfiable("FRIENDLY UNIT (1)", s, caster="player_1"))
        self.assertTrue(requirement_satisfiable("FRIENDLY UNIT (1)", s, caster="player_2"))
        # Caster owns no units (only the opponent does) ⇒ not satisfiable.
        self.assertFalse(
            requirement_satisfiable(
                "FRIENDLY UNIT (1)", state(p2=[FakeUnit(M2)]), caster="player_1"
            )
        )

    def test_enemy_matches_only_the_opponents_units(self):
        self.assertFalse(
            requirement_satisfiable(
                "ENEMY UNIT (1)", state(p1=[FakeUnit(M1)]), caster="player_1"
            )
        )
        self.assertTrue(
            requirement_satisfiable(
                "ENEMY UNIT (1)", state(p2=[FakeUnit(M2)]), caster="player_1"
            )
        )
        # From player_2's seat, player_1's unit is the enemy.
        self.assertTrue(
            requirement_satisfiable(
                "ENEMY UNIT (1)", state(p1=[FakeUnit(M1)]), caster="player_2"
            )
        )

    def test_enemy_bf_combines_side_and_per_unit_filter(self):
        self.assertFalse(
            requirement_satisfiable(
                "ENEMY UNIT (1[BF])", state(p2=[FakeUnit(M2, "base")]), caster="player_1"
            )
        )
        self.assertTrue(
            requirement_satisfiable(
                "ENEMY UNIT (1[BF])",
                state(p2=[FakeUnit(M2, "battlefield_1")]),
                caster="player_1",
            )
        )

    def test_caster_accepts_required_to_enum(self):
        s = state(p2=[FakeUnit(M2)])
        self.assertTrue(
            requirement_satisfiable("ENEMY UNIT (1)", s, caster=RequiredTo.PLAYER_1)
        )

    def test_no_caster_falls_back_to_any(self):
        # Without a caster the side filter can't resolve, so it's skipped
        # (behaves like ANY UNIT) rather than wrongly blocking the card.
        self.assertTrue(
            requirement_satisfiable("FRIENDLY UNIT (1)", state(p2=[FakeUnit(M2)]))
        )


class FriendlyEnemyEnumerateTests(unittest.TestCase):
    def test_enumerate_respects_side(self):
        req = selectable_unit_requirement("ENEMY UNIT (1)")
        s = state(p1=[FakeUnit(M1)], p2=[FakeUnit(M2)])
        self.assertEqual(
            enumerate_unit_target_sets(req, s, caster="player_1"), [(("player_2", 0),)]
        )
        self.assertEqual(
            enumerate_unit_target_sets(req, s, caster="player_2"), [(("player_1", 0),)]
        )

    def test_validate_rejects_wrong_side(self):
        req = selectable_unit_requirement("FRIENDLY UNIT (1)")
        s = state(p1=[FakeUnit(M1)], p2=[FakeUnit(M2)])
        self.assertTrue(
            target_set_satisfies(req, s, [("player_1", 0)], caster="player_1")
        )
        self.assertFalse(  # player_2's unit isn't friendly to player_1
            target_set_satisfies(req, s, [("player_2", 0)], caster="player_1")
        )


class FriendlyEnemyChoiceFlowTests(unittest.TestCase):
    """End-to-end: the offered targets are scoped to the caster's side."""

    def test_friendly_spell_only_offers_own_units(self):
        # En Garde = "FRIENDLY UNIT (1)"; caster is player_1.
        eng = _started_engine(
            ["En Garde"],
            p1=[PlayedUnit(card=M1, location="base")],
            p2=[PlayedUnit(card=M2, location="base")],
        )
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        # Only the caster's own unit is a legal target.
        self.assertEqual(
            eng.start().player_1_options, ["play:choose_spell_targets:p1-0"]
        )
        eng.apply_action(action="play:choose_spell_targets:p1-0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertEqual(gs.player_1_spells[0].targets, ["player_1:0"])

    def test_enemy_spell_only_offers_opponents_units(self):
        # Deadly Flourish = "ENEMY UNIT (1)"; caster is player_1.
        eng = _started_engine(
            ["Deadly Flourish"],
            p1=[PlayedUnit(card=M1, location="base")],
            p2=[PlayedUnit(card=M2, location="base")],
        )
        eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)
        self.assertEqual(
            eng.start().player_1_options, ["play:choose_spell_targets:p2-0"]
        )
        eng.apply_action(action="play:choose_spell_targets:p2-0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertEqual(gs.player_1_spells[0].targets, ["player_2:0"])

    def test_enemy_spell_not_offered_without_enemy_units(self):
        # Only the caster has units → an ENEMY UNIT spell has no legal target.
        eng = _started_engine(
            ["Deadly Flourish"], p1=[PlayedUnit(card=M1, location="base")]
        )
        self.assertNotIn("play:play_spell:0", eng.start().player_1_options)
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:play_spell:0", actor=RequiredTo.PLAYER_1)


class ShowdownCastingViaShortcutTests(unittest.TestCase):
    """In a showdown the focus holder casts via the same pre-costed picker as
    a normal turn — no standalone rune-tapping options in the menu."""

    def _showdown_engine(self):
        # Meditation: [Reaction], 2 Energy, no Power, no target requirement.
        eng = _started_engine(["Meditation"])
        gs = eng._game_state
        # Nothing banked, so the cast must be produced from runes.
        gs.player_1_energy = 0
        gs.player_1_power = {d: 0 for d in ("Fury", "Mind", "Calm", "Body", "Chaos", "Order")}
        gs.player_1_runes = [Rune(domain="Calm") for _ in range(3)]
        gs.pending_showdown = PendingShowdown(
            battlefield="battlefield_1", initiator=RequiredTo.PLAYER_1
        )
        return eng

    def test_menu_has_no_standalone_rune_options(self):
        eng = self._showdown_engine()
        opts = eng.start().player_1_options
        # No exhaust/recycle spam, and the unaffordable-now cast isn't a bare
        # option either — just pass (P1 has no base units to muster).
        self.assertEqual(opts, ["play:pass_showdown"])
        self.assertFalse(any("rune" in o for o in opts))

    def test_cast_offered_as_precosted_intent_and_resolves(self):
        from riftbound_engine.shortcuts import compute_play_intents, execution_steps

        eng = self._showdown_engine()
        intents = compute_play_intents(eng, RequiredTo.PLAYER_1)
        self.assertEqual([i.card_name for i in intents], ["Meditation"])
        # The combo produces Energy from runes, then casts.
        chain = execution_steps(intents[0].combos[0])
        self.assertEqual(
            [a for _, a in chain],
            ["play:exhaust_rune:0", "play:exhaust_rune:1", "play:play_spell:0"],
        )
        for actor, action in chain:
            eng.apply_action(action=action, actor=actor)
        gs = eng._game_state
        self.assertEqual([s.card for s in gs.player_1_spells], ["Meditation"])
        self.assertTrue(gs.pending_showdown.locked)  # casting started the fight
        self.assertEqual([i.card for i in gs.pending_chain.items], ["Meditation"])

    def test_non_focus_holder_gets_no_intents(self):
        from riftbound_engine.shortcuts import compute_play_intents

        eng = self._showdown_engine()
        # Focus is with P1 (initiator); P2 isn't on the clock.
        self.assertEqual(compute_play_intents(eng, RequiredTo.PLAYER_2), [])


class ZeroMightCombatTests(unittest.TestCase):
    """A 0-Might unit still needs 1 damage to die (lethal threshold max(M,1))."""

    def test_zero_might_unit_costs_one_to_kill(self):
        from riftbound_engine.engine import PendingCombat

        # P2 fields a 2-Might and a 0-Might unit; P1 has 2 damage to assign.
        eng = _started_engine(
            [],
            p2=[
                PlayedUnit(card=M2, location="battlefield_1", exhausted=True),  # Might 2
                PlayedUnit(card=M0, location="battlefield_1", exhausted=True),  # Might 0
            ],
        )
        eng._game_state.pending_combat = PendingCombat(
            battlefield="battlefield_1",
            player_1_might=2,
            player_2_might=0,
            player_2_targets=[],
        )
        opts = set(eng._assign_damage_options(RequiredTo.PLAYER_1))
        # 2 damage kills EITHER the 2-Might (cost 2) OR the 0-Might (cost 1),
        # never both (2+1 = 3 > 2). "Kill nothing" is illegal — 2 leftover
        # could still finish the 0-Might unit.
        self.assertEqual(opts, {"play:assign_damage:0", "play:assign_damage:1"})

    def test_one_damage_is_lethal_to_a_zero_might_unit(self):
        from riftbound_engine.engine import PendingCombat

        eng = _started_engine(
            [], p2=[PlayedUnit(card=M0, location="battlefield_1", exhausted=True)]
        )
        gs = eng._game_state
        gs.pending_combat = PendingCombat(
            battlefield="battlefield_1",
            player_1_might=1,
            player_2_might=0,
            player_2_targets=[],  # P2 has no damage → auto-commits nothing
        )
        eng.apply_action(action="play:assign_damage:0", actor=RequiredTo.PLAYER_1)
        gs = eng._game_state
        self.assertIsNone(gs.pending_combat)  # both committed → resolved
        self.assertEqual([u.card for u in gs.player_2_units], [])  # 0-Might died
        self.assertEqual(gs.player_2_trash, [M0])

    def test_zero_budget_cannot_kill_a_zero_might_unit(self):
        from riftbound_engine.engine import PendingCombat

        eng = _started_engine(
            [], p2=[PlayedUnit(card=M0, location="battlefield_1", exhausted=True)]
        )
        eng._game_state.pending_combat = PendingCombat(
            battlefield="battlefield_1",
            player_1_might=0,
            player_2_might=0,
            player_2_targets=[],
        )
        # 0 damage can't reach the 1-damage lethal threshold → only "kill nothing".
        self.assertEqual(
            eng._assign_damage_options(RequiredTo.PLAYER_1), ["play:assign_damage:"]
        )


class ChampionPlayTests(unittest.TestCase):
    """The chosen champion plays like a hand unit: surfaced when affordable,
    commits to a location, can only be played once."""

    def test_champion_offered_and_plays_then_locks(self):
        eng = _started_engine([])  # empty hand; champion still playable
        gs = eng._game_state
        champ = gs.player_1_deck.chosen_champion
        self.assertFalse(gs.player_1_champion_played)
        # Resources are flooded → offered as a flat option.
        self.assertIn("play:play_champion", eng.start().player_1_options)
        eng.apply_action(action="play:play_champion", actor=RequiredTo.PLAYER_1)
        self.assertTrue(gs.player_1_champion_played)
        self.assertEqual(gs.pending_play.card, champ)
        eng.apply_action(action="play:choose_location:base", actor=RequiredTo.PLAYER_1)
        self.assertIn(champ, [u.card for u in gs.player_1_units])
        # No longer offered, and a second play is rejected.
        self.assertNotIn("play:play_champion", eng.start().player_1_options)
        with self.assertRaises(ValueError):
            eng.apply_action(action="play:play_champion", actor=RequiredTo.PLAYER_1)

    def test_champion_surfaced_as_intent_when_rune_payment_needed(self):
        from riftbound_engine.shortcuts import compute_play_intents, CHAMPION_CARD_INDEX
        eng = _started_engine([])
        gs = eng._game_state
        # Strip pre-banked resources; give runes of the champion's domain so
        # it must be paid via rune taps → surfaced through the intent picker.
        from riftbound_engine.csv_data import card_domains_of, card_energy_of
        champ = gs.player_1_deck.chosen_champion
        dom = (card_domains_of(champ) or ("Chaos",))[0]
        gs.player_1_energy = 0
        gs.player_1_power = {}
        gs.player_1_runes = [Rune(domain=dom) for _ in range(max(2, (card_energy_of(champ) or 0) + 1))]
        champ_intents = [
            i for i in compute_play_intents(eng, RequiredTo.PLAYER_1)
            if i.play_action == "play_champion"
        ]
        self.assertEqual(len(champ_intents), 1)
        self.assertEqual(champ_intents[0].card_name, champ)
        self.assertEqual(champ_intents[0].card_index, CHAMPION_CARD_INDEX)
        self.assertTrue(len(champ_intents[0].combos) >= 1)


if __name__ == "__main__":
    unittest.main()
