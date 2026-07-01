"""Fizz, Trickster: "When you play me, you may play a spell from your trash with
Energy cost <= 3, ignoring its Energy cost." Energy is free; Power is still paid;
the spell is cast through the normal play path.
"""

import unittest

from riftbound_engine.effects import EffectContext, apply_choice_effect
from riftbound_engine.engine import GameEngine, GameState, RequiredTo


def _open(eng, controller=RequiredTo.PLAYER_1):
    eng._run_effect_codes(
        controller=controller, source=None, trigger="", event_kind="",
        label="Fizz", codes=["PLAY_SPELL_FROM_TRASH_LE3"],
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


class FizzTests(unittest.TestCase):
    def test_offers_only_le3_trash_spells(self):
        eng = GameEngine(game_state=GameState())
        gs = eng._game_state
        gs.player_1_hand = []
        gs.player_1_trash = ["Acceptable Losses", "Guardian Angel"]  # spell (1E) + non-spell
        choice = _open(eng)
        self.assertIsNotNone(choice)
        self.assertEqual(choice.options, ["ts-0"])  # only the ≤3 spell
        self.assertTrue(choice.optional)  # "you may"

    def test_plays_spell_from_trash_free_energy(self):
        eng = GameEngine(game_state=GameState())
        gs = eng._game_state
        gs.player_1_hand = []
        gs.player_1_trash = ["Acceptable Losses"]  # 1 Energy, no Power, no requirement
        eng.add_energy(RequiredTo.PLAYER_1, 0)  # no energy — proves it's free
        _open(eng)
        _answer(eng, "ts-0")
        # Left the trash and was actually cast (into the spells pile / chain).
        self.assertNotIn("Acceptable Losses", gs.player_1_trash)
        cast = "Acceptable Losses" in gs.player_1_spells or (
            gs.pending_chain is not None
            and any(it.card == "Acceptable Losses" for it in gs.pending_chain.items)
        )
        self.assertTrue(cast)
        self.assertEqual(eng.player_energy(RequiredTo.PLAYER_1), 0)  # net zero (free)

    def test_empty_trash_no_choice(self):
        eng = GameEngine(game_state=GameState())
        eng._game_state.player_1_hand = []
        eng._game_state.player_1_trash = []
        self.assertIsNone(_open(eng))  # nothing to play → no pause


if __name__ == "__main__":
    unittest.main()
