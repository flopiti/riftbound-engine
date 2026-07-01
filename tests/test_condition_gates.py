"""Ability-level condition gates evaluated in GameEngine._emit / _condition_met:

  * IF_DIED_ALONE        — Lonely Poro's Deathknell
  * IF_1+_UNIT_MIGHTY    — Sunken Temple's conquer trigger
  * WHILE_AT_BATTLEFIELD — Vex, Apathetic's on-opponent-play trigger

Hermetic: a fake taxonomy is patched in and the underlying event driven through
_emit, then the queued triggered abilities are inspected — the same approach as
test_new_triggers / test_first_choose_friendly. An ability fires iff its
condition holds; an unimplemented condition must fail closed.
"""

from __future__ import annotations

import random
import unittest
from unittest import mock

from riftbound_engine import GameEngine
from riftbound_engine import abilities as A
from riftbound_engine.engine import PlayedUnit
from riftbound_engine.triggers import GameEvent


def _ability(trigger: str, conditions: tuple[str, ...], *effects: str) -> tuple:
    return (
        A.Ability(
            triggers=(trigger,),
            conditions=conditions,
            active_effects=tuple(effects) or ("DRAW_1",),
        ),
    )


def _engine() -> GameEngine:
    eng = GameEngine(rng=random.Random(1))
    gs = eng._game_state
    gs.started = True
    gs.abcd_a_done = gs.abcd_b_done = gs.abcd_c_done = gs.abcd_d_done = True
    return eng


def _fire(eng: GameEngine, taxonomy: dict, event: GameEvent):
    eng._trigger_queue = []
    with mock.patch.object(
        A, "triggered_abilities_for", side_effect=lambda n: taxonomy.get(n, ())
    ):
        eng._emit(event)
    return eng._trigger_queue


class IfDiedAloneTests(unittest.TestCase):
    def _run(self, others: list[PlayedUnit]):
        eng = _engine()
        eng._game_state.player_1_units = [
            PlayedUnit(card="Lonely Poro", location="battlefield_1", exhausted=False),
            *others,
        ]
        tax = {"Lonely Poro": _ability("DEATHKNELL", ("IF_DIED_ALONE",), "DRAW_1")}
        return _fire(
            eng,
            tax,
            GameEvent(kind="ON_DEATH", controller="player_1", source="player_1:0",
                      battlefield="battlefield_1"),
        )

    def test_fires_when_alone(self) -> None:
        q = self._run(others=[])
        self.assertEqual([te.trigger for te in q], ["DEATHKNELL"])

    def test_fires_when_only_company_is_elsewhere(self) -> None:
        q = self._run(others=[PlayedUnit(card="Ally", location="battlefield_2")])
        self.assertEqual([te.source for te in q], ["player_1:0"])

    def test_blocked_when_a_friendly_unit_shares_location(self) -> None:
        q = self._run(others=[PlayedUnit(card="Ally", location="battlefield_1")])
        self.assertEqual(q, [])


class If1UnitMightyTests(unittest.TestCase):
    def _run(self, conqueror_units: list[PlayedUnit]):
        eng = _engine()
        eng._game_state.battlefield_1 = "Sunken Temple"
        eng._game_state.battlefield_1_controller = None
        eng._game_state.player_1_units = conqueror_units
        tax = {"Sunken Temple": _ability("WHEN_CONQUER_HERE", ("IF_1+_UNIT_MIGHTY",), "DRAW_1")}
        return _fire(
            eng,
            tax,
            GameEvent(kind="ON_CONQUER", controller="player_1", battlefield="battlefield_1"),
        )

    def test_fires_with_a_mighty_conqueror(self) -> None:
        # bonus_might 5 → effective might ≥ 5 → [Mighty].
        q = self._run([PlayedUnit(card="Bruiser", location="battlefield_1", bonus_might=5)])
        self.assertEqual([te.trigger for te in q], ["WHEN_CONQUER_HERE"])

    def test_blocked_without_a_mighty_unit(self) -> None:
        q = self._run([PlayedUnit(card="Bruiser", location="battlefield_1", bonus_might=1)])
        self.assertEqual(q, [])

    def test_blocked_when_mighty_unit_is_elsewhere(self) -> None:
        q = self._run([PlayedUnit(card="Bruiser", location="battlefield_2", bonus_might=5)])
        self.assertEqual(q, [])


class WhileAtBattlefieldTests(unittest.TestCase):
    def _run(self, vex_location: str):
        eng = _engine()
        eng._game_state.player_1_units = [
            PlayedUnit(card="Vex, Apathetic", location=vex_location, exhausted=False)
        ]
        # Effect codes are Group-2 (stun) and unimplemented, but the trigger +
        # condition still queue the ability; we assert on the gate only.
        tax = {"Vex, Apathetic": _ability("WHEN_OPPONENT_PLAYS_UNIT", ("WHILE_AT_BATTLEFIELD",), "STUN_IT")}
        return _fire(
            eng,
            tax,
            GameEvent(kind="ON_PLAY_UNIT", controller="player_2", source="player_2:0"),
        )

    def test_fires_while_at_a_battlefield(self) -> None:
        q = self._run("battlefield_1")
        self.assertEqual([te.trigger for te in q], ["WHEN_OPPONENT_PLAYS_UNIT"])

    def test_blocked_while_in_base(self) -> None:
        q = self._run("base")
        self.assertEqual(q, [])


class UnknownConditionFailsClosedTests(unittest.TestCase):
    def test_unknown_condition_does_not_fire(self) -> None:
        eng = _engine()
        eng._game_state.player_1_units = [PlayedUnit(card="Mystery", location="battlefield_1")]
        tax = {"Mystery": _ability("WHEN_I_CONQUER", ("NO_SUCH_CONDITION",), "DRAW_1")}
        q = _fire(
            eng, tax,
            GameEvent(kind="ON_CONQUER", controller="player_1", battlefield="battlefield_1"),
        )
        self.assertEqual(q, [])


if __name__ == "__main__":
    unittest.main()
