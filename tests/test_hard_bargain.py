"""Hard Bargain (COUNTER_SPELL_UNLESS_2E): counter a spell UNLESS its controller
pays 2 energy. The decision belongs to the TARGET spell's controller and is
queued (one per [Repeat] round), each opening a pay-or-be-countered choice.

Driven through the low-level chain harness (like test_counter_spell); the pending
opponent decision is answered via a helper that mirrors the choose_effect_target
handler's apply path (which itself resumes the queue for the next round).
"""

import unittest
from unittest import mock

from riftbound_engine.abilities import Ability
from riftbound_engine.effects import EffectContext, apply_choice_effect
from riftbound_engine.engine import GameEngine, GameState, RequiredTo

HB = (Ability(active_effects=("COUNTER_SPELL_UNLESS_2E",)),)


def _fake_abilities(name: str):
    return HB if name == "Hard Bargain" else ()


def _answer(eng: GameEngine, token: str) -> None:
    """Resolve the pending opponent decision with ``token`` (pay/decline),
    mirroring builtins._choose_effect_target's apply path."""
    ch = eng._game_state.pending_effect_choice
    assert ch is not None, "expected a pending pay-or-be-countered choice"
    assert token in ch.options, f"{token!r} not in {ch.options}"
    eng._game_state.pending_effect_choice = None
    ctx = EffectContext(
        engine=eng, controller=ch.actor, source=ch.source, code=ch.code,
        trigger=ch.trigger, event_kind=ch.event_kind,
        targets=tuple(ch.continuation_targets),
    )
    apply_choice_effect(ctx, token)


class HardBargainTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch("riftbound_engine.abilities.triggered_abilities_for", _fake_abilities)
        p.start()
        self.addCleanup(p.stop)

    def _chain(self, *, p2_energy: int, targets: list[str]):
        """P2's target spell(s) on the bottom, P1's Hard Bargain on top aiming at
        them (one round per target — the 2nd+ are [Repeat] rounds)."""
        eng = GameEngine(game_state=GameState())
        eng._game_state.player_2_energy = p2_energy
        cids = []
        for t in targets:
            eng._push_spell_to_chain(RequiredTo.PLAYER_2, t, [[]])
            cids.append(eng._game_state.pending_chain.items[0].cid)
        rounds = [[f"spell_cid:{c}"] for c in cids]
        eng._push_spell_to_chain(RequiredTo.PLAYER_1, "Hard Bargain", rounds)
        return eng, cids

    def _chain_cards(self, eng):
        gs = eng._game_state
        return [it.card for it in gs.pending_chain.items] if gs.pending_chain else []

    def test_controller_pays_to_save_the_spell(self):
        eng, _ = self._chain(p2_energy=5, targets=["Gust"])
        eng._resolve_chain()  # Hard Bargain resolves → opens P2's pay decision
        self.assertEqual(eng._game_state.pending_effect_choice.actor, RequiredTo.PLAYER_2)
        _answer(eng, "pay")
        self.assertIn("Gust", self._chain_cards(eng))       # survived on the chain
        self.assertNotIn("Gust", eng._game_state.player_2_trash)
        self.assertEqual(eng._game_state.player_2_energy, 3)  # paid 2

    def test_controller_declines_and_is_countered(self):
        eng, _ = self._chain(p2_energy=5, targets=["Gust"])
        eng._resolve_chain()
        _answer(eng, "decline")
        self.assertNotIn("Gust", self._chain_cards(eng))
        self.assertIn("Gust", eng._game_state.player_2_trash)
        self.assertEqual(eng._game_state.player_2_energy, 5)  # nothing paid

    def test_cannot_afford_auto_counters_without_asking(self):
        eng, _ = self._chain(p2_energy=1, targets=["Gust"])
        eng._resolve_chain()
        self.assertIsNone(eng._game_state.pending_effect_choice)  # no decision offered
        self.assertIn("Gust", eng._game_state.player_2_trash)

    def test_repeat_resolves_each_target_sequentially(self):
        # [Repeat]: because Hard Bargain has an implemented effect, its base round
        # and each repeat round land as SEPARATE chain items (each with its own
        # reaction window + pay decision). Pay for the first target, decline the
        # second.
        eng, cids = self._chain(p2_energy=5, targets=["Gust", "En Garde"])
        gust_cid, engarde_cid = cids

        eng._resolve_chain()  # resolve HB base item → decision for the base target
        ch1 = eng._game_state.pending_effect_choice
        self.assertEqual(ch1.actor, RequiredTo.PLAYER_2)
        self.assertEqual(ch1.continuation_targets, (f"spell_cid:{gust_cid}",))
        _answer(eng, "pay")   # save Gust (−2 energy)
        self.assertIsNone(eng._game_state.pending_effect_choice)  # nothing queued yet

        eng._resolve_chain()  # resolve HB [Repeat] item → decision for En Garde
        ch2 = eng._game_state.pending_effect_choice
        self.assertEqual(ch2.continuation_targets, (f"spell_cid:{engarde_cid}",))
        _answer(eng, "decline")  # counter En Garde

        cards = self._chain_cards(eng)
        self.assertIn("Gust", cards)
        self.assertNotIn("En Garde", cards)
        self.assertIn("En Garde", eng._game_state.player_2_trash)
        self.assertNotIn("Gust", eng._game_state.player_2_trash)
        self.assertEqual(eng._game_state.player_2_energy, 3)  # paid once
        self.assertEqual(eng._game_state.pending_counters, [])  # queue drained


if __name__ == "__main__":
    unittest.main()
