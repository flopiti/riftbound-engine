"""INCREASE_MIGHT_TO (Convergent Mutation): of the two chosen friendly units,
raise the lower-Might one up to the higher one's CURRENT Might this turn.
'Increase to' only raises; equal Might is a no-op; the buff expires at end of
turn like any this-turn bonus."""

from __future__ import annotations

import unittest

from riftbound_engine import GameEngine, RequiredTo
from riftbound_engine.effects import EffectContext, execute_effect
from riftbound_engine.engine import GameState, PlayedUnit

# Two real CSV units with different printed Might (Ravenbloom Student = 2).
LOW = "Ravenbloom Student"   # printed Might 2
HIGH = "Blazing Scorcher"    # printed Might 5


def _engine(p1_units):
    gs = GameState()
    gs.started = True
    gs.current_player = RequiredTo.PLAYER_1
    gs.player_1_units = list(p1_units)
    return GameEngine(game_state=gs)


def _run(eng, targets):
    ctx = EffectContext(
        engine=eng,
        controller=RequiredTo.PLAYER_1,
        source=None,
        code="INCREASE_MIGHT_TO",
        targets=tuple(targets),
    )
    execute_effect(ctx)


class IncreaseMightToTests(unittest.TestCase):
    def test_raises_lower_to_higher(self) -> None:
        eng = _engine([
            PlayedUnit(card=LOW, location="base"),    # might 2
            PlayedUnit(card=HIGH, location="base"),   # might 5
        ])
        _run(eng, ["player_1:0", "player_1:1"])
        u = eng._game_state.player_1_units
        self.assertEqual(u[0].bonus_might, 3)   # 2 -> 5
        self.assertEqual(u[1].bonus_might, 0)   # higher unchanged

    def test_order_independent(self) -> None:
        # Same result whether the higher unit is listed first.
        eng = _engine([
            PlayedUnit(card=HIGH, location="base"),
            PlayedUnit(card=LOW, location="base"),
        ])
        _run(eng, ["player_1:0", "player_1:1"])
        u = eng._game_state.player_1_units
        self.assertEqual(u[0].bonus_might, 0)   # higher unchanged
        self.assertEqual(u[1].bonus_might, 3)   # lower 2 -> 5

    def test_uses_current_buffed_might_of_reference(self) -> None:
        # Reference already buffed +2 (might 7); low (2) rises to 7 → +5.
        hi = PlayedUnit(card=HIGH, location="base")
        hi.bonus_might = 2
        eng = _engine([PlayedUnit(card=LOW, location="base"), hi])
        _run(eng, ["player_1:0", "player_1:1"])
        self.assertEqual(eng._game_state.player_1_units[0].bonus_might, 5)

    def test_equal_might_no_change(self) -> None:
        eng = _engine([
            PlayedUnit(card=LOW, location="base"),
            PlayedUnit(card=LOW, location="base"),
        ])
        _run(eng, ["player_1:0", "player_1:1"])
        u = eng._game_state.player_1_units
        self.assertEqual([x.bonus_might for x in u], [0, 0])

    def test_fizzles_with_one_target_gone(self) -> None:
        eng = _engine([PlayedUnit(card=LOW, location="base")])
        _run(eng, ["player_1:0", "player_1:1"])  # second ref doesn't exist
        self.assertEqual(eng._game_state.player_1_units[0].bonus_might, 0)

    def test_buff_expires_end_of_turn(self) -> None:
        eng = _engine([
            PlayedUnit(card=LOW, location="base"),
            PlayedUnit(card=HIGH, location="base"),
        ])
        _run(eng, ["player_1:0", "player_1:1"])
        self.assertEqual(eng._game_state.player_1_units[0].bonus_might, 3)
        eng._advance_turn()
        self.assertEqual(eng._game_state.player_1_units[0].bonus_might, 0)


if __name__ == "__main__":
    unittest.main()
