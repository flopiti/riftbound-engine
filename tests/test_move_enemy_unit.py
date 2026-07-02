"""MAY_MOVE_ENEMY_UNIT (Charm: "Move an enemy unit").

The enemy unit AND its destination are the spell's Spell Choice Requirement
("MOVE ENEMY UNIT"), so by resolution the effect receives the chosen unit ref
followed by a ``move_dest:<location>`` token. The move is a FREE relocation:
no exhaust, and it may go from any location to any other (base <-> either
battlefield)."""

import unittest

from riftbound_engine import effects as E
from riftbound_engine.engine import GameEngine, GameState, PlayedUnit, RequiredTo


def _run(engine, targets, controller=RequiredTo.PLAYER_1):
    ctx = E.EffectContext(
        engine=engine,
        controller=controller,
        source=None,
        code="MAY_MOVE_ENEMY_UNIT",
        targets=tuple(targets),
    )
    return E.execute_effect(ctx)


class MoveEnemyUnitTests(unittest.TestCase):
    def _engine(self, location="base", exhausted=False):
        gs = GameState(
            player_2_units=[PlayedUnit(card="Foe", location=location, exhausted=exhausted)]
        )
        return GameEngine(game_state=gs)

    def test_is_registered_as_a_targeted_effect(self) -> None:
        self.assertTrue(E.is_implemented("MAY_MOVE_ENEMY_UNIT"))
        self.assertTrue(E.effect_uses_targets("MAY_MOVE_ENEMY_UNIT"))

    def test_relocates_enemy_unit_base_to_battlefield(self) -> None:
        eng = self._engine(location="base")
        self.assertTrue(_run(eng, ["player_2:0", "move_dest:battlefield_1"]))
        self.assertEqual(eng._game_state.player_2_units[0].location, "battlefield_1")

    def test_move_does_not_exhaust_the_unit(self) -> None:
        eng = self._engine(location="base", exhausted=False)
        _run(eng, ["player_2:0", "move_dest:battlefield_2"])
        self.assertFalse(eng._game_state.player_2_units[0].exhausted)

    def test_keeps_an_already_exhausted_unit_exhausted(self) -> None:
        # The move never touches exhaust state either way.
        eng = self._engine(location="base", exhausted=True)
        _run(eng, ["player_2:0", "move_dest:battlefield_1"])
        self.assertTrue(eng._game_state.player_2_units[0].exhausted)

    def test_battlefield_to_battlefield_is_allowed(self) -> None:
        eng = self._engine(location="battlefield_1")
        _run(eng, ["player_2:0", "move_dest:battlefield_2"])
        self.assertEqual(eng._game_state.player_2_units[0].location, "battlefield_2")

    def test_battlefield_to_base_is_allowed(self) -> None:
        eng = self._engine(location="battlefield_1")
        _run(eng, ["player_2:0", "move_dest:base"])
        self.assertEqual(eng._game_state.player_2_units[0].location, "base")

    def test_unknown_destination_is_ignored(self) -> None:
        eng = self._engine(location="base")
        _run(eng, ["player_2:0", "move_dest:nowhere"])
        self.assertEqual(eng._game_state.player_2_units[0].location, "base")

    def test_missing_destination_token_is_a_noop(self) -> None:
        eng = self._engine(location="base")
        _run(eng, ["player_2:0"])
        self.assertEqual(eng._game_state.player_2_units[0].location, "base")

    def test_gone_target_does_not_raise(self) -> None:
        # Unit ref points nowhere (already left play) → no crash, no change.
        eng = self._engine(location="base")
        _run(eng, ["player_2:5", "move_dest:battlefield_1"])
        self.assertEqual(eng._game_state.player_2_units[0].location, "base")


if __name__ == "__main__":
    unittest.main()
