"""Stacked Deck (LOOK_TOP_3_1_HAND_RECYCLE): look at the top 3 cards of your Main
Deck, put 1 into your hand, and recycle the rest (to the bottom of the deck).

Driven at the effect layer: open the choice via GameEngine._run_effect_codes
(the same path a resolving spell uses), inspect the enumerated ``lib-<i>``
options, then answer via a helper mirroring the choose_effect_target apply path.
"""

import unittest

from riftbound_engine.effects import EffectContext, apply_choice_effect
from riftbound_engine.engine import GameEngine, GameState, RequiredTo


def _engine(library, hand=None):
    eng = GameEngine(game_state=GameState())
    eng._game_state.player_1_library = list(library)
    eng._game_state.player_1_hand = list(hand or [])
    return eng


def _open(eng):
    eng._run_effect_codes(
        controller=RequiredTo.PLAYER_1,
        source=None, trigger="", event_kind="",
        label="Stacked Deck",
        codes=["LOOK_TOP_3_1_HAND_RECYCLE"],
    )
    return eng._game_state.pending_effect_choice


def _answer(eng, token):
    ch = eng._game_state.pending_effect_choice
    eng._game_state.pending_effect_choice = None
    ctx = EffectContext(
        engine=eng, controller=ch.actor, source=ch.source, code=ch.code,
        trigger=ch.trigger, event_kind=ch.event_kind,
        targets=tuple(ch.continuation_targets),
    )
    return apply_choice_effect(ctx, token)


class StackedDeckTests(unittest.TestCase):
    def test_looks_at_top_three_only(self):
        eng = _engine(["A", "B", "C", "D", "E"])
        choice = _open(eng)
        self.assertEqual(choice.options, ["lib-0", "lib-1", "lib-2"])
        self.assertFalse(choice.optional)  # must take one

    def test_pick_to_hand_and_recycle_rest_to_bottom(self):
        eng = _engine(["A", "B", "C", "D", "E"], hand=["X"])
        _open(eng)
        _answer(eng, "lib-1")  # take B
        gs = eng._game_state
        self.assertEqual(gs.player_1_hand, ["X", "B"])
        # A and C (the other looked-at cards) recycled to the bottom, in order;
        # D, E (untouched rest) now on top.
        self.assertEqual(gs.player_1_library, ["D", "E", "A", "C"])

    def test_short_library_looks_at_what_exists(self):
        eng = _engine(["A", "B"])
        choice = _open(eng)
        self.assertEqual(choice.options, ["lib-0", "lib-1"])
        _answer(eng, "lib-0")  # take A
        gs = eng._game_state
        self.assertEqual(gs.player_1_hand, ["A"])
        self.assertEqual(gs.player_1_library, ["B"])  # B recycled to bottom

    def test_empty_library_fizzles(self):
        eng = _engine([])
        choice = _open(eng)
        self.assertIsNone(choice)  # nothing to look at → no pause
        self.assertEqual(eng._game_state.player_1_hand, [])


if __name__ == "__main__":
    unittest.main()
