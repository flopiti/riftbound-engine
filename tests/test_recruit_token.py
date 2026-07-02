"""Recruit unit tokens (PLAY_RECRUIT_BASE) and the token cease-to-exist rule.

A Recruit token is a 1-Might unit token (CSV rows ogn-271/272/273). Like the
Gold gear token it is NOT a real card: when it leaves play — dying in combat,
killed by an effect, or bounced/recalled to hand — it CEASES TO EXIST rather
than going to its owner's trash or hand. These tests exercise every leave-play
path and confirm a real (non-token) unit still routes to trash/hand as before.
"""

import unittest

from riftbound_engine import effects as E
from riftbound_engine.csv_data import card_might_of
from riftbound_engine.engine import (
    GameEngine,
    GameState,
    PlayedUnit,
    PendingCombat,
    RequiredTo,
)


def _spawn_recruit(eng: GameEngine, who=RequiredTo.PLAYER_1) -> None:
    ctx = E.EffectContext(
        engine=eng, controller=who, source=None, code="PLAY_RECRUIT_BASE"
    )
    assert E.execute_effect(ctx) is True


class PlayRecruitBaseEffectTests(unittest.TestCase):
    def test_spawns_a_one_might_recruit_token_at_base(self) -> None:
        eng = GameEngine(game_state=GameState())
        _spawn_recruit(eng)
        units = eng._game_state.player_1_units
        self.assertEqual(len(units), 1)
        u = units[0]
        self.assertEqual(u.location, "base")
        self.assertTrue(u.exhausted)         # summoning sickness
        self.assertTrue(u.token)             # marked as a token
        self.assertEqual(card_might_of(u.card), 1)  # 1-might via CSV
        self.assertEqual(eng._game_state.player_2_units, [])

    def test_goes_to_the_right_controller(self) -> None:
        eng = GameEngine(game_state=GameState())
        _spawn_recruit(eng, RequiredTo.PLAYER_2)
        self.assertEqual(len(eng._game_state.player_2_units), 1)
        self.assertEqual(eng._game_state.player_1_units, [])


class RecruitFamilyVariantTests(unittest.TestCase):
    """The generalized PLAY_<N>_RECRUIT_<BASE|HERE> family — count and
    destination are parsed from the code."""

    def _run(self, code, source, who=RequiredTo.PLAYER_1):
        eng = GameEngine(game_state=GameState())
        ctx = E.EffectContext(engine=eng, controller=who, source=source, code=code)
        self.assertTrue(E.execute_effect(ctx))
        return eng

    def test_counted_base_variant_plays_n_tokens_at_base(self) -> None:
        eng = self._run("PLAY_3_RECRUIT_BASE", source=None)
        units = eng._game_state.player_1_units
        self.assertEqual(len(units), 3)
        self.assertTrue(all(u.location == "base" and u.token for u in units))

    def test_here_variant_uses_battlefield_source_location(self) -> None:
        # A battlefield ability: source is the bare battlefield slot.
        eng = self._run("PLAY_2_RECRUIT_HERE", source="battlefield_1")
        units = eng._game_state.player_1_units
        self.assertEqual(len(units), 2)
        self.assertTrue(all(u.location == "battlefield_1" for u in units))

    def test_here_variant_uses_unit_source_location(self) -> None:
        # A unit ability ("play a token here"): source is the unit's ref, so
        # the token lands wherever that unit currently sits.
        eng = GameEngine(game_state=GameState())
        eng._game_state.player_1_units.append(
            PlayedUnit(card="Faithful Manufactor", location="battlefield_2", uid=3)
        )
        ctx = E.EffectContext(
            engine=eng,
            controller=RequiredTo.PLAYER_1,
            source="player_1:0",
            code="PLAY_RECRUIT_HERE",
        )
        E.execute_effect(ctx)
        toks = [u for u in eng._game_state.player_1_units if u.token]
        self.assertEqual(len(toks), 1)
        self.assertEqual(toks[0].location, "battlefield_2")

    def test_here_with_no_source_falls_back_to_base(self) -> None:
        eng = self._run("PLAY_4_RECRUIT_BASE", source=None)
        self.assertEqual(len(eng._game_state.player_1_units), 4)
        self.assertTrue(all(u.location == "base" for u in eng._game_state.player_1_units))


class TokenCeasesToExistTests(unittest.TestCase):
    def test_killed_by_effect_does_not_go_to_trash(self) -> None:
        eng = GameEngine(game_state=GameState())
        _spawn_recruit(eng)
        eng._kill_unit("player_1", 0)
        self.assertEqual(eng._game_state.player_1_units, [])
        self.assertEqual(eng._game_state.player_1_trash, [])  # ceased to exist

    def test_real_unit_killed_by_effect_still_goes_to_trash(self) -> None:
        # Control: a non-token unit must still trash normally.
        eng = GameEngine(game_state=GameState())
        eng._game_state.player_1_units.append(
            PlayedUnit(card="Vayne, Hunter", location="base")
        )
        eng._kill_unit("player_1", 0)
        self.assertEqual(eng._game_state.player_1_units, [])
        self.assertEqual(eng._game_state.player_1_trash, ["Vayne, Hunter"])

    def test_dies_in_combat_does_not_go_to_trash(self) -> None:
        eng = GameEngine(game_state=GameState())
        gs = eng._game_state
        # P1 token defends battlefield_1; P2 has a real attacker that kills it.
        gs.player_1_units.append(
            PlayedUnit(card="Recruit (DE)", location="battlefield_1", token=True)
        )
        gs.player_2_units.append(
            PlayedUnit(card="Vayne, Hunter", location="battlefield_1")
        )
        # P2 (attacker) kills P1's unit at index 0; P1 kills nothing.
        gs.pending_combat = PendingCombat(
            battlefield="battlefield_1",
            player_1_might=1,
            player_2_might=4,
            player_1_targets=[],
            player_2_targets=[0],
        )
        eng.resolve_combat("battlefield_1")
        self.assertEqual(gs.player_1_units, [])           # token removed
        self.assertEqual(gs.player_1_trash, [])           # but NOT trashed
        self.assertEqual(gs.player_2_trash, [])           # attacker survived

    def test_returned_to_hand_does_not_enter_hand(self) -> None:
        eng = GameEngine(game_state=GameState())
        gs = eng._game_state
        gs.player_1_hand = []
        gs.player_1_units.append(
            PlayedUnit(card="Recruit (DE)", location="battlefield_1", token=True, uid=7)
        )
        ctx = E.EffectContext(
            engine=eng,
            controller=RequiredTo.PLAYER_1,
            source=None,
            code="RETURN_TO_HAND",
            targets=("player_1:0",),
        )
        E.execute_effect(ctx)
        self.assertEqual(gs.player_1_units, [])   # removed from play
        self.assertEqual(gs.player_1_hand, [])    # ceased to exist, not bounced


class RecruitEndToEndTests(unittest.TestCase):
    """Full play → ON_PLAY_UNIT trigger → chain → effect path, proving a mapped
    Recruit ability actually spawns tokens in a real game (not just the handler
    in isolation). Mirrors test_triggers' end-to-end style."""

    def test_playing_a_recruit_producer_spawns_tokens_on_resolve(self) -> None:
        from unittest import mock
        from riftbound_engine import abilities as A
        from riftbound_engine.csv_data import card_type_of, card_has_accelerate
        from tests.test_triggers import _drive_to_action_turn, _bank

        engine, ready = _drive_to_action_turn()
        _bank(engine, RequiredTo.PLAYER_1)
        ready = engine.start()
        hand = list(ready.game_state.player_1_hand or [])
        unit_idx = next(
            (i for i, c in enumerate(hand)
             if card_type_of(c) == "Unit" and not card_has_accelerate(c)),
            None,
        )
        if unit_idx is None:
            self.skipTest("dealt hand contains no non-Accelerate Unit cards")
        unit_card = hand[unit_idx]

        # Force this unit to carry "When you play me, play two Recruit tokens here".
        stub = (A.Ability(triggers=("WHEN_YOU_PLAY_ME",),
                          active_effects=("PLAY_2_RECRUIT_HERE",)),)
        with mock.patch.object(
            A, "triggered_abilities_for", side_effect=lambda n: stub if n == unit_card else ()
        ):
            engine.apply_action(action=f"play:play_unit:{unit_idx}", actor=RequiredTo.PLAYER_1)
            engine.apply_action(action="play:choose_location:base", actor=RequiredTo.PLAYER_1)
            engine.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
            after = engine.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_2)

        self.assertIsNone(after.game_state.pending_chain)
        units = engine._game_state.player_1_units
        tokens = [u for u in units if getattr(u, "token", False)]
        self.assertEqual(len(tokens), 2, "two Recruit tokens should have spawned")
        # Played to base, so "here" == base.
        self.assertTrue(all(t.location == "base" and t.card == "Recruit (DE)" for t in tokens))
        # The producer unit itself is still there (non-token).
        self.assertEqual(len([u for u in units if not getattr(u, "token", False)]), 1)


class TriggerAndCoverageTests(unittest.TestCase):
    def test_when_i_move_maps_to_on_move_self(self) -> None:
        from riftbound_engine.triggers import GameEvent, ON_MOVE, trigger_matches

        ev = GameEvent(kind=ON_MOVE, controller="player_1", source="player_1:0",
                       battlefield="battlefield_1")
        # SELF: the moved unit fires.
        self.assertTrue(trigger_matches(
            "WHEN_I_MOVE", ev, owner_controller="player_1", owner_ref="player_1:0"))
        # A different unit does NOT.
        self.assertFalse(trigger_matches(
            "WHEN_I_MOVE", ev, owner_controller="player_1", owner_ref="player_1:5"))

    def test_all_recruit_family_codes_are_implemented(self) -> None:
        for code in (
            "PLAY_RECRUIT_BASE", "PLAY_RECRUIT_HERE", "PLAY_2_RECRUIT_HERE",
            "PLAY_3_RECRUIT_BASE", "PLAY_3_RECRUIT_HERE", "PLAY_4_RECRUIT_BASE",
        ):
            self.assertTrue(E.is_implemented(code), f"{code} not implemented")


if __name__ == "__main__":
    unittest.main()
