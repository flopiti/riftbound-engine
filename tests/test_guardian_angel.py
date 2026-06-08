"""Guardian Angel is a would-die REPLACEMENT effect.

The Guardian Angel gear carries the equipment trigger ``IF_ID_DIE`` whose
effect is ``HEAL_EXHAUST_RECALL``. ``IF_ID_DIE`` is a replacement marker, NOT a
chain trigger: when the equipped HOST unit would die, the death is replaced —
the unit is healed (a no-op here, no persistent damage), exhausted, and
recalled to its owner's hand, instead of dying. No ON_DEATH fires.

The suite is hermetic (no taxonomy loads), so we monkeypatch
``abilities.triggered_abilities_for`` to give the Guardian Angel gear its real
abilities (the +2 Might passive and the IF_ID_DIE replacement), exactly as the
conftest docstring prescribes for trigger tests."""

import unittest
from unittest import mock

from riftbound_engine.abilities import Ability
from riftbound_engine.csv_data import card_might_of, csv_cards
from riftbound_engine.engine import (
    GameEngine,
    PendingCombat,
    PlayedGear,
    PlayedUnit,
    RequiredTo,
)

_GUARDIAN_ANGEL = (
    Ability(passive_effects=("UNIT_ATTACHED_+2M",), effect_text=True),
    Ability(triggers=("IF_ID_DIE",), active_effects=("HEAL_EXHAUST_RECALL",), effect_text=True),
)


def _fake_abilities(name: str):
    return _GUARDIAN_ANGEL if (name or "").strip().lower() == "guardian angel" else ()


def _might_3_unit() -> str:
    for c in csv_cards():
        if card_might_of(c.name) == 3:
            return c.name
    raise unittest.SkipTest("no Might-3 unit in the CSV")


class GuardianAngelReplacementTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch(
            "riftbound_engine.abilities.triggered_abilities_for", _fake_abilities
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _combat(self, *, with_gear: bool) -> GameEngine:
        host = _might_3_unit()
        eng = GameEngine()
        gs = eng._game_state
        gs.player_1_units = [
            PlayedUnit(card=host, location="battlefield_1", exhausted=False, bonus_might=0, uid=10)
        ]
        gs.player_2_units = [
            PlayedUnit(card=host, location="battlefield_1", exhausted=False, bonus_might=0, uid=20)
        ]
        gs.player_1_gears = (
            [PlayedGear(card="Guardian Angel", attached_uid=10, attached_to="player_1:0", attached_on_turn=1)]
            if with_gear
            else []
        )
        gs.player_2_gears = []
        gs.player_1_hand, gs.player_2_hand = [], []
        gs.player_1_trash, gs.player_2_trash = [], []
        # Give P2 enough budget to assign lethal to P1's host (buffed +2 by the
        # gear when present), so the host WOULD die either way.
        p1m = eng.might_at_battlefield(RequiredTo.PLAYER_1, "battlefield_1")
        p2m = eng.might_at_battlefield(RequiredTo.PLAYER_2, "battlefield_1")
        gs.pending_combat = PendingCombat(
            battlefield="battlefield_1",
            player_1_might=p1m,
            player_2_might=max(p2m, p1m),
        )
        gs.pending_combat.player_2_targets = [0]  # P2 assigns lethal to P1's host
        gs.pending_combat.player_1_targets = []
        return eng

    def test_without_gear_host_dies_to_trash(self):
        eng = self._combat(with_gear=False)
        host = eng._game_state.player_1_units[0].card
        eng.resolve_combat("battlefield_1")
        gs = eng._game_state
        self.assertEqual(gs.player_1_units, [])
        self.assertEqual(gs.player_1_hand, [])
        self.assertEqual(gs.player_1_trash, [host])

    def test_with_guardian_angel_host_recalled_to_hand(self):
        eng = self._combat(with_gear=True)
        host = eng._game_state.player_1_units[0].card
        # Sanity: the gear's +2 passive applies, so the host is at 5 Might.
        self.assertEqual(eng.effective_unit_might("player_1", 0), 5)
        eng.resolve_combat("battlefield_1")
        gs = eng._game_state
        # Saved: left the battlefield, returned to hand, NOT trashed.
        self.assertEqual(gs.player_1_units, [])
        self.assertEqual(gs.player_1_hand, [host])
        self.assertEqual(gs.player_1_trash, [])

    def test_if_id_die_is_not_a_chain_trigger(self):
        # The replacement marker must never be registered as an event trigger,
        # or it would push a phantom item onto the chain when a unit dies.
        from riftbound_engine.triggers import TRIGGER_EVENT_MAP

        self.assertNotIn("IF_ID_DIE", TRIGGER_EVENT_MAP)


if __name__ == "__main__":
    unittest.main()
