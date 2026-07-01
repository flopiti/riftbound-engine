"""The move / ready effect group:
  - MAY_MOVE_FRIENDLY_UNIT_HERE_TO_BASE (Reaver's Row)
  - MAY_MOVE_UNIT_AT_BF_TO_BASE (Amateur Recital)
  - MAY_MOVE_ANOTHER_UNIT_TO_BASE (Star Spring)
  - MAY_MOVE_ENEMY_UNIT_HERE (Evelynn)
  - MOVE_FRIENDLY_UNIT_AND_READY (Ride the Wind)
  - READY_UNIT (Blood Rose), READY_LEGEND (Hall of Legends)
  - READY_2_RUNES_AT_END_TURN (Targon's Peak)
  - CANT_MOVE (Vex) + Emperor's Dais return half
"""

from __future__ import annotations

import unittest

from riftbound_engine import effects as E
from riftbound_engine.engine import (
    GameEngine,
    GameState,
    PlayedUnit,
    RequiredTo,
    Rune,
    build_deck_from_id,
)


def _ctx(engine, *, controller=RequiredTo.PLAYER_1, source=None, code="", targets=()):
    return E.EffectContext(
        engine=engine, controller=controller, source=source, code=code, targets=tuple(targets)
    )


def _eng(p1=(), p2=(), bf1="A", bf2="B") -> GameEngine:
    gs = GameState()
    gs.player_1_units = list(p1)
    gs.player_2_units = list(p2)
    gs.battlefield_1 = bf1
    gs.battlefield_2 = bf2
    return GameEngine(game_state=gs)


class MoveEffectTests(unittest.TestCase):
    def test_move_friendly_here_to_base(self) -> None:
        eng = _eng(p1=[PlayedUnit(card="A", location="battlefield_1", uid=1)])
        ctx = _ctx(eng, source="battlefield_1", code="MAY_MOVE_FRIENDLY_UNIT_HERE_TO_BASE")
        opts = E.choice_effect_options(ctx)
        self.assertEqual(opts, ["p1-0"])
        E.apply_choice_effect(ctx, "p1-0")
        self.assertEqual(eng._game_state.player_1_units[0].location, "base")

    def test_move_any_unit_at_bf_to_base_spans_both_players(self) -> None:
        eng = _eng(
            p1=[PlayedUnit(card="A", location="battlefield_1", uid=1),
                PlayedUnit(card="B", location="base", uid=2)],
            p2=[PlayedUnit(card="C", location="battlefield_2", uid=3)],
        )
        ctx = _ctx(eng, source="battlefield_1", code="MAY_MOVE_UNIT_AT_BF_TO_BASE")
        opts = E.choice_effect_options(ctx)
        self.assertIn("p1-0", opts)   # P1 unit at a BF
        self.assertIn("p2-0", opts)   # P2 unit at a BF (any player)
        self.assertNotIn("p1-1", opts)  # the one at base isn't eligible
        E.apply_choice_effect(ctx, "p2-0")
        self.assertEqual(eng._game_state.player_2_units[0].location, "base")

    def test_move_enemy_unit_here(self) -> None:
        # Evelynn (P1) at battlefield_2 pulls an enemy unit to her location.
        eng = _eng(
            p1=[PlayedUnit(card="Evelynn", location="battlefield_2", uid=1)],
            p2=[PlayedUnit(card="Foe", location="base", uid=2)],
        )
        ctx = _ctx(eng, source="player_1:0", code="MAY_MOVE_ENEMY_UNIT_HERE")
        self.assertEqual(E.choice_effect_options(ctx), ["p2-0"])
        E.apply_choice_effect(ctx, "p2-0")
        self.assertEqual(eng._game_state.player_2_units[0].location, "battlefield_2")

    def test_move_friendly_and_ready_spell(self) -> None:
        eng = _eng(p1=[PlayedUnit(card="A", location="base", exhausted=True, uid=1)])
        ctx = _ctx(
            eng, code="MOVE_FRIENDLY_UNIT_AND_READY",
            targets=("player_1:0", "move_dest:battlefield_1"),
        )
        self.assertTrue(E.execute_effect(ctx))
        u = eng._game_state.player_1_units[0]
        self.assertEqual(u.location, "battlefield_1")
        self.assertFalse(u.exhausted)  # readied


class ReadyEffectTests(unittest.TestCase):
    def test_ready_unit(self) -> None:
        eng = _eng(p1=[PlayedUnit(card="A", location="base", exhausted=True, uid=1)])
        ctx = _ctx(eng, code="READY_UNIT")
        self.assertEqual(E.choice_effect_options(ctx), ["p1-0"])
        E.apply_choice_effect(ctx, "p1-0")
        self.assertFalse(eng._game_state.player_1_units[0].exhausted)

    def test_ready_legend(self) -> None:
        eng = _eng()
        eng._game_state.player_1_legend_exhausted = True
        self.assertTrue(E.execute_effect(_ctx(eng, code="READY_LEGEND")))
        self.assertFalse(eng._game_state.player_1_legend_exhausted)

    def test_ready_2_runes_scheduled_and_applied_at_end_turn(self) -> None:
        eng = _eng()
        eng._game_state.player_1_runes = [Rune(domain="Fury", exhausted=True) for _ in range(3)]
        E.execute_effect(_ctx(eng, code="READY_2_RUNES_AT_END_TURN"))
        self.assertEqual(eng._game_state.player_1_end_turn_ready_runes, 2)
        eng._apply_end_turn_ready_runes()
        readied = sum(1 for r in eng._game_state.player_1_runes if not r.exhausted)
        self.assertEqual(readied, 2)  # up to 2; the third stays exhausted
        self.assertEqual(eng._game_state.player_1_end_turn_ready_runes, 0)


class CantMoveTests(unittest.TestCase):
    def test_cant_move_flag_set_and_suppresses_move_option(self) -> None:
        gs = GameState()
        gs.started = True
        gs.is_mulligan_done = True
        gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
        gs.current_player = RequiredTo.PLAYER_1
        gs.player_1_deck = build_deck_from_id("ezreal_prodigal_explorer")
        gs.player_2_deck = build_deck_from_id("irelia_nates")
        gs.battlefield_1 = "A"
        gs.battlefield_2 = "B"
        gs.player_1_units = [PlayedUnit(card="A", location="base", exhausted=False, uid=1)]
        eng = GameEngine(game_state=gs)
        out = eng.start()
        self.assertTrue(any(o.startswith("play:move_unit:0") for o in out.player_1_options))
        # Apply CANT_MOVE to that unit; its move options vanish.
        E.execute_effect(_ctx(eng, code="CANT_MOVE", targets=("player_1:0",)))
        self.assertTrue(eng._game_state.player_1_units[0].cant_move)
        out2 = eng.start()
        self.assertFalse(any(o.startswith("play:move_unit:0") for o in out2.player_1_options))


class EmperorsDaisTests(unittest.TestCase):
    def test_return_unit_to_hand(self) -> None:
        eng = _eng(p1=[PlayedUnit(card="Royal Guard", location="battlefield_1", uid=1)])
        eng._game_state.player_1_hand = []
        ctx = _ctx(eng, source="battlefield_1", code="RETURN_UNIT_YOU_CONTROL_HERE_HAND_PLAY_SS_HERE")
        self.assertEqual(E.choice_effect_options(ctx), ["p1-0"])
        E.apply_choice_effect(ctx, "p1-0")
        self.assertEqual(eng._game_state.player_1_units, [])
        self.assertIn("Royal Guard", eng._game_state.player_1_hand)


if __name__ == "__main__":
    unittest.main()
