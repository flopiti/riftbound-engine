"""Quick-Draw: certain Equipment gain [Reaction] timing and, when played that
way, attach to a unit you control for FREE — you pay only the card cost, NOT
the [Equip] cost. The normal play:equip (with its [Equip] cost) still handles
re-attaching later, e.g. after the host dies."""

import unittest

from riftbound_engine.action_turn.builtins import _quick_draw
from riftbound_engine.action_turn.context import ActionTurnContext
from riftbound_engine.csv_data import card_has_quick_draw
from riftbound_engine.engine import GameEngine, GameState, PlayedUnit, RequiredTo


def _ctx(eng: GameEngine, payload: str) -> ActionTurnContext:
    return ActionTurnContext(engine=eng, actor=RequiredTo.PLAYER_1, verb="quick_draw", payload=payload)


class QuickDrawDetectionTests(unittest.TestCase):
    def test_detects_intrinsic_quick_draw(self) -> None:
        for n in ("Long Sword", "Sterak's Gage", "Cloth Armor", "Spinning Axe"):
            self.assertTrue(card_has_quick_draw(n), n)

    def test_non_quick_draw_cards(self) -> None:
        self.assertFalse(card_has_quick_draw("Brutalizer"))  # equipment, but no [Quick-Draw]
        self.assertFalse(card_has_quick_draw("Ravenbloom Student"))  # not equipment


class QuickDrawPlayTests(unittest.TestCase):
    def _engine(self, energy: int = 5, power: dict | None = None) -> GameEngine:
        gs = GameState(
            current_player=RequiredTo.PLAYER_1,
            total_turn_number=4,
            player_1_hand=["Long Sword"],  # card cost 2 Energy; [Equip] cost 1 Fury power
            player_1_units=[PlayedUnit(card="Ravenbloom Student", location="battlefield_1")],
            player_1_energy=energy,
            player_1_power=dict(power or {"Fury": 2}),
        )
        eng = GameEngine(game_state=gs)
        eng._backfill_unit_uids()
        return eng

    def test_plays_and_attaches_for_free(self) -> None:
        eng = self._engine(energy=5, power={"Fury": 2})
        _quick_draw(_ctx(eng, "0:player_1:0"))
        gs = eng._game_state
        self.assertEqual(gs.player_1_hand, [])  # left hand
        self.assertEqual(len(gs.player_1_gears), 1)
        gear = gs.player_1_gears[0]
        self.assertEqual(gear.card, "Long Sword")
        self.assertEqual(gear.attached_uid, gs.player_1_units[0].uid)  # attached immediately
        self.assertEqual(gear.attached_on_turn, 4)
        self.assertEqual(eng.player_energy(RequiredTo.PLAYER_1), 3)  # 5 - 2 card cost
        # The [Equip] cost (1 Fury power) was NOT paid — Quick-Draw skips it.
        self.assertEqual(gs.player_1_power.get("Fury", 0), 2)

    def test_allowed_during_open_chain(self) -> None:
        eng = self._engine()
        eng._game_state.pending_chain = object()  # a reaction window is open
        _quick_draw(_ctx(eng, "0:player_1:0"))  # should not raise
        self.assertEqual(len(eng._game_state.player_1_gears), 1)

    def test_rejected_during_combat(self) -> None:
        eng = self._engine()
        eng._game_state.pending_combat = object()
        with self.assertRaises(ValueError):
            _quick_draw(_ctx(eng, "0:player_1:0"))

    def test_rejected_onto_enemy_unit(self) -> None:
        eng = self._engine()
        with self.assertRaises(ValueError):
            _quick_draw(_ctx(eng, "0:player_2:0"))

    def test_rejected_for_non_quick_draw_card(self) -> None:
        gs = GameState(
            current_player=RequiredTo.PLAYER_1,
            player_1_hand=["Brutalizer"],
            player_1_units=[PlayedUnit(card="Ravenbloom Student", location="base")],
            player_1_energy=5,
            player_1_power={"Calm": 2},
        )
        eng = GameEngine(game_state=gs)
        eng._backfill_unit_uids()
        with self.assertRaises(ValueError):
            _quick_draw(_ctx(eng, "0:player_1:0"))


class QuickDrawIntentTests(unittest.TestCase):
    """The pre-costed Quick-Draw picker chips offered to the UI / search."""

    def _engine(self) -> GameEngine:
        gs = GameState(
            current_player=RequiredTo.PLAYER_1,
            total_turn_number=4,
            player_1_hand=["Long Sword"],
            player_1_units=[PlayedUnit(card="Ravenbloom Student", location="battlefield_1")],
            player_1_energy=5,
            player_1_power={"Fury": 2},
        )
        eng = GameEngine(game_state=gs)
        eng._backfill_unit_uids()
        return eng

    def test_offered_on_action_turn(self) -> None:
        from riftbound_engine.shortcuts import compute_quick_draw_intents

        intents = compute_quick_draw_intents(self._engine(), RequiredTo.PLAYER_1)
        self.assertEqual(len(intents), 1)
        it = intents[0]
        self.assertEqual(it.play_action, "quick_draw")
        self.assertEqual(it.card_name, "Long Sword")
        self.assertEqual(it.combos[0].final_action, "play:quick_draw:0:player_1:0")

    def test_not_offered_without_a_unit_to_attach_to(self) -> None:
        from riftbound_engine.shortcuts import compute_quick_draw_intents

        gs = GameState(
            current_player=RequiredTo.PLAYER_1,
            player_1_hand=["Long Sword"],
            player_1_units=[],
            player_1_energy=5,
            player_1_power={"Fury": 2},
        )
        eng = GameEngine(game_state=gs)
        self.assertEqual(compute_quick_draw_intents(eng, RequiredTo.PLAYER_1), [])

    def test_offered_in_a_reaction_window(self) -> None:
        from riftbound_engine.engine import ChainItem, PendingChain
        from riftbound_engine.shortcuts import compute_quick_draw_intents

        eng = self._engine()
        # Opponent's turn, a chain is open and P1 holds priority → reaction window.
        eng._game_state.current_player = RequiredTo.PLAYER_2
        eng._game_state.pending_chain = PendingChain(
            items=[ChainItem(actor=RequiredTo.PLAYER_2, card="Gust")],
            priority=RequiredTo.PLAYER_1,
            consecutive_passes=0,
        )
        intents = compute_quick_draw_intents(eng, RequiredTo.PLAYER_1)
        self.assertEqual(len(intents), 1)  # P1 may Quick-Draw as a reaction


if __name__ == "__main__":
    unittest.main()
