"""Continuous grant AURAS: a permanent conferring a property (Deflect, Might, …)
to a SET of other units, via the declarative ``abilities.AURA_GRANTS`` table +
``engine.aura_bonus``. Real cards exercised:
  - Allay, Eager Admirer (unl-041): "while I'm at a battlefield, your other units
    here have [Deflect]" → OTHER_AT_THS_BF_DEFLECT.
  - Hexdrinker (sfd-102): an [Equip] granting the equipped unit Deflect (bare
    DEFLECT passive on the gear).
  - Trifarian War Camp (ogn-294, battlefield): "units here have +1 Might."
"""

from __future__ import annotations

import unittest
from unittest import mock

from riftbound_engine import abilities as A
from riftbound_engine.abilities import Ability
from riftbound_engine.engine import GameEngine, GameState, PlayedGear, PlayedUnit, RequiredTo

ALLAY = "Allay, Eager Admirer"
HEXDRINKER = "Hexdrinker"
WAR_CAMP = "Trifarian War Camp"
PLAIN = "Scout"  # a unit with no Deflect / aura of its own

# The suite runs hermetically (conftest disables the real taxonomy), so the
# aura/equip PASSIVES these cards carry must be supplied explicitly — printed
# Deflect/Might still come from the real CSV. Mirrors test_defend_here etc.
_ABILITIES = {
    # Allay: "while I'm at a battlefield, your other units here have [Deflect]."
    ALLAY: (Ability(passive_effects=("OTHER_AT_THS_BF_DEFLECT",)),),
    # Hexdrinker: an [Equip] (effect-text) granting the host a bare [Deflect].
    HEXDRINKER: (Ability(effect_text=True, passive_effects=("DEFLECT",)),),
    # Trifarian War Camp (battlefield): "units here have +1 Might."
    WAR_CAMP: (Ability(passive_effects=("UNITS_HERE_HAVE_+1M",)),),
}


class _AuraMockMixin(unittest.TestCase):
    """Supply the aura/equip passives for the real card names used here."""

    def setUp(self) -> None:
        p = mock.patch.object(
            A, "triggered_abilities_for", side_effect=lambda n: _ABILITIES.get(n, ())
        )
        p.start()
        self.addCleanup(p.stop)


def _eng(p1_units=(), p2_units=(), p1_gears=(), bf1=None, bf2=None) -> GameEngine:
    gs = GameState()
    gs.player_1_units = list(p1_units)
    gs.player_2_units = list(p2_units)
    gs.player_1_gears = list(p1_gears)
    gs.battlefield_1 = bf1
    gs.battlefield_2 = bf2
    return GameEngine(game_state=gs)


class DeflectAuraTests(_AuraMockMixin):
    def test_allay_grants_friendly_unit_at_same_bf(self) -> None:
        eng = _eng(p1_units=[
            PlayedUnit(card=ALLAY, location="battlefield_1", uid=1),
            PlayedUnit(card=PLAIN, location="battlefield_1", uid=2),
        ])
        # The plain friendly unit at the same battlefield gets Deflect from Allay.
        self.assertEqual(eng.unit_deflect_count("player_1", 1), 1)
        # Allay itself: its OWN printed [Deflect] (=1), NOT the aura (excludes self).
        self.assertEqual(eng.unit_deflect_count("player_1", 0), 1)

    def test_aura_excludes_other_location_and_enemies(self) -> None:
        eng = _eng(
            p1_units=[
                PlayedUnit(card=ALLAY, location="battlefield_1", uid=1),
                PlayedUnit(card=PLAIN, location="battlefield_2", uid=2),  # elsewhere
            ],
            p2_units=[PlayedUnit(card=PLAIN, location="battlefield_1", uid=3)],  # enemy here
        )
        self.assertEqual(eng.unit_deflect_count("player_1", 1), 0)  # different BF
        self.assertEqual(eng.unit_deflect_count("player_2", 0), 0)  # enemy, not "your"

    def test_aura_requires_source_at_battlefield(self) -> None:
        eng = _eng(p1_units=[
            PlayedUnit(card=ALLAY, location="base", uid=1),  # Allay at base, not a BF
            PlayedUnit(card=PLAIN, location="base", uid=2),
        ])
        self.assertEqual(eng.unit_deflect_count("player_1", 1), 0)

    def test_auras_stack(self) -> None:
        eng = _eng(p1_units=[
            PlayedUnit(card=ALLAY, location="battlefield_1", uid=1),
            PlayedUnit(card=ALLAY, location="battlefield_1", uid=2),
            PlayedUnit(card=PLAIN, location="battlefield_1", uid=3),
        ])
        # Two Allays → the plain unit gets Deflect 2; each Allay gets 1 (from the
        # OTHER Allay's aura) on top of its own printed 1 = 2.
        self.assertEqual(eng.unit_deflect_count("player_1", 2), 2)
        self.assertEqual(eng.unit_deflect_count("player_1", 0), 2)

    def test_hexdrinker_equip_grants_deflect(self) -> None:
        eng = _eng(
            p1_units=[PlayedUnit(card=PLAIN, location="base", uid=5)],
            p1_gears=[PlayedGear(card=HEXDRINKER, attached_uid=5)],
        )
        self.assertEqual(eng.unit_deflect_count("player_1", 0), 1)


class AuraFeedsDeflectTaxTests(_AuraMockMixin):
    def test_enemy_targeting_allay_buffed_unit_is_taxed(self) -> None:
        # P1 has Allay + a plain unit at battlefield_1 (so the plain unit has
        # Deflect 1 from the aura). P2 targets it with a spell → P2 must pay.
        gs = GameState()
        gs.player_1_units = [
            PlayedUnit(card=ALLAY, location="battlefield_1", uid=1),
            PlayedUnit(card=PLAIN, location="battlefield_1", uid=2),
        ]
        gs.player_2_power = {"Fury": 2}
        gs.battlefield_1 = "A"
        gs.battlefield_2 = "B"
        eng = GameEngine(game_state=gs)
        eng._push_spell_to_chain(RequiredTo.PLAYER_2, "Gust", [["player_1:1"]])
        pd = eng._game_state.pending_deflect
        self.assertIsNotNone(pd)  # the aura-granted Deflect triggered the tax
        self.assertEqual(pd.queue[0][1], 1)
        eng.resolve_deflect_decision(RequiredTo.PLAYER_2, pay=True)
        self.assertEqual(eng._total_power(RequiredTo.PLAYER_2), 1)  # paid 1


class MightAuraTests(_AuraMockMixin):
    def test_war_camp_battlefield_grants_might_here(self) -> None:
        eng = _eng(
            p1_units=[
                PlayedUnit(card=PLAIN, location="battlefield_1", uid=1),
                PlayedUnit(card=PLAIN, location="base", uid=2),
            ],
            bf1=WAR_CAMP,
        )
        base = eng.effective_unit_might("player_1", 1)  # at base, no aura
        here = eng.effective_unit_might("player_1", 0)  # at battlefield_1
        self.assertEqual(here - base, 1)  # +1 Might from the battlefield aura


if __name__ == "__main__":
    unittest.main()
