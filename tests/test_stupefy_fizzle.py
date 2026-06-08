"""A spell's UNtargeted effects still resolve when its target vanishes.

Stupefy is "[Reaction] Give a unit -1 Might this turn (min 1). Draw 1." If the
chosen unit is removed in reaction before Stupefy resolves, the "-1 Might" part
(TARGETED) fizzles, but "Draw 1" (UNtargeted) must still happen. We resolve a
Stupefy chain item whose captured target uid is no longer in play and assert
the caster drew exactly one card and no Might was changed."""

import unittest
from unittest import mock

from riftbound_engine import abilities as _abilities
from riftbound_engine.abilities import Ability
from riftbound_engine.csv_data import card_spell_requirement_of
from riftbound_engine.engine import ChainItem, GameEngine, PlayedUnit, RequiredTo

_STUPEFY = (Ability(active_effects=("GIVE_UNIT_-1M", "DRAW_1")),)


def _fake_abilities(name: str):
    return _STUPEFY if (name or "").strip().lower() == "stupefy" else ()


class StupefyFizzleDrawTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch("riftbound_engine.abilities.triggered_abilities_for", _fake_abilities)
        p.start()
        self.addCleanup(p.stop)

    def _engine(self):
        eng = GameEngine()
        gs = eng._game_state
        gs.player_1_hand = []
        gs.player_1_library = ["CardA", "CardB"]  # something to draw
        gs.player_2_units = []  # the target is GONE (uid below not in play)
        return eng, gs

    def _stupefy_item(self) -> ChainItem:
        # Targeted a player_2 unit (uid 777) that is no longer in play.
        return ChainItem(
            actor=RequiredTo.PLAYER_1,
            card="Stupefy",
            requirement=card_spell_requirement_of("Stupefy") or "",
            targets=["player_2:0"],
            target_uids=[777],
        )

    def test_draw_still_happens_when_target_gone(self):
        eng, gs = self._engine()
        before = len(gs.player_1_hand)
        eng._run_spell_effects(self._stupefy_item())
        # Untargeted Draw 1 resolved despite the missing target.
        self.assertEqual(len(gs.player_1_hand), before + 1)

    def test_targeted_part_does_nothing_when_target_gone(self):
        eng, gs = self._engine()
        # A bystander P2 unit must NOT be debuffed — the -1 Might had no legal
        # target, so it fizzles rather than splashing onto someone else.
        gs.player_2_units = [PlayedUnit(card="Bystander", location="base", bonus_might=0, uid=1)]
        eng._run_spell_effects(self._stupefy_item())
        self.assertEqual(gs.player_2_units[0].bonus_might, 0)


if __name__ == "__main__":
    unittest.main()
