"""Equipping is a NORMAL-TURN play (action timing): it pays the [Equip] cost
to attach an Equipment gear to one of your units, and — like playing a gear or
unit — it can't happen mid-resolution (open chain, showdown, combat, or while a
choice is pending). These guards live in the ``_equip`` handler so a raw
``play:equip`` is rejected even though the UI only ever offers it on the action
turn (compute_equip_intents gates the same conditions)."""

import csv
import unittest
from unittest import mock

from riftbound_engine import abilities as _abilities
from riftbound_engine.action_turn.builtins import _equip
from riftbound_engine.action_turn.context import ActionTurnContext
from riftbound_engine.csv_data import card_equip_cost, card_is_equipment
from riftbound_engine.engine import (
    GameEngine,
    GameState,
    PlayedGear,
    PlayedUnit,
    RequiredTo,
)


def _an_equipment() -> str:
    for r in csv.DictReader(open("riftbound_cards.csv", newline="", encoding="utf-8")):
        n = (r.get("Name") or "").strip()
        if n and card_is_equipment(n) and card_equip_cost(n):
            return n
    raise unittest.SkipTest("no parseable equipment in the CSV")


def _engine() -> GameEngine:
    gear = _an_equipment()
    gs = GameState(
        player_1_units=[PlayedUnit(card="Ravenbloom Student", location="base")],
        player_1_gears=[PlayedGear(card=gear, location="base", exhausted=False)],
        player_1_energy=10,
        player_1_power={"Fury": 5, "Calm": 5, "Mind": 5, "Body": 5, "Chaos": 5, "Order": 5},
    )
    return GameEngine(game_state=gs)


def _equip_now(eng: GameEngine) -> None:
    _equip(ActionTurnContext(engine=eng, actor=RequiredTo.PLAYER_1, verb="equip", payload="0:player_1:0"))


class EquipTimingTests(unittest.TestCase):
    def test_equips_on_a_clean_action_turn(self) -> None:
        eng = _engine()
        _equip_now(eng)
        self.assertEqual(eng._game_state.player_1_gears[0].attached_to, "player_1:0")

    def test_rejected_during_showdown(self) -> None:
        eng = _engine()
        eng._game_state.pending_showdown = object()  # a showdown is open
        with self.assertRaises(ValueError):
            _equip_now(eng)
        self.assertIsNone(eng._game_state.player_1_gears[0].attached_to)

    def test_rejected_during_combat(self) -> None:
        eng = _engine()
        eng._game_state.pending_combat = object()
        with self.assertRaises(ValueError):
            _equip_now(eng)

    def test_rejected_with_open_chain(self) -> None:
        eng = _engine()
        eng._game_state.pending_chain = object()
        with self.assertRaises(ValueError):
            _equip_now(eng)


class AttachedMightTests(unittest.TestCase):
    """An EFFECT-TEXT equipment passive (UNIT_ATTACHED_+NM) grants its host
    unit Might continuously while attached — counted everywhere Might matters."""

    EQ = _abilities.Ability(effect_text=True, passive_effects=("UNIT_ATTACHED_+2M",))

    def setUp(self) -> None:
        # Hermetic suite disables the taxonomy; stub a +2M effect-text passive
        # for our fake equipment "Buff Blade".
        patch = mock.patch.object(
            _abilities,
            "triggered_abilities_for",
            side_effect=lambda n: (self.EQ,) if n == "Buff Blade" else (),
        )
        patch.start()
        self.addCleanup(patch.stop)

    def _engine_with_attached(self) -> GameEngine:
        gs = GameState(
            player_1_units=[PlayedUnit(card="Ravenbloom Student", location="battlefield_1")],  # printed 2
            player_1_gears=[
                PlayedGear(card="Buff Blade", location="base", attached_to="player_1:0", attached_uid=1)
            ],
        )
        eng = GameEngine(game_state=gs)
        eng._backfill_unit_uids()  # host unit → uid 1, matching attached_uid
        return eng

    def test_effective_might_includes_attached_equipment(self) -> None:
        eng = self._engine_with_attached()
        # 2 printed + 2 from the attached equipment.
        self.assertEqual(eng.effective_unit_might("player_1", 0), 4)

    def test_disappears_when_unattached(self) -> None:
        eng = self._engine_with_attached()
        eng._game_state.player_1_gears[0].attached_uid = None  # identity = uid
        self.assertEqual(eng.effective_unit_might("player_1", 0), 2)  # back to printed

    def test_counts_toward_battlefield_budget(self) -> None:
        eng = self._engine_with_attached()
        self.assertEqual(eng.might_at_battlefield(RequiredTo.PLAYER_1, "battlefield_1"), 4)

    def test_buff_follows_host_across_index_shift(self) -> None:
        # Attach to the SECOND unit; when the FIRST leaves play (indices shift),
        # the +2 must still apply to the same unit — not whatever now sits at
        # the old index. This is the uid-stability guarantee.
        gs = GameState(
            player_1_units=[
                PlayedUnit(card="Ravenbloom Student", location="battlefield_1"),  # 2
                PlayedUnit(card="Plundering Poro", location="battlefield_1"),  # 2
            ],
            player_1_gears=[PlayedGear(card="Buff Blade", location="base")],
        )
        eng = GameEngine(game_state=gs)
        eng._backfill_unit_uids()  # uids 1, 2
        eng._game_state.player_1_gears[0].attached_uid = eng._game_state.player_1_units[1].uid
        self.assertEqual(eng.effective_unit_might("player_1", 1), 4)  # Poro buffed
        self.assertEqual(eng.effective_unit_might("player_1", 0), 2)  # Raven not
        eng._game_state.player_1_units.pop(0)  # Raven leaves → Poro shifts to idx 0
        self.assertEqual(eng.effective_unit_might("player_1", 0), 4)  # still the Poro, still buffed

    def test_lifts_unit_out_of_gust_range(self) -> None:
        # Gust targets a unit at a battlefield with <=3 Might. Base 2 → eligible;
        # +2 equipment → 4 Might → no longer a legal Gust target.
        from riftbound_engine.requirements import board_units, targets_still_satisfy

        eng = self._engine_with_attached()
        views = board_units(eng._game_state)
        self.assertEqual(views[0].might, 4)
        self.assertFalse(
            targets_still_satisfy("ANY UNIT (1[<=3M BF])", [views[0]], "player_2")
        )


class ConditionalAttachMightTests(unittest.TestCase):
    """Brutalizer-style modelling: a +1M passive (always) plus a +2M passive
    gated by ATTACHED_THIS_TURN, evaluated against the gear's attach turn."""

    BRUTALIZER = (
        _abilities.Ability(effect_text=True, passive_effects=("UNIT_ATTACHED_+1M",)),
        _abilities.Ability(
            effect_text=True,
            conditions=("ATTACHED_THIS_TURN",),
            passive_effects=("UNIT_ATTACHED_+2M",),
        ),
    )

    def setUp(self) -> None:
        patch = mock.patch.object(
            _abilities,
            "triggered_abilities_for",
            side_effect=lambda n: self.BRUTALIZER if n == "Brutalizer" else (),
        )
        patch.start()
        self.addCleanup(patch.stop)

    def _engine(self, attached_on_turn: int, current_turn: int) -> GameEngine:
        gs = GameState(
            total_turn_number=current_turn,
            player_1_units=[PlayedUnit(card="Ravenbloom Student", location="battlefield_1")],  # printed 2
            player_1_gears=[
                PlayedGear(
                    card="Brutalizer",
                    location="base",
                    attached_to="player_1:0",
                    attached_uid=1,
                    attached_on_turn=attached_on_turn,
                )
            ],
        )
        eng = GameEngine(game_state=gs)
        eng._backfill_unit_uids()  # host unit → uid 1, matching attached_uid
        return eng

    def test_extra_2m_applies_the_turn_it_is_attached(self) -> None:
        eng = self._engine(attached_on_turn=5, current_turn=5)
        # 2 printed + 1 (always) + 2 (attached this turn) = 5.
        self.assertEqual(eng.effective_unit_might("player_1", 0), 5)

    def test_extra_2m_gone_on_a_later_turn(self) -> None:
        eng = self._engine(attached_on_turn=5, current_turn=6)
        # 2 printed + 1 (always) only — the conditional +2 expired.
        self.assertEqual(eng.effective_unit_might("player_1", 0), 3)


class EndOfTurnEquipmentTests(unittest.TestCase):
    """Blighted Battleaxe: AT_END_OF_TURN → UNATTACH_THIS + DEAL_4_SELF, where
    'this' is the gear and 'self' is the equipped unit."""

    ABILITY = _abilities.Ability(
        effect_text=True,
        triggers=("AT_END_OF_TURN",),
        active_effects=("UNATTACH_THIS", "DEAL_4_SELF"),
    )

    def setUp(self) -> None:
        patch = mock.patch.object(
            _abilities,
            "triggered_abilities_for",
            side_effect=lambda n: (self.ABILITY,) if n == "Blighted Battleaxe" else (),
        )
        patch.start()
        self.addCleanup(patch.stop)

    def _engine(self, host_card: str = "Ravenbloom Student") -> GameEngine:  # printed 2
        gs = GameState(
            current_player=RequiredTo.PLAYER_1,
            total_turn_number=3,
            player_1_units=[PlayedUnit(card=host_card, location="battlefield_1")],
            player_1_gears=[
                PlayedGear(
                    card="Blighted Battleaxe",
                    location="base",
                    attached_to="player_1:0",
                    attached_uid=1,
                    attached_on_turn=3,
                )
            ],
        )
        eng = GameEngine(game_state=gs)
        eng._backfill_unit_uids()
        return eng

    def _src(self, eng: GameEngine) -> str:
        return f"equip:player_1:0:{eng._game_state.player_1_units[0].uid}"

    def test_attached_equipment_is_scanned_for_triggers(self) -> None:
        eng = self._engine()
        refs = [ref for (ref, *_rest) in eng._abilities_in_play()]
        self.assertTrue(any(r.startswith("equip:player_1:0:") for r in refs))

    def test_turn_end_queues_the_equipment_ability(self) -> None:
        from riftbound_engine.triggers import GameEvent

        eng = self._engine()
        eng._emit(GameEvent(kind="TURN_END", controller="player_1"))
        self.assertEqual(len(eng._trigger_queue), 1)
        te = eng._trigger_queue[0]
        self.assertEqual(te.effects, ("UNATTACH_THIS", "DEAL_4_SELF"))
        self.assertTrue(te.source.startswith("equip:player_1:0:"))

    def test_unattach_and_lethal_self_damage(self) -> None:
        from riftbound_engine import effects

        eng = self._engine()  # 2-Might host
        src = self._src(eng)
        effects.execute_effect(
            effects.EffectContext(engine=eng, controller=RequiredTo.PLAYER_1, source=src, code="UNATTACH_THIS")
        )
        self.assertIsNone(eng._game_state.player_1_gears[0].attached_to)
        effects.execute_effect(
            effects.EffectContext(engine=eng, controller=RequiredTo.PLAYER_1, source=src, code="DEAL_4_SELF")
        )
        # Persistent-damage model: DEAL_4_SELF MARKS 4 damage (rule 417); the
        # unit is not removed at the instant the damage lands.
        self.assertEqual(eng._game_state.player_1_units[0].damage, 4)
        # Death is resolved by the cleanup lethal sweep (323.4/323.5) — which
        # _resolve_chain runs in real play after the ability resolves. 4 >= the
        # 2-Might host → it dies to its owner's trash.
        eng._lethal_sweep()
        self.assertEqual(eng._game_state.player_1_units, [])
        self.assertEqual(eng._game_state.player_1_trash, ["Ravenbloom Student"])

    def test_self_damage_below_might_is_survivable(self) -> None:
        from riftbound_engine import effects

        eng = self._engine(host_card="Garen, Rugged")  # printed 5
        effects.execute_effect(
            effects.EffectContext(
                engine=eng, controller=RequiredTo.PLAYER_1, source=self._src(eng), code="DEAL_4_SELF"
            )
        )
        self.assertEqual(len(eng._game_state.player_1_units), 1)  # 4 < 5 → survives


if __name__ == "__main__":
    unittest.main()
