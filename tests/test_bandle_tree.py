"""Bandle Tree (battlefield): "You may hide an additional card here." Raises the
per-battlefield [Hidden] limit from the default 1 to 2 at its battlefield.
"""

import unittest

from riftbound_engine.engine import GameEngine, GameState, HiddenCard, RequiredTo


class HideLimitTests(unittest.TestCase):
    def _eng(self, bf1_name):
        eng = GameEngine(game_state=GameState())
        eng._game_state.battlefield_1 = bf1_name
        eng._game_state.battlefield_2 = "Plain Field"
        return eng

    def test_default_limit_is_one(self):
        eng = self._eng("Plain Field")
        self.assertEqual(eng._hide_limit("battlefield_1"), 1)

    def test_bandle_tree_raises_limit_to_two(self):
        eng = self._eng("Bandle Tree")
        self.assertEqual(eng._hide_limit("battlefield_1"), 2)

    def test_count_and_limit_interaction(self):
        eng = self._eng("Bandle Tree")
        hidden = eng.player_hidden(RequiredTo.PLAYER_1)
        self.assertEqual(eng._hidden_count_at(RequiredTo.PLAYER_1, "battlefield_1"), 0)
        hidden.append(HiddenCard(card="X", battlefield="battlefield_1", hidden_on_turn=1))
        self.assertEqual(eng._hidden_count_at(RequiredTo.PLAYER_1, "battlefield_1"), 1)
        # Still under Bandle Tree's limit of 2.
        self.assertLess(
            eng._hidden_count_at(RequiredTo.PLAYER_1, "battlefield_1"),
            eng._hide_limit("battlefield_1"),
        )
        hidden.append(HiddenCard(card="Y", battlefield="battlefield_1", hidden_on_turn=1))
        # Now at the limit.
        self.assertEqual(
            eng._hidden_count_at(RequiredTo.PLAYER_1, "battlefield_1"),
            eng._hide_limit("battlefield_1"),
        )


if __name__ == "__main__":
    unittest.main()
