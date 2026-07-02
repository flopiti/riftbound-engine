"""Conquering and holding a battlefield are now DISTINCT events.

Previously both (and "score here") collapsed onto a single ON_CONQUER, so
hold/conquer triggers fired on each other. Now ``award_bf_point`` emits:
  * ON_CONQUER  when control changes hands (via="conquer"),
  * ON_HOLD     when kept across turns at the beginning phase (via="hold"),
  * ON_SCORE    for ANY point (either source).
And the trigger map routes WHEN_CONQUER_HERE/WHEN_HOLD_HERE/WHEN_SCORE_HERE to
exactly one of those."""

import unittest

from riftbound_engine.engine import GameEngine, GameState, RequiredTo
from riftbound_engine.triggers import GameEvent, trigger_matches


def _capture(eng):
    kinds = []
    orig = eng._emit

    def wrap(ev):
        kinds.append(ev.kind)
        return orig(ev)

    eng._emit = wrap
    return kinds


class ConquerHoldEmitTests(unittest.TestCase):
    def test_hold_emits_on_hold_and_score_not_conquer(self):
        eng = GameEngine(game_state=GameState())
        kinds = _capture(eng)
        awarded = eng.award_bf_point(RequiredTo.PLAYER_1, "battlefield_1", via="hold")
        self.assertTrue(awarded)
        self.assertIn("ON_HOLD", kinds)
        self.assertIn("ON_SCORE", kinds)
        self.assertNotIn("ON_CONQUER", kinds)

    def test_conquer_emits_on_conquer_and_score_not_hold(self):
        eng = GameEngine(game_state=GameState())
        kinds = _capture(eng)
        eng.award_bf_point(RequiredTo.PLAYER_2, "battlefield_2", via="conquer")
        self.assertIn("ON_CONQUER", kinds)
        self.assertIn("ON_SCORE", kinds)
        self.assertNotIn("ON_HOLD", kinds)

    def test_per_turn_cap_still_suppresses_second_award(self):
        eng = GameEngine(game_state=GameState())
        self.assertTrue(eng.award_bf_point(RequiredTo.PLAYER_1, "battlefield_1", via="hold"))
        kinds = _capture(eng)
        self.assertFalse(eng.award_bf_point(RequiredTo.PLAYER_1, "battlefield_1", via="conquer"))
        self.assertEqual(kinds, [])  # capped → no events


class ConquerHoldTriggerMapTests(unittest.TestCase):
    def _at(self, kind):
        return GameEvent(kind=kind, controller="player_1", battlefield="battlefield_1")

    def test_hold_here_matches_only_hold(self):
        kw = dict(owner_controller="player_1", owner_location="battlefield_1")
        self.assertTrue(trigger_matches("WHEN_HOLD_HERE", self._at("ON_HOLD"), **kw))
        self.assertFalse(trigger_matches("WHEN_HOLD_HERE", self._at("ON_CONQUER"), **kw))
        self.assertFalse(trigger_matches("WHEN_HOLD_HERE", self._at("ON_SCORE"), **kw))

    def test_conquer_here_matches_only_conquer(self):
        kw = dict(owner_controller="player_1", owner_location="battlefield_1")
        self.assertTrue(trigger_matches("WHEN_CONQUER_HERE", self._at("ON_CONQUER"), **kw))
        self.assertFalse(trigger_matches("WHEN_CONQUER_HERE", self._at("ON_HOLD"), **kw))

    def test_score_here_matches_only_score(self):
        kw = dict(owner_controller="player_1", owner_location="battlefield_1")
        self.assertTrue(trigger_matches("WHEN_SCORE_HERE", self._at("ON_SCORE"), **kw))
        self.assertFalse(trigger_matches("WHEN_SCORE_HERE", self._at("ON_CONQUER"), **kw))
        self.assertFalse(trigger_matches("WHEN_SCORE_HERE", self._at("ON_HOLD"), **kw))


if __name__ == "__main__":
    unittest.main()
