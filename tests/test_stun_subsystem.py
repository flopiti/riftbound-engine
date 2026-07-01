"""Stun subsystem (Riftbound stun).

Covers the shared ``stunned`` unit state and its consumers:
  * combat: a stunned unit contributes 0 to the battlefield damage budget;
  * timing: stun is consumed at the controller's next ready step, and the unit
    skips readying that once (it does NOT clear at generic end of turn);
  * effects: STUN_ENEMY_UNIT / STUN_UNIT set the flag and emit ON_STUN for
    enemy targets; STUN_OR_RETURN_IF_STUNNED (Existential Dread) stuns a fresh
    unit but bounces an already-stunned one to hand;
  * trigger: WHEN_YOU_STUN_ENEMY_UNIT responds to ON_STUN (Vex, Eclipse Herald);
  * Vex, Mocking's MAY_MOVE_ME_TO_STUNNED_UNIT_BF self-move;
  * the flag survives a state clone and is queryable by the search predicate.
"""

import unittest

from riftbound_engine import effects as E
from riftbound_engine.engine import GameEngine, GameState, PlayedUnit, RequiredTo
from riftbound_engine.triggers import (
    ON_STUN,
    GameEvent,
    trigger_matches,
)


def _run(engine, code, targets, controller=RequiredTo.PLAYER_1, source=None):
    ctx = E.EffectContext(
        engine=engine,
        controller=controller,
        source=source,
        code=code,
        targets=tuple(targets),
    )
    return E.execute_effect(ctx)


class StunEffectTests(unittest.TestCase):
    def _engine(self, **kw):
        gs = GameState(
            player_2_units=[PlayedUnit(card="Foe", location="battlefield_1", **kw)]
        )
        return GameEngine(game_state=gs)

    def test_codes_are_registered_and_targeted(self) -> None:
        for code in ("STUN_UNIT", "STUN_ENEMY_UNIT", "STUN_OR_RETURN_IF_STUNNED"):
            self.assertTrue(E.is_implemented(code), code)
            self.assertTrue(E.effect_uses_targets(code), code)

    def test_stun_sets_the_flag(self) -> None:
        eng = self._engine()
        self.assertTrue(_run(eng, "STUN_ENEMY_UNIT", ["player_2:0"]))
        self.assertTrue(eng._game_state.player_2_units[0].stunned)

    def test_stun_emits_on_stun_for_an_enemy_target(self) -> None:
        eng = self._engine()
        seen = []
        orig = eng._emit
        eng._emit = lambda ev: (seen.append(ev), orig(ev))[1]  # type: ignore
        _run(eng, "STUN_ENEMY_UNIT", ["player_2:0"], controller=RequiredTo.PLAYER_1)
        stun_events = [e for e in seen if e.kind == ON_STUN]
        self.assertEqual(len(stun_events), 1)
        self.assertEqual(stun_events[0].controller, "player_1")
        self.assertEqual(stun_events[0].battlefield, "battlefield_1")
        self.assertEqual(stun_events[0].data.get("unit"), "player_2:0")

    def test_stunning_your_own_unit_fires_no_enemy_stun_event(self) -> None:
        # Facebreaker stuns a friendly unit too — that must not fire "when you
        # stun an ENEMY unit".
        gs = GameState(
            player_1_units=[PlayedUnit(card="Ally", location="battlefield_1")]
        )
        eng = GameEngine(game_state=gs)
        seen = []
        orig = eng._emit
        eng._emit = lambda ev: (seen.append(ev), orig(ev))[1]  # type: ignore
        _run(eng, "STUN_UNIT", ["player_1:0"], controller=RequiredTo.PLAYER_1)
        self.assertTrue(eng._game_state.player_1_units[0].stunned)
        self.assertEqual([e for e in seen if e.kind == ON_STUN], [])


class StunCombatTests(unittest.TestCase):
    def test_stunned_unit_contributes_zero_combat_might(self) -> None:
        # bonus_might gives each unit combat strength without relying on the
        # card catalog (dummy card names have no printed Might).
        gs = GameState(
            player_1_units=[
                PlayedUnit(card="A", location="battlefield_1", exhausted=False, bonus_might=3),
                PlayedUnit(card="B", location="battlefield_1", exhausted=False, bonus_might=2),
            ]
        )
        eng = GameEngine(game_state=gs)
        base = eng.might_at_battlefield(RequiredTo.PLAYER_1, "battlefield_1")
        self.assertEqual(base, 5)
        eng._game_state.player_1_units[0].stunned = True
        reduced = eng.might_at_battlefield(RequiredTo.PLAYER_1, "battlefield_1")
        # The stunned unit drops out of the budget; the other still counts.
        self.assertLess(reduced, base)
        eng._game_state.player_1_units[1].stunned = True
        self.assertEqual(eng.might_at_battlefield(RequiredTo.PLAYER_1, "battlefield_1"), 0)


class StunTimingTests(unittest.TestCase):
    def test_ready_step_consumes_stun_and_skips_readying(self) -> None:
        gs = GameState(
            player_1_units=[
                PlayedUnit(card="Stunned", location="battlefield_1", exhausted=True, stunned=True),
                PlayedUnit(card="Normal", location="battlefield_1", exhausted=True),
            ]
        )
        eng = GameEngine(game_state=gs)
        eng._ready_all_units(RequiredTo.PLAYER_1)
        stunned, normal = eng._game_state.player_1_units
        # Stun consumed, but the unit missed this ready step (stays exhausted).
        self.assertFalse(stunned.stunned)
        self.assertTrue(stunned.exhausted)
        # The un-stunned unit readied normally.
        self.assertFalse(normal.exhausted)


class ExistentialDreadTests(unittest.TestCase):
    def _engine(self, stunned=False):
        gs = GameState(
            player_2_units=[
                PlayedUnit(card="Attacker", location="battlefield_1", stunned=stunned)
            ],
            player_2_hand=[],
        )
        return GameEngine(game_state=gs)

    def test_fresh_target_is_stunned(self) -> None:
        eng = self._engine(stunned=False)
        _run(eng, "STUN_OR_RETURN_IF_STUNNED", ["player_2:0"])
        self.assertEqual(len(eng._game_state.player_2_units), 1)
        self.assertTrue(eng._game_state.player_2_units[0].stunned)

    def test_already_stunned_target_is_returned_to_hand(self) -> None:
        eng = self._engine(stunned=True)
        _run(eng, "STUN_OR_RETURN_IF_STUNNED", ["player_2:0"])
        self.assertEqual(eng._game_state.player_2_units, [])
        self.assertIn("Attacker", eng._game_state.player_2_hand)


class StunTriggerTests(unittest.TestCase):
    def test_when_you_stun_enemy_unit_matches_on_stun(self) -> None:
        ev = GameEvent(kind=ON_STUN, controller="player_1", battlefield="battlefield_1")
        # FRIENDLY scope: fires for the stunner.
        self.assertTrue(
            trigger_matches("WHEN_YOU_STUN_ENEMY_UNIT", ev, owner_controller="player_1")
        )
        # Does not fire for the opponent.
        self.assertFalse(
            trigger_matches("WHEN_YOU_STUN_ENEMY_UNIT", ev, owner_controller="player_2")
        )

    def _vex_ctx(self, vex_loc="base"):
        gs = GameState(
            player_1_units=[PlayedUnit(card="Vex, Mocking", location=vex_loc)],
            player_2_units=[PlayedUnit(card="Foe", location="battlefield_2", stunned=True)],
        )
        eng = GameEngine(game_state=gs)
        # The trigger feeds the stunned unit ref as the target; Vex is the source.
        ctx = E.EffectContext(
            engine=eng,
            controller=RequiredTo.PLAYER_1,
            source="player_1:0",
            code="MAY_MOVE_ME_TO_STUNNED_UNIT_BF",
            targets=("player_2:0",),
        )
        return eng, ctx

    def test_vex_move_is_an_optional_choice(self) -> None:
        self.assertTrue(E.is_choice_effect("MAY_MOVE_ME_TO_STUNNED_UNIT_BF"))
        self.assertTrue(E.choice_effect_optional("MAY_MOVE_ME_TO_STUNNED_UNIT_BF"))

    def test_vex_taking_the_move_relocates_her(self) -> None:
        eng, ctx = self._vex_ctx()
        opts = E.choice_effect_options(ctx)
        self.assertEqual(len(opts), 1)  # the stunned unit's battlefield
        E.apply_choice_effect(ctx, opts[0])
        self.assertEqual(eng._game_state.player_1_units[0].location, "battlefield_2")

    def test_vex_declining_leaves_her_in_place(self) -> None:
        # Declining = the engine never calls apply; state is unchanged.
        eng, ctx = self._vex_ctx(vex_loc="base")
        _ = E.choice_effect_options(ctx)
        self.assertEqual(eng._game_state.player_1_units[0].location, "base")

    def test_vex_has_no_option_when_stunned_unit_is_at_base(self) -> None:
        gs = GameState(
            player_1_units=[PlayedUnit(card="Vex, Mocking", location="base")],
            player_2_units=[PlayedUnit(card="Foe", location="base", stunned=True)],
        )
        eng = GameEngine(game_state=gs)
        ctx = E.EffectContext(
            engine=eng, controller=RequiredTo.PLAYER_1, source="player_1:0",
            code="MAY_MOVE_ME_TO_STUNNED_UNIT_BF", targets=("player_2:0",),
        )
        self.assertEqual(E.choice_effect_options(ctx), [])


class StunVariantTests(unittest.TestCase):
    def test_stun_it_alias_stuns_the_fed_target(self) -> None:
        # Blast Cone / Vex Apathetic "…, [Stun] IT": the event feeds the unit ref.
        gs = GameState(player_2_units=[PlayedUnit(card="Foe", location="battlefield_1")])
        eng = GameEngine(game_state=gs)
        self.assertTrue(_run(eng, "STUN_IT", ["player_2:0"]))
        self.assertTrue(eng._game_state.player_2_units[0].stunned)

    def test_stun_a_unit_is_a_forced_choice_over_all_units(self) -> None:
        gs = GameState(
            player_1_units=[PlayedUnit(card="Mine", location="base")],
            player_2_units=[PlayedUnit(card="Foe", location="battlefield_1")],
        )
        eng = GameEngine(game_state=gs)
        ctx = E.EffectContext(
            engine=eng, controller=RequiredTo.PLAYER_1, source=None, code="STUN_A_UNIT"
        )
        self.assertFalse(E.choice_effect_optional("STUN_A_UNIT"))
        opts = E.choice_effect_options(ctx)
        self.assertEqual(len(opts), 2)  # both units are eligible
        E.apply_choice_effect(ctx, "p2-0")
        self.assertTrue(eng._game_state.player_2_units[0].stunned)

    def test_stun_an_enemy_unit_here_scopes_to_source_battlefield(self) -> None:
        gs = GameState(
            player_1_units=[PlayedUnit(card="Attacker", location="battlefield_1")],
            player_2_units=[
                PlayedUnit(card="Here", location="battlefield_1"),
                PlayedUnit(card="Elsewhere", location="battlefield_2"),
            ],
        )
        eng = GameEngine(game_state=gs)
        ctx = E.EffectContext(
            engine=eng, controller=RequiredTo.PLAYER_1, source="player_1:0",
            code="STUN_AN_ENEMY_UNIT_HERE",
        )
        opts = E.choice_effect_options(ctx)
        self.assertEqual(opts, ["p2-0"])  # only the enemy unit HERE (battlefield_1)


class StunCardWiringTests(unittest.TestCase):
    """The engine test-suite is hermetic (conftest points the taxonomy loader at
    a nonexistent file). This test opts into the REAL front-end taxonomy to
    confirm the stun cards are wired, then restores the hermetic default."""

    def test_all_wired_stun_cards_attach_stun_abilities(self) -> None:
        import os
        from unittest import mock

        from riftbound_engine import abilities as A

        real = os.path.join(
            os.path.dirname(__file__), "..", "..", "riftbound", "data", "card_taxonomy.json"
        )
        if not os.path.exists(real):
            self.skipTest("front-end taxonomy not checked out alongside the engine")

        stun_codes = {
            "STUN_UNIT", "STUN_ENEMY_UNIT", "STUN_IT", "STUN_A_UNIT",
            "STUN_AN_ENEMY_UNIT", "STUN_AN_ENEMY_UNIT_HERE",
            "STUN_OR_RETURN_IF_STUNNED", "MAY_MOVE_ME_TO_STUNNED_UNIT_BF",
        }
        expected = [
            "Rune Prison", "Zenith Blade", "Thwonk!", "Back Off",
            "Solari Shieldbearer", "Leona, Determined", "Vi, Peacekeeper",
            "Vex, Mocking", "Vex, Apathetic", "Blast Cone", "Existential Dread",
        ]
        try:
            with mock.patch.dict("os.environ", {"RIFTBOUND_TAXONOMY_PATH": real}):
                A.reset_caches()
                for name in expected:
                    abils = A.triggered_abilities_for(name)
                    self.assertTrue(abils, f"{name} has no abilities")
                    codes = {c for ab in abils for c in ab.active_effects}
                    self.assertTrue(
                        codes & stun_codes, f"{name} has no stun effect (got {codes})"
                    )
        finally:
            A.reset_caches()


class SelfEffectTests(unittest.TestCase):
    def test_ready_me_readies_the_source_unit(self) -> None:
        gs = GameState(
            player_1_units=[PlayedUnit(card="Eclipse Herald", location="battlefield_1", exhausted=True)]
        )
        eng = GameEngine(game_state=gs)
        self.assertTrue(_run(eng, "READY_ME", [], source="player_1:0"))
        self.assertFalse(eng._game_state.player_1_units[0].exhausted)

    def test_ready_me_is_a_noop_without_a_unit_source(self) -> None:
        eng = GameEngine(game_state=GameState())
        # No source unit → runs but changes nothing, no crash.
        _run(eng, "READY_ME", [], source=None)

    def test_eclipse_herald_readies_and_buffs_itself(self) -> None:
        # The full ability: ready me + give me +1 might this turn.
        gs = GameState(
            player_1_units=[PlayedUnit(card="Eclipse Herald", location="battlefield_1", exhausted=True)]
        )
        eng = GameEngine(game_state=gs)
        _run(eng, "READY_ME", [], source="player_1:0")
        _run(eng, "GIVE_ME_+1M", [], source="player_1:0")
        me = eng._game_state.player_1_units[0]
        self.assertFalse(me.exhausted)
        self.assertEqual(me.bonus_might, 1)


class GearPlayTriggerTests(unittest.TestCase):
    """WHEN_YOU_PLAY_GEAR / ON_PLAY_GEAR (Pit Crew: "when you play a gear, ready
    me")."""

    def test_trigger_matches_on_play_gear_for_the_player(self) -> None:
        from riftbound_engine.triggers import ON_PLAY_GEAR, GameEvent, trigger_matches

        ev = GameEvent(kind=ON_PLAY_GEAR, controller="player_1")
        self.assertTrue(trigger_matches("WHEN_YOU_PLAY_GEAR", ev, owner_controller="player_1"))
        self.assertFalse(trigger_matches("WHEN_YOU_PLAY_GEAR", ev, owner_controller="player_2"))

    def test_pit_crew_queues_ready_me_when_a_gear_is_played(self) -> None:
        import random
        from unittest import mock

        from riftbound_engine import abilities as A
        from riftbound_engine.triggers import GameEvent

        taxonomy = {
            "Pit Crew": (
                A.Ability(triggers=("WHEN_YOU_PLAY_GEAR",), active_effects=("READY_ME",)),
            )
        }
        eng = GameEngine(rng=random.Random(1))
        gs = eng._game_state
        gs.started = True
        gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
        gs.player_1_units = [PlayedUnit(card="Pit Crew", location="base", exhausted=True)]
        eng._trigger_queue = []
        with mock.patch.object(A, "triggered_abilities_for", side_effect=lambda n: taxonomy.get(n, ())):
            eng._emit(GameEvent(kind="ON_PLAY_GEAR", controller="player_1"))
        te = [t for t in eng._trigger_queue if t.trigger == "WHEN_YOU_PLAY_GEAR"]
        self.assertEqual(len(te), 1)
        # Source is Pit Crew itself, so READY_ME will ready it.
        self.assertEqual(te[0].source, "player_1:0")
        self.assertEqual(te[0].effects, ("READY_ME",))

    def test_opponents_gear_play_does_not_fire_pit_crew(self) -> None:
        import random
        from unittest import mock

        from riftbound_engine import abilities as A
        from riftbound_engine.triggers import GameEvent

        taxonomy = {
            "Pit Crew": (
                A.Ability(triggers=("WHEN_YOU_PLAY_GEAR",), active_effects=("READY_ME",)),
            )
        }
        eng = GameEngine(rng=random.Random(1))
        eng._game_state.player_1_units = [PlayedUnit(card="Pit Crew", location="base", exhausted=True)]
        eng._trigger_queue = []
        with mock.patch.object(A, "triggered_abilities_for", side_effect=lambda n: taxonomy.get(n, ())):
            eng._emit(GameEvent(kind="ON_PLAY_GEAR", controller="player_2"))
        self.assertEqual([t for t in eng._trigger_queue if t.trigger == "WHEN_YOU_PLAY_GEAR"], [])


class DiscardTriggerTests(unittest.TestCase):
    """ON_DISCARD / WHEN_YOU_DISCARD (Jinx, Rebel: "when you discard one or more
    cards, ready me and give me +1 might")."""

    def test_trigger_matches_on_discard_for_the_discarding_player(self) -> None:
        from riftbound_engine.triggers import ON_DISCARD, GameEvent, trigger_matches

        ev = GameEvent(kind=ON_DISCARD, controller="player_1")
        self.assertTrue(trigger_matches("WHEN_YOU_DISCARD", ev, owner_controller="player_1"))
        self.assertFalse(trigger_matches("WHEN_YOU_DISCARD", ev, owner_controller="player_2"))

    def test_discarding_emits_on_discard_for_the_owner(self) -> None:
        # DISCARD_1 (a forced choice) moves the picked card to trash and emits
        # ON_DISCARD for the discarding player.
        gs = GameState(player_1_hand=["Ziggs", "Poppy"], player_1_trash=[])
        eng = GameEngine(game_state=gs)
        seen = []
        orig = eng._emit
        eng._emit = lambda ev: (seen.append(ev), orig(ev))[1]  # type: ignore
        ctx = E.EffectContext(
            engine=eng, controller=RequiredTo.PLAYER_1, source=None, code="DISCARD_1"
        )
        E.apply_choice_effect(ctx, "h-0")
        self.assertEqual(eng._game_state.player_1_trash, ["Ziggs"])
        discards = [e for e in seen if e.kind == "ON_DISCARD"]
        self.assertEqual(len(discards), 1)
        self.assertEqual(discards[0].controller, "player_1")
        self.assertEqual(discards[0].data.get("card"), "Ziggs")

    def test_jinx_queues_ready_and_buff_on_discard(self) -> None:
        import random
        from unittest import mock

        from riftbound_engine import abilities as A
        from riftbound_engine.triggers import GameEvent

        taxonomy = {
            "Jinx, Rebel": (
                A.Ability(triggers=("WHEN_YOU_DISCARD",), active_effects=("READY_ME", "GIVE_ME_+1M")),
            )
        }
        eng = GameEngine(rng=random.Random(1))
        eng._game_state.player_1_units = [
            PlayedUnit(card="Jinx, Rebel", location="base", exhausted=True)
        ]
        eng._trigger_queue = []
        with mock.patch.object(A, "triggered_abilities_for", side_effect=lambda n: taxonomy.get(n, ())):
            eng._emit(GameEvent(kind="ON_DISCARD", controller="player_1", data={"card": "X"}))
        te = [t for t in eng._trigger_queue if t.trigger == "WHEN_YOU_DISCARD"]
        self.assertEqual(len(te), 1)
        self.assertEqual(te[0].source, "player_1:0")
        self.assertEqual(te[0].effects, ("READY_ME", "GIVE_ME_+1M"))


class DiscardMeTriggerTests(unittest.TestCase):
    """WHEN_YOU_DISCARD_ME (Flame Chompers). The discarded card is NOT in play,
    so it fires via engine._queue_discard_me_trigger, and its "you may pay 1
    fury to play me" is an OPTIONAL choice."""

    def _taxonomy(self):
        from riftbound_engine import abilities as A

        return {
            "Flame Chompers": (
                A.Ability(
                    triggers=("WHEN_YOU_DISCARD_ME",),
                    active_effects=("MAY_PAY_1_FURY_PLAY_ME_FROM_TRASH",),
                ),
            )
        }

    def test_discarding_flame_chompers_queues_its_own_trigger(self) -> None:
        import random
        from unittest import mock

        from riftbound_engine import abilities as A
        from riftbound_engine.triggers import GameEvent

        eng = GameEngine(rng=random.Random(1))
        eng._game_state.player_1_trash = ["Flame Chompers"]
        eng._trigger_queue = []
        with mock.patch.object(A, "triggered_abilities_for", side_effect=lambda n: self._taxonomy().get(n, ())):
            eng._emit(GameEvent(kind="ON_DISCARD", controller="player_1", data={"card": "Flame Chompers"}))
        te = [t for t in eng._trigger_queue if t.trigger == "WHEN_YOU_DISCARD_ME"]
        self.assertEqual(len(te), 1)
        self.assertEqual(te[0].source, "trash:player_1:Flame Chompers")
        self.assertEqual(te[0].effects, ("MAY_PAY_1_FURY_PLAY_ME_FROM_TRASH",))

    def test_no_trigger_for_a_plain_card(self) -> None:
        import random
        from unittest import mock

        from riftbound_engine import abilities as A
        from riftbound_engine.triggers import GameEvent

        eng = GameEngine(rng=random.Random(1))
        eng._trigger_queue = []
        with mock.patch.object(A, "triggered_abilities_for", side_effect=lambda n: self._taxonomy().get(n, ())):
            eng._emit(GameEvent(kind="ON_DISCARD", controller="player_1", data={"card": "Some Card"}))
        self.assertEqual([t for t in eng._trigger_queue if t.trigger == "WHEN_YOU_DISCARD_ME"], [])

    def _ctx(self, eng):
        return E.EffectContext(
            engine=eng, controller=RequiredTo.PLAYER_1,
            source="trash:player_1:Flame Chompers",
            code="MAY_PAY_1_FURY_PLAY_ME_FROM_TRASH",
        )

    def test_option_offered_only_when_fury_is_affordable(self) -> None:
        eng = GameEngine(game_state=GameState(player_1_trash=["Flame Chompers"]))
        # No Fury power → no option.
        self.assertEqual(E.choice_effect_options(self._ctx(eng)), [])
        eng._game_state.player_1_power = {"Fury": 1}
        self.assertEqual(E.choice_effect_options(self._ctx(eng)), ["pay_fury"])

    def test_paying_spends_fury_and_plays_from_trash(self) -> None:
        gs = GameState(player_1_trash=["Flame Chompers"], player_1_units=[])
        gs.player_1_power = {"Fury": 2}
        eng = GameEngine(game_state=gs)
        E.apply_choice_effect(self._ctx(eng), "pay_fury")
        self.assertEqual(eng._game_state.player_1_power.get("Fury", 0), 1)  # spent 1
        self.assertNotIn("Flame Chompers", eng._game_state.player_1_trash)
        units = eng._game_state.player_1_units
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].card, "Flame Chompers")
        self.assertTrue(units[0].exhausted)  # summoning sickness

    def test_the_choice_is_optional(self) -> None:
        self.assertTrue(E.choice_effect_optional("MAY_PAY_1_FURY_PLAY_ME_FROM_TRASH"))
        # Declining changes nothing (engine never calls apply).
        gs = GameState(player_1_trash=["Flame Chompers"])
        gs.player_1_power = {"Fury": 1}
        eng = GameEngine(game_state=gs)
        _ = E.choice_effect_options(self._ctx(eng))
        self.assertEqual(eng._game_state.player_1_trash, ["Flame Chompers"])
        self.assertEqual(eng._game_state.player_1_units, [])


class PassiveCantMoveTests(unittest.TestCase):
    """Vex, Apathetic: "…, [Stun] it. They can't move it this turn." STUN_IT is
    the ACTIVE (chain) effect; CANT_MOVE is a PASSIVE this-turn restriction,
    applied off the chain the moment the ability fires."""

    def test_apply_trigger_passives_sets_cant_move_off_chain(self) -> None:
        from riftbound_engine import abilities as A

        gs = GameState(player_2_units=[PlayedUnit(card="Foe", location="battlefield_1")])
        eng = GameEngine(game_state=gs)
        ability = A.Ability(triggers=("WHEN_OPPONENT_PLAYS_UNIT",), active_effects=("STUN_IT",),
                            passive_effects=("CANT_MOVE",))
        eng._apply_trigger_passives(ability, "player_1", ("player_2:0",))
        self.assertTrue(eng._game_state.player_2_units[0].cant_move)
        # Passive, not chain: it did NOT stun (that's the active effect).
        self.assertFalse(eng._game_state.player_2_units[0].stunned)

    def test_continuous_aura_passives_are_not_applied_here(self) -> None:
        from riftbound_engine import abilities as A

        gs = GameState(player_2_units=[PlayedUnit(card="Foe", location="battlefield_1")])
        eng = GameEngine(game_state=gs)
        # A continuous aura code (read live elsewhere) must NOT be "applied".
        ability = A.Ability(passive_effects=("OTHER_FRIENDLY_UNITS_HERE_+1M",))
        eng._apply_trigger_passives(ability, "player_1", ("player_2:0",))
        self.assertFalse(eng._game_state.player_2_units[0].cant_move)

    def test_vex_apathetic_stuns_on_chain_and_cant_move_off_chain(self) -> None:
        import random
        from unittest import mock

        from riftbound_engine import abilities as A
        from riftbound_engine.triggers import GameEvent

        taxonomy = {
            "Vex, Apathetic": (
                A.Ability(
                    triggers=("WHEN_OPPONENT_PLAYS_UNIT",),
                    conditions=("WHILE_AT_BATTLEFIELD",),
                    active_effects=("STUN_IT",),
                    passive_effects=("CANT_MOVE",),
                ),
            )
        }
        eng = GameEngine(rng=random.Random(1))
        gs = eng._game_state
        gs.player_1_units = [PlayedUnit(card="Vex, Apathetic", location="battlefield_1", exhausted=False)]
        gs.player_2_units = [PlayedUnit(card="Intruder", location="battlefield_1", exhausted=True)]
        eng._trigger_queue = []
        with mock.patch.object(A, "triggered_abilities_for", side_effect=lambda n: taxonomy.get(n, ())):
            eng._emit(GameEvent(kind="ON_PLAY_UNIT", controller="player_2", source="player_2:0", battlefield="battlefield_1"))
        played = eng._game_state.player_2_units[0]
        # CANT_MOVE applied immediately (passive, off-chain).
        self.assertTrue(played.cant_move)
        # STUN is queued as the chain effect — NOT applied yet, and CANT_MOVE is
        # NOT among the chain effects.
        te = [t for t in eng._trigger_queue if t.trigger == "WHEN_OPPONENT_PLAYS_UNIT"]
        self.assertEqual(len(te), 1)
        self.assertEqual(te[0].effects, ("STUN_IT",))
        self.assertFalse(played.stunned)


class StunClonePredicateTests(unittest.TestCase):
    def test_stunned_survives_state_clone(self) -> None:
        gs = GameState(
            player_1_units=[PlayedUnit(card="A", location="battlefield_1", stunned=True)]
        )
        eng = GameEngine(game_state=gs)
        self.assertTrue(eng.game_state.player_1_units[0].stunned)

    def test_predicate_can_match_a_stunned_unit(self) -> None:
        from riftbound_engine.search import _match_units, state_view

        gs = GameState(
            player_2_units=[PlayedUnit(card="Foe", location="battlefield_1", stunned=True)]
        )
        view = state_view(GameEngine(game_state=gs)._game_state)
        self.assertTrue(_match_units(view, {"controller": "player_2", "stunned": True, "min": 1}))
        self.assertFalse(_match_units(view, {"controller": "player_2", "stunned": False, "min": 1}))


if __name__ == "__main__":
    unittest.main()
