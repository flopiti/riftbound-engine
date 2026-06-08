"""Gold gear tokens.

Two halves:
  * the ``PLAY_GOLD_EXHAUSTED`` effect (Plundering Poro et al.) spawns a Gold
    gear token under the controller, entering EXHAUSTED; and
  * the token's own activated ability "Kill this, exhaust: [Add] 1 Power of any
    domain" — surfaced as ``play:use_gold:<gear_index>:<domain>`` and handled by
    ``_use_gold``. Using a READY token kills it (tokens leave the game, not to
    trash) and adds 1 Power of the chosen domain. An exhausted token can't be
    used until it readies on the owner's Awake step.
"""

import unittest

from riftbound_engine import effects as E
from riftbound_engine.action_turn.builtins import _use_gold
from riftbound_engine.action_turn.context import ActionTurnContext
from riftbound_engine.csv_data import card_type_of
from riftbound_engine.engine import (
    GameEngine,
    GameState,
    PlayedGear,
    RequiredTo,
    RequiredStep,
)


def _use_gold_now(eng: GameEngine, payload: str, actor=RequiredTo.PLAYER_1) -> None:
    _use_gold(ActionTurnContext(engine=eng, actor=actor, verb="use_gold", payload=payload))


class PlayGoldExhaustedEffectTests(unittest.TestCase):
    def test_effect_spawns_an_exhausted_gold_gear_for_the_controller(self) -> None:
        eng = GameEngine(game_state=GameState())
        ctx = E.EffectContext(
            engine=eng,
            controller=RequiredTo.PLAYER_1,
            source="player_1:0",
            code="PLAY_GOLD_EXHAUSTED",
        )
        self.assertTrue(E.execute_effect(ctx))
        gears = eng._game_state.player_1_gears
        self.assertEqual(len(gears), 1)
        self.assertEqual(gears[0].card, "Gold")
        self.assertTrue(gears[0].exhausted)
        self.assertEqual(gears[0].location, "base")
        # The opponent gets nothing.
        self.assertEqual(eng._game_state.player_2_gears, [])

    def test_effect_goes_to_the_right_controller(self) -> None:
        eng = GameEngine(game_state=GameState())
        ctx = E.EffectContext(
            engine=eng, controller=RequiredTo.PLAYER_2, source=None, code="PLAY_GOLD_EXHAUSTED"
        )
        E.execute_effect(ctx)
        self.assertEqual(len(eng._game_state.player_2_gears), 1)
        self.assertEqual(eng._game_state.player_1_gears, [])

    def test_gold_is_a_gear_in_the_csv(self) -> None:
        # The token name resolves to a Gear card type, so it renders/behaves
        # like any other gear (anchored at base, re-readies on Awake).
        self.assertEqual(card_type_of("Gold"), "Gear")


class UseGoldActionTests(unittest.TestCase):
    def _engine_with_ready_gold(self) -> GameEngine:
        gs = GameState(player_1_gears=[PlayedGear(card="Gold", location="base", exhausted=False)])
        return GameEngine(game_state=gs)

    def test_ready_gold_adds_one_power_of_chosen_domain_and_dies(self) -> None:
        eng = self._engine_with_ready_gold()
        _use_gold_now(eng, "0:Fury")
        # Token left play entirely (not to trash).
        self.assertEqual(eng._game_state.player_1_gears, [])
        self.assertEqual(eng._game_state.player_1_trash, [])
        self.assertEqual(eng.player_power(RequiredTo.PLAYER_1).get("Fury", 0), 1)

    def test_any_of_the_six_domains_is_accepted(self) -> None:
        for dom in ("Fury", "Calm", "Mind", "Body", "Chaos", "Order"):
            eng = self._engine_with_ready_gold()
            _use_gold_now(eng, f"0:{dom}")
            self.assertEqual(eng.player_power(RequiredTo.PLAYER_1).get(dom, 0), 1)

    def test_exhausted_gold_cannot_be_used(self) -> None:
        gs = GameState(player_1_gears=[PlayedGear(card="Gold", location="base", exhausted=True)])
        eng = GameEngine(game_state=gs)
        with self.assertRaises(ValueError):
            _use_gold_now(eng, "0:Fury")
        # Still on the board, no Power produced.
        self.assertEqual(len(eng._game_state.player_1_gears), 1)
        self.assertEqual(eng.player_power(RequiredTo.PLAYER_1), {})

    def test_rejects_unknown_domain(self) -> None:
        eng = self._engine_with_ready_gold()
        with self.assertRaises(ValueError):
            _use_gold_now(eng, "0:Colorless")
        self.assertEqual(len(eng._game_state.player_1_gears), 1)

    def test_rejects_non_gold_gear(self) -> None:
        gs = GameState(player_1_gears=[PlayedGear(card="Blighted Battleaxe", location="base")])
        eng = GameEngine(game_state=gs)
        with self.assertRaises(ValueError):
            _use_gold_now(eng, "0:Fury")

    def test_rejects_out_of_range_index(self) -> None:
        eng = self._engine_with_ready_gold()
        with self.assertRaises(ValueError):
            _use_gold_now(eng, "5:Fury")

    def test_rejects_malformed_payload(self) -> None:
        eng = self._engine_with_ready_gold()
        for bad in ("", "0", "Fury", "0:", ":Fury"):
            with self.assertRaises(ValueError):
                _use_gold_now(eng, bad)

    def test_rejected_during_open_chain(self) -> None:
        eng = self._engine_with_ready_gold()
        eng._game_state.pending_chain = object()
        with self.assertRaises(ValueError):
            _use_gold_now(eng, "0:Fury")
        self.assertEqual(len(eng._game_state.player_1_gears), 1)

    def test_rejected_during_showdown_and_combat(self) -> None:
        for attr in ("pending_showdown", "pending_combat"):
            eng = self._engine_with_ready_gold()
            setattr(eng._game_state, attr, object())
            with self.assertRaises(ValueError):
                _use_gold_now(eng, "0:Fury")


def _drive_to_action_turn(engine: GameEngine):
    """Run the standard start sequence so player_1 is sitting on their action
    turn (mirrors the setup in test_engine)."""
    first = engine.start()
    engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
    second = engine.apply_action(
        action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_2
    )
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


class UseGoldOptionSurfacingTests(unittest.TestCase):
    def test_ready_gold_token_surfaces_one_option_per_domain(self) -> None:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        ready = _drive_to_action_turn(engine)
        self.assertEqual(ready.required_action.name, RequiredStep.ACTION_TURN)
        self.assertEqual(ready.required_action.actor, RequiredTo.PLAYER_1)

        # Drop one ready and one exhausted Gold token onto P1's base.
        engine._game_state.player_1_gears.append(
            PlayedGear(card="Gold", location="base", exhausted=False)
        )
        engine._game_state.player_1_gears.append(
            PlayedGear(card="Gold", location="base", exhausted=True)
        )
        out = engine.start()
        gold_opts = [o for o in out.player_1_options if o.startswith("play:use_gold:")]
        # Only the READY token (index 0) is offered, one option per domain.
        self.assertEqual(
            sorted(gold_opts),
            sorted(
                f"play:use_gold:0:{d}" for d in ("Fury", "Calm", "Mind", "Body", "Chaos", "Order")
            ),
        )
        # The exhausted token (index 1) is never offered.
        self.assertFalse(any(o.startswith("play:use_gold:1:") for o in gold_opts))


if __name__ == "__main__":
    unittest.main()
