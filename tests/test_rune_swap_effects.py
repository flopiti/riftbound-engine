"""Standalone effects from the tagging batch:
  * ADD_1_CALM_RUNE          — Seal of Focus
  * READY_UP_TO_4_RUNES      — Sona, Harmonious
  * SWAP_ME_WITH_FRIENDLY_UNIT — Tideturner (choice effect)
"""

import unittest

from riftbound_engine.effects import EffectContext, apply_choice_effect
from riftbound_engine.engine import GameEngine, GameState, PlayedUnit, RequiredTo, Rune


def _run(eng, source, code, targets=None):
    eng._run_effect_codes(
        controller=RequiredTo.PLAYER_1, source=source, trigger="", event_kind="",
        label=code, codes=[code], targets=targets,
    )


class AddCalmRuneTests(unittest.TestCase):
    def test_adds_ready_calm_rune(self):
        eng = GameEngine(game_state=GameState())
        eng._game_state.player_1_runes = []
        _run(eng, None, "ADD_1_CALM_RUNE")
        pool = eng._game_state.player_1_runes
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0].domain, "Calm")
        self.assertFalse(pool[0].exhausted)  # enters ready


class Ready4RunesTests(unittest.TestCase):
    def test_readies_up_to_four_exhausted(self):
        eng = GameEngine(game_state=GameState())
        eng._game_state.player_1_runes = [Rune("Calm", exhausted=True) for _ in range(6)]
        _run(eng, None, "READY_UP_TO_4_RUNES")
        ready = sum(1 for r in eng._game_state.player_1_runes if not r.exhausted)
        self.assertEqual(ready, 4)  # exactly 4 flipped, 2 still exhausted

    def test_caps_at_available(self):
        eng = GameEngine(game_state=GameState())
        eng._game_state.player_1_runes = [Rune("Calm", exhausted=True) for _ in range(2)]
        _run(eng, None, "READY_UP_TO_4_RUNES")
        self.assertTrue(all(not r.exhausted for r in eng._game_state.player_1_runes))


class SwapTests(unittest.TestCase):
    def _answer(self, eng, token):
        ch = eng._game_state.pending_effect_choice
        eng._game_state.pending_effect_choice = None
        ctx = EffectContext(
            engine=eng, controller=ch.actor, source=ch.source, code=ch.code,
            trigger=ch.trigger, event_kind=ch.event_kind,
            targets=tuple(ch.continuation_targets),
        )
        return apply_choice_effect(ctx, token)

    def test_swaps_locations_with_chosen_unit(self):
        eng = GameEngine(game_state=GameState())
        eng._game_state.player_1_units = [
            PlayedUnit(card="Tideturner", location="base", uid=1),        # source (index 0)
            PlayedUnit(card="Ally", location="battlefield_1", uid=2),     # elsewhere
        ]
        _run(eng, "player_1:0", "SWAP_ME_WITH_FRIENDLY_UNIT")
        choice = eng._game_state.pending_effect_choice
        self.assertIsNotNone(choice)
        self.assertEqual(choice.options, ["p1-1"])  # only the unit elsewhere
        self._answer(eng, "p1-1")
        units = eng._game_state.player_1_units
        self.assertEqual(units[0].location, "battlefield_1")  # Tideturner moved
        self.assertEqual(units[1].location, "base")           # Ally took its spot

    def test_optional_when_no_unit_elsewhere(self):
        eng = GameEngine(game_state=GameState())
        eng._game_state.player_1_units = [PlayedUnit(card="Tideturner", location="base", uid=1)]
        _run(eng, "player_1:0", "SWAP_ME_WITH_FRIENDLY_UNIT")
        # No swap partner → no pause.
        self.assertIsNone(eng._game_state.pending_effect_choice)


if __name__ == "__main__":
    unittest.main()
