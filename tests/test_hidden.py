"""[Hidden] keyword — hide a card from hand face-down at a battlefield you
control (pay 1 Power), then reveal-and-play it for FREE on a LATER turn. A
hidden card is bound to its battlefield: lose control and you lose the card.

Fixtures:
  * Blastcone Fae — Unit, 2 Energy + 1 Mind Power, [Hidden] (not a Reaction).
  * Consult the Past — Spell, [Hidden] + [Reaction].
"""

import unittest

from riftbound_engine.action_turn.builtins import _hide, _play_hidden
from riftbound_engine.action_turn.context import ActionTurnContext
from riftbound_engine.engine import GameEngine, GameState, HiddenCard, RequiredTo

UNIT = "Blastcone Fae"  # Unit, 2 Energy + 1 Mind, [Hidden]
REACTION = "Consult the Past"  # Spell, [Hidden] + [Reaction]
ACTION_SPELL = "Block"  # Spell, [Hidden] + [Action] (NOT a Reaction)
P1 = RequiredTo.PLAYER_1


def _state(**over) -> GameState:
    base = dict(
        started=True,
        abcd_a_done=True,
        abcd_b_done=True,
        abcd_c_done=True,
        abcd_d_done=True,
        current_player=P1,
        total_turn_number=1,
        player_1_hand=[UNIT],
        player_1_energy=0,
        player_1_power={"Mind": 1},
        battlefield_1="Some Battlefield",
        battlefield_1_controller=P1,
    )
    base.update(over)
    return GameState(**base)


def _ctx(eng, verb, payload):
    return ActionTurnContext(engine=eng, actor=P1, verb=verb, payload=payload)


class HideTests(unittest.TestCase):
    def test_hide_option_offered_only_for_hidden_card_with_control_and_power(self):
        eng = GameEngine(game_state=_state())
        self.assertIn("play:hide:0:battlefield_1", eng._hide_options(P1))
        # No Power → no hide option.
        eng2 = GameEngine(game_state=_state(player_1_power={}))
        self.assertEqual(eng2._hide_options(P1), [])
        # Don't control any battlefield → no hide option.
        eng3 = GameEngine(game_state=_state(battlefield_1_controller=None))
        self.assertEqual(eng3._hide_options(P1), [])
        # A non-Hidden card is never offered.
        eng4 = GameEngine(game_state=_state(player_1_hand=["Blue Sentinel"]))
        self.assertEqual(eng4._hide_options(P1), [])

    def test_hide_moves_card_and_pays_one_power(self):
        eng = GameEngine(game_state=_state())
        _hide(_ctx(eng, "hide", "0:battlefield_1"))
        gs = eng._game_state
        self.assertEqual(gs.player_1_hand, [])
        self.assertEqual(len(gs.player_1_hidden), 1)
        entry = gs.player_1_hidden[0]
        self.assertEqual(entry.card, UNIT)
        self.assertEqual(entry.battlefield, "battlefield_1")
        self.assertEqual(entry.hidden_on_turn, 1)
        self.assertEqual(eng.total_power(P1), 0)  # 1 Power spent

    def test_cannot_hide_at_battlefield_you_dont_control(self):
        eng = GameEngine(game_state=_state(battlefield_1_controller=None))
        with self.assertRaises(ValueError):
            _hide(_ctx(eng, "hide", "0:battlefield_1"))


class RevealTests(unittest.TestCase):
    def _hidden_state(self, **over):
        """State with UNIT already hidden at battlefield_1 on turn 1."""
        base = dict(
            player_1_hand=[],
            player_1_power={},
            player_1_hidden=[HiddenCard(card=UNIT, battlefield="battlefield_1", hidden_on_turn=1)],
        )
        base.update(over)
        return _state(**base)

    def test_cannot_reveal_same_turn(self):
        eng = GameEngine(game_state=self._hidden_state(total_turn_number=1))
        self.assertEqual(eng._hidden_reveal_options(P1), [])

    def test_can_reveal_next_turn_for_free(self):
        eng = GameEngine(game_state=self._hidden_state(total_turn_number=2))
        self.assertIn("play:play_hidden:0", eng._hidden_reveal_options(P1))
        _play_hidden(_ctx(eng, "play_hidden", "0"))
        gs = eng._game_state
        # Unit entered play EXHAUSTED, straight at the battlefield it was hidden
        # at (no location pick), the hidden slot is consumed, and it was FREE:
        # energy still 0 and no Power was spent (we started with none).
        self.assertEqual([u.card for u in gs.player_1_units], [UNIT])
        self.assertEqual(gs.player_1_units[0].location, "battlefield_1")
        self.assertTrue(gs.player_1_units[0].exhausted)
        self.assertEqual(gs.player_1_hidden, [])
        self.assertEqual(eng.player_energy(P1), 0)
        self.assertEqual(eng.total_power(P1), 0)

    def test_losing_battlefield_control_discards_hidden_card(self):
        eng = GameEngine(game_state=self._hidden_state(total_turn_number=2))
        # Opponent takes the battlefield the card was hidden at.
        eng._game_state.battlefield_1_controller = RequiredTo.PLAYER_2
        eng._sweep_lost_hidden()
        gs = eng._game_state
        self.assertEqual(gs.player_1_hidden, [])
        self.assertIn(UNIT, gs.player_1_trash)

    def test_reaction_window_offers_units_gears_and_reaction_spells_only(self):
        # In a reaction window: Units qualify (enter play directly) and Reaction
        # spells qualify (ride the chain), but a non-Reaction spell does NOT.
        eng = GameEngine(
            game_state=self._hidden_state(
                total_turn_number=2,
                player_1_hidden=[
                    HiddenCard(card=UNIT, battlefield="battlefield_1", hidden_on_turn=1),
                    HiddenCard(card=REACTION, battlefield="battlefield_1", hidden_on_turn=1),
                    HiddenCard(card=ACTION_SPELL, battlefield="battlefield_1", hidden_on_turn=1),
                ],
            )
        )
        opts = eng._hidden_reveal_options(P1, reaction_only=True)
        # idx 0 (Unit) and idx 1 (Reaction spell), but NOT idx 2 (Action spell).
        self.assertEqual(opts, ["play:play_hidden:0", "play:play_hidden:1"])


if __name__ == "__main__":
    unittest.main()
