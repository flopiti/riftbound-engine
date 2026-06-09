"""COUNTER_SPELL (Defy): counter a spell on the chain.

Defy is a [Reaction] resolving on TOP of the chain, so its target sits below it.
COUNTER_SPELL re-locates the target by its STABLE chain cid (captured as a
``spell_cid:<n>`` token, since positional indices shift as the chain resolves),
removes it before it can resolve, and sends it to its caster's trash with NO
effect. If the target already left the chain, it's a clean no-op."""

import unittest
from unittest import mock

from riftbound_engine import abilities as _abilities
from riftbound_engine.abilities import Ability
from riftbound_engine.engine import GameEngine, GameState, RequiredTo
from riftbound_engine.requirements import SpellRequirement, matching_spell_refs

DEFY = (Ability(active_effects=("COUNTER_SPELL",)),)


def _fake_abilities(name: str):
    return DEFY if name == "Defy" else ()


class CounterSpellTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch("riftbound_engine.abilities.triggered_abilities_for", _fake_abilities)
        p.start()
        self.addCleanup(p.stop)

    def _engine_with_target(self, target="Gust"):
        """A chain with P2's `target` spell on the bottom and P1's Defy on top,
        Defy aimed at the target by its stable cid."""
        eng = GameEngine(game_state=GameState())
        eng._push_spell_to_chain(RequiredTo.PLAYER_2, target, [[]])
        target_cid = eng._game_state.pending_chain.items[0].cid
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Defy", [[f"spell_cid:{target_cid}"]])
        return eng, target_cid

    def test_counters_and_trashes_the_target(self):
        eng, _ = self._engine_with_target("Gust")
        self.assertEqual(len(eng._game_state.pending_chain.items), 2)
        eng._resolve_chain()  # resolves Defy (top) → runs COUNTER_SPELL
        gs = eng._game_state
        items = gs.pending_chain.items if gs.pending_chain else []
        # Target is gone from the chain and never resolved — it's in P2's trash.
        self.assertTrue(all(it.card != "Gust" for it in items))
        self.assertIn("Gust", gs.player_2_trash)
        # Defy itself resolved normally (its own card → P1 trash).
        self.assertIn("Defy", gs.player_1_trash)

    def test_target_does_not_resolve_its_effect(self):
        # Gust returns a unit to hand. Put a P1 unit on a battlefield that Gust
        # *would* bounce, and confirm countering Gust leaves it on the board.
        from riftbound_engine.engine import PlayedUnit

        eng, target_cid = self._engine_with_target("Gust")
        eng._game_state.player_1_units = [
            PlayedUnit(card="Plundering Poro", location="battlefield_1", uid=1)
        ]
        # Re-aim Gust at that unit so, if it resolved, the unit would leave play.
        gust = next(it for it in eng._game_state.pending_chain.items if it.card == "Gust")
        gust.targets = ["player_1:0"]
        gust.target_uids = [1]
        eng._resolve_chain()  # Defy counters Gust before it resolves
        units = eng._game_state.player_1_units
        self.assertEqual([u.card for u in units], ["Plundering Poro"])  # still on board

    def test_no_op_when_target_already_gone(self):
        eng = GameEngine(game_state=GameState())
        # Defy alone, aimed at a cid that isn't on the chain.
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Defy", [["spell_cid:9999"]])
        eng._resolve_chain()  # must not raise; nothing to counter
        gs = eng._game_state
        self.assertIsNone(gs.pending_chain)  # chain emptied cleanly
        self.assertIn("Defy", gs.player_1_trash)

    def test_requirement_cost_cap_filters_targets(self):
        # The "ANY SPELL (<=4E AND <=1P)" filter: a cheap spell matches, an
        # expensive one doesn't. (Targeting layer that gates what Defy may pick.)
        from riftbound_engine.csv_data import card_energy_of, csv_cards

        cheap = next(
            c.name for c in csv_cards()
            if c.card_type == "Spell" and (card_energy_of(c.name) or 0) <= 4
        )
        expensive = next(
            (c.name for c in csv_cards()
             if c.card_type == "Spell" and (card_energy_of(c.name) or 0) > 4),
            None,
        )
        req = SpellRequirement(energy_max=4, power_max=1)
        items = [("player_2", cheap)]
        if expensive:
            items.append(("player_2", expensive))
        matched = set(matching_spell_refs(req, items, "player_1"))
        self.assertIn(0, matched)  # cheap spell is a legal counter target
        if expensive:
            self.assertNotIn(1, matched)  # too expensive → not offered


if __name__ == "__main__":
    unittest.main()
