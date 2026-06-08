"""Targets chosen at cast time are RE-LOCATED by stable uid and RE-VALIDATED
against the requirement when the spell resolves — so a reaction that changes
the board between cast and resolution (e.g. buffing a unit past Gust's "3
Might or less", or an earlier unit leaving play and shifting indices) is
handled correctly: the spell fizzles when its target no longer qualifies, and
always hits the unit that was actually chosen.
"""

import unittest
from unittest import mock

from riftbound_engine import abilities as _abilities
from riftbound_engine.engine import GameEngine, GameState, PlayedUnit, RequiredTo
from riftbound_engine.requirements import UnitView, targets_still_satisfy

GUST_REQ = "ANY UNIT (1[<=3M BF])"

# The pytest suite is hermetic (conftest points the taxonomy loader at a
# nonexistent file), so Gust's tagged RETURN_TO_HAND effect won't load on its
# own. Stub it the same way the trigger tests do so the effect actually runs.
_GUST_ABILITY = _abilities.Ability(
    activation_speeds=("REACTION",), active_effects=("RETURN_TO_HAND",)
)


def _view(controller: str, index: int, *, might: int, location: str = "battlefield_1") -> UnitView:
    return UnitView(
        controller=controller,
        index=index,
        card="Ravenbloom Student",
        location=location,
        exhausted=False,
        might=might,
        attacking=False,
    )


class TestTargetsStillSatisfy(unittest.TestCase):
    def test_unit_at_or_below_cap_passes(self) -> None:
        self.assertTrue(targets_still_satisfy(GUST_REQ, [_view("player_2", 0, might=3)], "player_1"))

    def test_unit_over_cap_fizzles(self) -> None:
        self.assertFalse(targets_still_satisfy(GUST_REQ, [_view("player_2", 0, might=4)], "player_1"))

    def test_no_surviving_target_fizzles(self) -> None:
        self.assertFalse(targets_still_satisfy(GUST_REQ, [], "player_1"))

    def test_unit_off_battlefield_fizzles(self) -> None:
        # Requirement demands a unit AT A BATTLEFIELD; one at base no longer qualifies.
        self.assertFalse(
            targets_still_satisfy(GUST_REQ, [_view("player_2", 0, might=2, location="base")], "player_1")
        )

    def test_no_enforceable_pick_is_lenient(self) -> None:
        # Blank / unknowable requirement → nothing to adjudicate → never fizzle.
        self.assertTrue(targets_still_satisfy("", [], "player_1"))


def _engine_with_units(units: list[PlayedUnit]) -> GameEngine:
    eng = GameEngine(game_state=GameState(player_2_units=list(units), player_2_hand=[]))
    eng._backfill_unit_uids()
    return eng


class TestUidRelocation(unittest.TestCase):
    def test_uid_survives_index_shift(self) -> None:
        eng = _engine_with_units(
            [
                PlayedUnit(card="Ravenbloom Student", location="battlefield_1"),
                PlayedUnit(card="Plundering Poro", location="battlefield_1"),
            ]
        )
        uid_b = eng._target_token_uid("player_2:1")  # the Plundering Poro
        self.assertIsNotNone(uid_b)
        # An earlier unit leaves play → indices shift down by one.
        eng._game_state.player_2_units.pop(0)
        self.assertEqual(eng._unit_by_uid(uid_b), ("player_2", 0))

    def test_uid_gone_when_unit_leaves(self) -> None:
        eng = _engine_with_units([PlayedUnit(card="Ravenbloom Student", location="battlefield_1")])
        uid = eng._target_token_uid("player_2:0")
        eng._game_state.player_2_units.clear()
        self.assertIsNone(eng._unit_by_uid(uid))


class TestGustResolution(unittest.TestCase):
    def setUp(self) -> None:
        patch = mock.patch.object(
            _abilities,
            "triggered_abilities_for",
            side_effect=lambda n: (_GUST_ABILITY,) if n == "Gust" else (),
        )
        patch.start()
        self.addCleanup(patch.stop)

    def _setup(self, units: list[PlayedUnit]) -> GameEngine:
        eng = _engine_with_units(units)
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Gust", [["player_2:0"]])
        return eng

    def _chain_item(self, eng: GameEngine):
        return eng._game_state.pending_chain.items[0]

    def test_applies_when_target_still_legal(self) -> None:
        eng = self._setup([PlayedUnit(card="Ravenbloom Student", location="battlefield_1")])
        eng._run_spell_effects(self._chain_item(eng))
        # Unit returned to its owner's hand.
        self.assertEqual(eng._game_state.player_2_units, [])
        self.assertEqual(eng._game_state.player_2_hand, ["Ravenbloom Student"])

    def test_fizzles_when_target_buffed_past_cap(self) -> None:
        eng = self._setup([PlayedUnit(card="Ravenbloom Student", location="battlefield_1")])
        # A reaction buffs the unit to 2 + 5 = 7 Might before Gust resolves.
        eng._game_state.player_2_units[0].bonus_might = 5
        eng._run_spell_effects(self._chain_item(eng))
        # Effect did NOT apply — the unit is still on the board, hand untouched.
        self.assertEqual(len(eng._game_state.player_2_units), 1)
        self.assertEqual(eng._game_state.player_2_hand, [])
        # A dedicated "fizzle" feed line explains WHY, with the concrete value.
        fizzles = [e for e in eng._game_state.event_feed if e.kind == "fizzle"]
        self.assertEqual(len(fizzles), 1)
        self.assertIn("7 Might", fizzles[0].text)
        self.assertIn("needs ≤3", fizzles[0].text)

    def test_relocates_to_chosen_unit_after_earlier_leaves(self) -> None:
        # Target the SECOND unit; then the first leaves play (index shift).
        eng = _engine_with_units(
            [
                PlayedUnit(card="Ravenbloom Student", location="battlefield_1"),
                PlayedUnit(card="Plundering Poro", location="battlefield_1"),
            ]
        )
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Gust", [["player_2:1"]])
        item = eng._game_state.pending_chain.items[0]
        eng._game_state.player_2_units.pop(0)  # Ravenbloom leaves; Poro shifts to index 0
        eng._run_spell_effects(item)
        # Gust returned the CHOSEN unit (Plundering Poro), not whatever now sits at index 1.
        self.assertEqual(eng._game_state.player_2_units, [])
        self.assertEqual(eng._game_state.player_2_hand, ["Plundering Poro"])

    def test_fizzles_when_target_left_play(self) -> None:
        eng = self._setup([PlayedUnit(card="Ravenbloom Student", location="battlefield_1")])
        item = self._chain_item(eng)
        eng._game_state.player_2_units.clear()  # target died/returned before Gust resolved
        eng._run_spell_effects(item)
        self.assertEqual(eng._game_state.player_2_hand, [])


if __name__ == "__main__":
    unittest.main()
