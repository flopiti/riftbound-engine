"""[Tank] (must be assigned combat damage FIRST) and [Backline] (LAST) constrain
which combat kill-sets are legal. A player must assign lethal to higher-priority
enemy units before lower-priority ones: Tank → normal → Backline.
"""

import unittest

from riftbound_engine.engine import (
    GameEngine,
    GameState,
    PlayedUnit,
    PendingCombat,
    RequiredTo,
)

TANK = "Blitzcrank, Impassive"   # printed [Tank]
BACKLINE = "Evelynn, Entrancing"  # printed [Backline]
NORMAL = "Plundering Poro"        # no combat keyword


class KillSetLogicTests(unittest.TestCase):
    def setUp(self):
        self.eng = GameEngine(game_state=GameState())

    def test_tank_must_die_before_normal(self):
        costs = {0: 2, 1: 2}
        tiers = {0: 0, 1: 1}  # 0 = Tank, 1 = normal
        ok = self.eng._kill_set_ok
        self.assertFalse(ok(costs, tiers, {1}, 2))   # can't kill normal, tank alive
        self.assertTrue(ok(costs, tiers, {0}, 2))    # kill the tank — legal
        self.assertFalse(ok(costs, tiers, set(), 2))  # must kill the affordable tank

    def test_unaffordable_tank_protects_everything(self):
        costs = {0: 5, 1: 1}
        tiers = {0: 0, 1: 1}  # tank costs 5, budget 2 → can't kill it
        ok = self.eng._kill_set_ok
        self.assertTrue(ok(costs, tiers, set(), 2))   # kill nothing — tank shields
        self.assertFalse(ok(costs, tiers, {1}, 2))    # can't reach the unit behind

    def test_backline_dies_last(self):
        costs = {0: 1, 1: 1}
        tiers = {0: 1, 1: 2}  # 0 = normal, 1 = Backline
        ok = self.eng._kill_set_ok
        self.assertFalse(ok(costs, tiers, {1}, 1))   # can't kill backline first
        self.assertTrue(ok(costs, tiers, {0}, 1))    # kill the normal first
        self.assertTrue(ok(costs, tiers, {0, 1}, 2))  # both, backline last — ok


class OptionEnumerationTests(unittest.TestCase):
    def _combat(self, defender_cards, budget):
        eng = GameEngine(game_state=GameState())
        gs = eng._game_state
        gs.player_2_units = [
            PlayedUnit(card=c, location="battlefield_1", uid=i + 1)
            for i, c in enumerate(defender_cards)
        ]
        gs.pending_combat = PendingCombat(
            battlefield="battlefield_1",
            player_1_might=budget,
            player_2_might=0,
            initiator=RequiredTo.PLAYER_1,
        )
        return eng

    def test_tank_gated_options(self):
        # Defender: a Tank (idx0, 5 Might) and a normal (idx1, 2 Might); attacker
        # has exactly enough to kill the tank (5).
        eng = self._combat([TANK, NORMAL], budget=5)
        opts = set(eng._assign_damage_options(RequiredTo.PLAYER_1))
        self.assertIn("play:assign_damage:0", opts)      # kill the tank
        self.assertNotIn("play:assign_damage:1", opts)   # can't skip to the normal
        self.assertNotIn("play:assign_damage:", opts)    # must kill the affordable tank

    def test_backline_gated_options(self):
        # Defender: a normal (idx0) and a Backline (idx1), each 2 Might; attacker 2.
        eng = self._combat([NORMAL, BACKLINE], budget=2)
        opts = set(eng._assign_damage_options(RequiredTo.PLAYER_1))
        self.assertIn("play:assign_damage:0", opts)      # kill the normal
        self.assertNotIn("play:assign_damage:1", opts)   # can't kill backline first


if __name__ == "__main__":
    unittest.main()
