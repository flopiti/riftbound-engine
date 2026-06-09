"""GIVE_FRIENDLY_+1M_AND_+1M_IF_ALONE (En Garde): give a friendly unit +1 Might
this turn, then +1 more if it's the ONLY unit its controller has at that unit's
location ("there"). So +2 when alone, +1 when it has a buddy there."""

import unittest

from riftbound_engine import effects as E
from riftbound_engine.effects import EffectContext
from riftbound_engine.engine import GameEngine, GameState, PlayedUnit, RequiredTo

CODE = "GIVE_FRIENDLY_+1M_AND_+1M_IF_ALONE"


def _run(eng, target_ref):
    E.execute_effect(
        EffectContext(
            engine=eng,
            controller=RequiredTo.PLAYER_1,
            source=None,
            code=CODE,
            targets=(target_ref,),
        )
    )


class EnGardeTests(unittest.TestCase):
    def test_alone_gives_plus_two(self):
        eng = GameEngine(
            game_state=GameState(
                player_1_units=[PlayedUnit(card="Plundering Poro", location="battlefield_1", uid=1)]
            )
        )
        _run(eng, "player_1:0")
        self.assertEqual(eng._game_state.player_1_units[0].bonus_might, 2)

    def test_with_a_buddy_there_gives_plus_one(self):
        eng = GameEngine(
            game_state=GameState(
                player_1_units=[
                    PlayedUnit(card="Plundering Poro", location="battlefield_1", uid=1),
                    PlayedUnit(card="Lonely Poro", location="battlefield_1", uid=2),
                ]
            )
        )
        _run(eng, "player_1:0")
        self.assertEqual(eng._game_state.player_1_units[0].bonus_might, 1)

    def test_buddy_elsewhere_still_alone_there(self):
        # A second unit at a DIFFERENT location doesn't count — the target is
        # still alone "there".
        eng = GameEngine(
            game_state=GameState(
                player_1_units=[
                    PlayedUnit(card="Plundering Poro", location="battlefield_1", uid=1),
                    PlayedUnit(card="Lonely Poro", location="base", uid=2),
                ]
            )
        )
        _run(eng, "player_1:0")
        self.assertEqual(eng._game_state.player_1_units[0].bonus_might, 2)

    def test_only_enemy_unit_there_does_not_count(self):
        # "the only unit YOU control there" — an enemy unit at the same spot is
        # irrelevant; the target is alone among the caster's own units.
        eng = GameEngine(
            game_state=GameState(
                player_1_units=[PlayedUnit(card="Plundering Poro", location="battlefield_1", uid=1)],
                player_2_units=[PlayedUnit(card="Lonely Poro", location="battlefield_1", uid=2)],
            )
        )
        _run(eng, "player_1:0")
        self.assertEqual(eng._game_state.player_1_units[0].bonus_might, 2)


if __name__ == "__main__":
    unittest.main()
