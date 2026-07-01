"""Not So Fast (COUNTER_SPELL_OR_ABILITY): counter an ENEMY spell OR triggered
ability on the chain that chooses a friendly unit or gear.

Targeting (requirements layer): only enemy items whose targets include a unit/
gear the caster controls are legal. Effect (effects layer): the chosen item is
removed before it resolves — a spell to its caster's trash, an ability dropped.
"""

import unittest
from unittest import mock

from riftbound_engine import abilities as _abilities
from riftbound_engine.abilities import Ability
from riftbound_engine.engine import (
    ChainItem,
    GameEngine,
    GameState,
    PendingChain,
    RequiredTo,
    TriggeredEffect,
)
from riftbound_engine.requirements import (
    ChainItemInfo,
    matching_spell_refs,
    spell_picks,
)

NSF = (Ability(active_effects=("COUNTER_SPELL_OR_ABILITY",)),)
NSF_REQ = "ENEMY SPELL[CHOOSE_FRIENDLY]||ENEMY ABILITY[CHOOSE_FRIENDLY]"


def _fake_abilities(name: str):
    return NSF if name == "Not So Fast" else ()


class NotSoFastTargetingTests(unittest.TestCase):
    """The merged requirement offers exactly the enemy spells/abilities that
    choose one of the caster's units/gear."""

    def _req(self):
        picks = spell_picks(NSF_REQ)
        self.assertEqual(len(picks), 1)  # one pick, spell OR ability
        return picks[0]

    def test_enemy_spell_choosing_friendly_is_legal(self):
        req = self._req()
        items = [ChainItemInfo("player_2", "Gust", False, frozenset({"player_1"}))]
        self.assertEqual(matching_spell_refs(req, items, "player_1"), [0])

    def test_enemy_ability_choosing_friendly_is_legal(self):
        req = self._req()
        items = [ChainItemInfo("player_2", None, True, frozenset({"player_1"}))]
        self.assertEqual(matching_spell_refs(req, items, "player_1"), [0])

    def test_enemy_item_not_choosing_friendly_is_rejected(self):
        req = self._req()
        # Chooses the caster's OPPONENT's unit → not "a friendly unit".
        items = [ChainItemInfo("player_2", "Gust", False, frozenset({"player_2"}))]
        self.assertEqual(matching_spell_refs(req, items, "player_1"), [])

    def test_own_spell_is_rejected(self):
        req = self._req()  # side=enemy
        items = [ChainItemInfo("player_1", "Gust", False, frozenset({"player_1"}))]
        self.assertEqual(matching_spell_refs(req, items, "player_1"), [])


class NotSoFastEffectTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch("riftbound_engine.abilities.triggered_abilities_for", _fake_abilities)
        p.start()
        self.addCleanup(p.stop)

    def test_counters_enemy_spell_to_trash(self):
        eng = GameEngine(game_state=GameState())
        eng._push_spell_to_chain(RequiredTo.PLAYER_2, "Gust", [["player_1:0"]])
        cid = eng._game_state.pending_chain.items[0].cid
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Not So Fast", [[f"spell_cid:{cid}"]])
        eng._resolve_chain()  # Not So Fast (top) resolves → counters Gust
        gs = eng._game_state
        items = gs.pending_chain.items if gs.pending_chain else []
        self.assertTrue(all(it.card != "Gust" for it in items))
        self.assertIn("Gust", gs.player_2_trash)
        self.assertIn("Not So Fast", gs.player_1_trash)

    def test_counters_enemy_ability_item(self):
        eng = GameEngine(game_state=GameState())
        # An enemy triggered-ability item on the chain (card=None).
        te = TriggeredEffect(
            controller="player_2", source="player_2:0", trigger="X",
            event_kind="", effects=("DRAW_1",), label="Foe — X",
        )
        ability_item = ChainItem(
            actor=RequiredTo.PLAYER_2, card=None, targets=["player_1:0"],
            effect=te, label="Foe — X", cid=eng._take_chain_cid(),
        )
        eng._game_state.pending_chain = PendingChain(
            items=[ability_item], priority=RequiredTo.PLAYER_2, consecutive_passes=0
        )
        cid = ability_item.cid
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Not So Fast", [[f"spell_cid:{cid}"]])
        eng._resolve_chain()  # counters the ability before it draws
        gs = eng._game_state
        items = gs.pending_chain.items if gs.pending_chain else []
        self.assertTrue(all(it.effect is None for it in items))  # ability gone
        self.assertIn("Not So Fast", gs.player_1_trash)


if __name__ == "__main__":
    unittest.main()
