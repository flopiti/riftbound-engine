import unittest

from riftbound_engine import (
    GameEngine,
    GameState,
    RequiredStep,
    RequiredTo,
    build_deck_from_id,
    registered_turn_action_verbs,
)
from riftbound_engine.csv_data import (
    card_domains_of,
    card_energy_of,
    card_power_of,
    card_spell_requirement_of,
    card_type_of,
    csv_cards,
)
from riftbound_engine.deck_files import list_deck_ids
from riftbound_engine.engine import PlayedSpell, Rune
from riftbound_engine.requirements import spell_playable, spell_target_plan


def _flood_runes(engine: GameEngine, actor: RequiredTo, count: int = 20) -> None:
    """Replace the active rune pool with ``count`` fresh, ready runes.

    Tests that focus on play/location mechanics use this so the player has
    plenty of runes to exhaust into Energy before playing a card. It mutates
    the live pool directly — the engine's snapshot getter still deep-copies
    safely.
    """
    pool = engine.runes_for(actor)
    pool.clear()
    pool.extend(Rune(domain="Fury") for _ in range(count))


def _bank_all_resources(engine: GameEngine, actor: RequiredTo, amount: int = 99) -> None:
    """Pre-bank ``amount`` of Energy and ``amount`` of Power in every known
    domain so the resource gates (Energy + domain Power) never reject a Unit
    in the player's hand.

    Used by tests that focus on play_unit / location plumbing rather than the
    cost gate itself — those are covered separately in
    PlayUnitEnergyCostTests and PlayUnitPowerCostTests."""
    engine.add_energy(actor, amount)
    # Pull every known rune domain from the CSV catalog.
    seen: set[str] = set()
    for card in csv_cards():
        for d in card_domains_of(card.name):
            if d:
                seen.add(d)
    for domain in sorted(seen):
        engine.add_power(actor, domain, amount)


def _build_energy(engine: GameEngine, actor: RequiredTo, amount: int) -> None:
    """Exhaust ``amount`` ready runes (in order) so ``actor`` has ``amount`` Energy.

    Mirrors what a player would do via repeated ``play:exhaust_rune:i``
    actions during the new action turn flow — without going through the
    public API so tests stay terse.
    """
    pool = engine.runes_for(actor)
    flipped = 0
    for rune in pool:
        if flipped >= amount:
            break
        if rune.exhausted:
            continue
        rune.exhausted = True
        flipped += 1
    if flipped < amount:
        raise AssertionError(
            f"not enough ready runes to build {amount} Energy (only flipped {flipped})"
        )
    engine.add_energy(actor, amount)


class GameEngineTests(unittest.TestCase):
    def test_counter_starts_at_zero(self) -> None:
        engine = GameEngine()
        self.assertEqual(engine.game_state, GameState(counter=0))

    def test_start_requires_player_1_to_choose_deck_first(self) -> None:
        engine = GameEngine()
        result = engine.start()

        self.assertEqual(result.required_action.name, RequiredStep.CHOOSE_DECK)
        self.assertEqual(result.required_action.actor, RequiredTo.PLAYER_1)
        self.assertGreaterEqual(len(result.player_1_options), 2)

    def test_deck_selection_is_sequential_player_1_then_player_2(self) -> None:
        engine = GameEngine()
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)

        self.assertEqual(second.required_action.name, RequiredStep.CHOOSE_DECK)
        self.assertEqual(second.required_action.actor, RequiredTo.PLAYER_2)
        self.assertIsNotNone(second.game_state.player_1_deck)
        self.assertIsNone(second.game_state.player_2_deck)

    def test_after_both_decks_selected_first_turn_choice_is_prompted(self) -> None:
        engine = GameEngine()
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)

        self.assertEqual(third.required_action.name, RequiredStep.CHOOSE_FIRST_TURN)
        self.assertIn(third.required_action.actor, (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2))
        self.assertEqual(len(third.game_state.player_1_deck.battlefields), 3)
        self.assertEqual(len(third.game_state.player_2_deck.battlefields), 3)
        self.assertEqual(len(third.game_state.player_1_deck.cards), 39)
        self.assertEqual(len(third.game_state.player_2_deck.cards), 39)
        self.assertEqual(third.game_state.counter, 0)
        self.assertFalse(third.game_state.started)
        self.assertEqual(third.game_state.first_turn_choice, third.required_action.actor)
        self.assertIsNone(third.game_state.first_turn)

    def test_deck_builder_enforces_required_shape(self) -> None:
        # Pick the first deck file that's actually on disk so this test
        # stays valid as decks are added or removed.
        deck_id = list_deck_ids()[0]
        deck = build_deck_from_id(deck_id)
        catalog_names = {card.name for card in csv_cards()}
        self.assertEqual(len(deck.battlefields), 3)
        self.assertTrue(deck.chosen_champion)
        self.assertTrue(deck.legend)
        self.assertEqual(len(deck.cards), 39)
        self.assertLessEqual(max(deck.cards.count(card) for card in set(deck.cards)), 3)
        self.assertEqual(len(deck.runes), 12)
        # Every rune has a non-empty domain; the exact composition is
        # deck-specific and validated separately in test_deck_runes_match_file.
        self.assertTrue(all(r.domain for r in deck.runes))
        self.assertTrue(all(name in catalog_names for name in deck.cards))
        self.assertIn(deck.chosen_champion, catalog_names)
        self.assertIn(deck.legend, catalog_names)
        self.assertTrue(all(bf in catalog_names for bf in deck.battlefields))

    def test_deck_runes_match_file(self) -> None:
        # irelia_nates is a Calm/Chaos archetype with 12 runes total; the
        # exact split is whatever the deck file declares. Sanity-check the
        # shape and that domain counts sum to 12.
        deck = build_deck_from_id("irelia_nates")
        self.assertLessEqual(max(deck.cards.count(card) for card in set(deck.cards)), 3)
        self.assertEqual(len(deck.runes), 12)
        from collections import Counter
        counts = Counter(r.domain for r in deck.runes)
        self.assertEqual(sum(counts.values()), 12)
        self.assertTrue(all(d for d in counts.keys()))

    def test_game_with_both_decks_selected_is_ready(self) -> None:
        ids = list_deck_ids()
        if len(ids) < 2:
            self.skipTest("need at least two deck files on disk")
        engine = GameEngine(
            game_state=GameState(
                counter=0,
                started=False,
                current_player=RequiredTo.PLAYER_1,
                player_1_deck=build_deck_from_id(ids[0]),
                player_2_deck=build_deck_from_id(ids[1]),
            )
        )
        result = engine.start()
        self.assertEqual(result.required_action.name, RequiredStep.CHOOSE_FIRST_TURN)
        self.assertIn(result.required_action.actor, (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2))
        self.assertEqual(result.game_state.counter, 0)
        self.assertEqual(result.game_state.first_turn_choice, result.required_action.actor)
        self.assertIsNone(result.game_state.first_turn)

    def test_first_turn_choice_rerolls_on_tie(self) -> None:
        rolls = iter([4, 4, 2, 6])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)

        self.assertEqual(third.required_action.actor, RequiredTo.PLAYER_2)
        self.assertEqual(third.game_state.first_turn_choice, RequiredTo.PLAYER_2)

    def test_first_turn_selection_completes_setup(self) -> None:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_2", actor=RequiredTo.PLAYER_1)

        self.assertIsNotNone(fourth.required_action)
        self.assertEqual(fourth.required_action.name, RequiredStep.CHOOSE_BATTLEFIELDS)
        # Battlefields are now chosen sequentially: player_1 picks first.
        self.assertEqual(fourth.required_action.actor, RequiredTo.PLAYER_1)
        self.assertEqual(fourth.game_state.first_turn, RequiredTo.PLAYER_2)
        self.assertEqual(fourth.game_state.counter, 0)
        self.assertFalse(fourth.game_state.started)

    def test_both_battlefields_are_required_before_setup_complete(self) -> None:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)

        # Battlefields are chosen SEQUENTIALLY: player_1 first (only their
        # deck's battlefields), then player_2.
        self.assertEqual(fourth.required_action.name, RequiredStep.CHOOSE_BATTLEFIELDS)
        self.assertEqual(fourth.required_action.actor, RequiredTo.PLAYER_1)
        self.assertEqual(len(fourth.player_1_options), 3)
        self.assertEqual(len(fourth.player_2_options), 0)

        fifth = engine.apply_action(action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        self.assertEqual(fifth.required_action.name, RequiredStep.CHOOSE_BATTLEFIELDS)
        self.assertEqual(fifth.required_action.actor, RequiredTo.PLAYER_2)
        self.assertEqual(len(fifth.player_2_options), 3)
        self.assertEqual(fifth.game_state.battlefield_1, fourth.player_1_options[0])
        self.assertEqual(fifth.game_state.player_1_base, fourth.player_1_options[0])
        self.assertIsNone(fifth.game_state.battlefield_2)
        self.assertIsNone(fifth.game_state.player_2_base)

        sixth = engine.apply_action(action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        self.assertEqual(sixth.required_action.name, RequiredStep.CHOOSE_MULLIGAN)
        self.assertEqual(sixth.required_action.actor, RequiredTo.BOTH)
        # Mulligan options are full `mulligan_resolve:player_N:<csv>` actions:
        # keep-all + bottom-each-single + bottom-each-pair = 1 + 4 + C(4,2) = 11.
        self.assertEqual(len(sixth.player_1_options), 11)
        self.assertEqual(len(sixth.player_2_options), 11)
        self.assertTrue(
            all(o.startswith("mulligan_resolve:player_1:") for o in sixth.player_1_options)
        )
        self.assertIn("mulligan_resolve:player_1:", sixth.player_1_options)  # keep-all
        self.assertEqual(sixth.game_state.battlefield_2, fifth.player_2_options[0])
        self.assertEqual(sixth.game_state.player_2_base, fifth.player_2_options[0])
        self.assertFalse(sixth.game_state.is_mulligan_done)
        self.assertEqual(sixth.game_state.counter, 0)
        self.assertFalse(sixth.game_state.started)

        seventh = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:",
            actor=RequiredTo.PLAYER_1,
        )
        self.assertEqual(seventh.required_action.name, RequiredStep.CHOOSE_MULLIGAN)
        self.assertTrue(seventh.game_state.mulligan_player_1_resolved)
        self.assertFalse(seventh.game_state.mulligan_player_2_resolved)
        self.assertEqual(len(seventh.game_state.player_1_hand), 4)
        self.assertEqual(len(seventh.game_state.player_1_library), 35)
        self.assertIsNone(seventh.game_state.player_2_hand)

        eighth = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:",
            actor=RequiredTo.PLAYER_2,
        )
        self.assertIsNotNone(eighth.required_action)
        self.assertEqual(eighth.required_action.name, RequiredStep.ACTION_TURN)
        self.assertEqual(eighth.required_action.actor, eighth.game_state.first_turn)
        self.assertTrue(eighth.game_state.is_mulligan_done)
        # First player’s ABCD runs immediately (including draw): library 35→34, hand 4→5.
        self.assertEqual(len(eighth.game_state.player_1_library), 34)
        self.assertEqual(len(eighth.game_state.player_2_library), 35)
        self.assertEqual(len(eighth.game_state.player_1_hand), 5)
        self.assertEqual(len(eighth.game_state.player_2_hand), 4)
        self.assertEqual(eighth.game_state.counter, 1)
        self.assertTrue(eighth.game_state.started)
        self.assertEqual(eighth.game_state.total_turn_number, 1)
        self.assertEqual(eighth.game_state.player_1_turn_number, 1)
        self.assertEqual(eighth.game_state.player_2_turn_number, 0)

    def test_after_abcd_action_turn_play_end_turn_advances_to_next_player_abcd(self) -> None:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)
        fifth = engine.apply_action(action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        sixth = engine.apply_action(action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        engine.apply_action(action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:", actor=RequiredTo.PLAYER_1)
        after_mulligan = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:",
            actor=RequiredTo.PLAYER_2,
        )
        self.assertEqual(after_mulligan.required_action.name, RequiredStep.ACTION_TURN)
        ft = after_mulligan.game_state.first_turn
        self.assertEqual(after_mulligan.game_state.current_player, ft)

        gs = after_mulligan.game_state
        self.assertEqual(len(gs.player_1_runes), 2)
        self.assertEqual(len(gs.player_1_rune_library or []), 10)
        self.assertEqual(len(gs.player_1_hand or []), 5)
        self.assertEqual(len(gs.player_1_library or []), 34)

        self.assertIn("end_turn", registered_turn_action_verbs())
        done = engine.apply_action(action="play:end_turn", actor=after_mulligan.required_action.actor)
        self.assertEqual(done.required_action.name, RequiredStep.ACTION_TURN)
        self.assertEqual(done.required_action.actor, RequiredTo.PLAYER_2)
        self.assertTrue(done.game_state.abcd_a_done)
        self.assertEqual(done.game_state.player_1_turn_number, 1)
        self.assertEqual(done.game_state.player_2_turn_number, 1)
        self.assertEqual(done.game_state.total_turn_number, 2)
        self.assertEqual(len(done.game_state.player_2_runes), 3)
        self.assertEqual(len(done.game_state.player_2_rune_library or []), 9)

        end_p2 = engine.apply_action(action="play:end_turn", actor=done.required_action.actor)
        self.assertEqual(end_p2.required_action.name, RequiredStep.ACTION_TURN)
        self.assertEqual(end_p2.required_action.actor, RequiredTo.PLAYER_1)
        self.assertEqual(len(end_p2.game_state.player_1_runes), 4)
        self.assertEqual(end_p2.game_state.global_channel_count, 3)

    def test_action_turn_options_offer_play_unit_per_hand_card_and_end_turn(self) -> None:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)
        fifth = engine.apply_action(action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        engine.apply_action(action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:", actor=RequiredTo.PLAYER_1)
        ready = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:",
            actor=RequiredTo.PLAYER_2,
        )

        # P1 went first; after ABCD they're sitting in the action turn with 5 cards in hand.
        self.assertEqual(ready.required_action.name, RequiredStep.ACTION_TURN)
        self.assertEqual(ready.required_action.actor, RequiredTo.PLAYER_1)
        self.assertEqual(len(ready.game_state.player_1_hand or []), 5)

        # Give the player a single Fury rune pool + max Energy so every Unit
        # is affordable. This keeps the assertion focused on
        # play_unit/play_spell/end_turn surfacing — the Energy cost gate is
        # covered separately in PlayUnitEnergyCostTests.
        _flood_runes(engine, RequiredTo.PLAYER_1)
        # Bank an absurd amount of Energy AND Power across every domain
        # without exhausting any runes so every Unit in hand clears both the
        # Energy and Power gates.
        _bank_all_resources(engine, RequiredTo.PLAYER_1, 99)
        ready = engine.start()

        # The engine no longer surfaces rune-resource actions
        # (exhaust_rune / recycle_rune / exhaust_and_recycle_rune) as
        # standalone options — clients drive payments via the
        # shortcuts.ts plan and just receive the final play_unit /
        # play_spell call back here. So the option list is just play_unit
        # for each affordable unique Unit, play_spell for each affordable
        # unique Spell, then end_turn. Duplicate card names in hand
        # collapse to the leftmost occurrence since only one action can
        # be taken at a time.
        hand = ready.game_state.player_1_hand or []
        seen: set[str] = set()
        unit_opts: list[str] = []
        for i, card in enumerate(hand):
            if card in seen:
                continue
            if card_type_of(card) != "Unit":
                continue
            seen.add(card)
            unit_opts.append(f"play:play_unit:{i}")
        spell_opts: list[str] = []
        for i, card in enumerate(hand):
            if card in seen:
                continue
            if card_type_of(card) != "Spell":
                continue
            # A spell is only offered if its Spell Choice Requirement can be
            # met on the current board (no units are in play here, so any
            # spell needing a unit target is correctly gated out).
            if not spell_playable(ready.game_state, card):
                continue
            seen.add(card)
            spell_opts.append(f"play:play_spell:{i}")
        # The chosen champion plays like a hand unit and is surfaced as a flat
        # option here too (resources are flooded, so it's affordable and not
        # yet played). It comes after the hand plays, before end_turn.
        expected = unit_opts + spell_opts + ["play:play_champion", "play:end_turn"]
        self.assertEqual(ready.player_1_options, expected)
        # Sanity: none of the rune-resource verbs are surfaced anywhere
        # in the option list any more.
        for opt in ready.player_1_options:
            self.assertFalse(
                opt.startswith("play:exhaust_rune:")
                or opt.startswith("play:recycle_rune:")
                or opt.startswith("play:exhaust_and_recycle_rune:"),
                f"unexpected rune option surfaced: {opt}",
            )
        # Inactive player has no options during the active player's action turn.
        self.assertEqual(ready.player_2_options, [])

    def _drive_to_action_turn(self) -> tuple["GameEngine", "EngineOutput"]:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)
        fifth = engine.apply_action(action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        engine.apply_action(action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:", actor=RequiredTo.PLAYER_1)
        ready = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:",
            actor=RequiredTo.PLAYER_2,
        )
        return engine, ready

    def _first_unit_index(self, hand: list[str]) -> int:
        for i, card in enumerate(hand):
            if card_type_of(card) == "Unit":
                return i
        raise unittest.SkipTest("dealt hand contains no Unit-type cards")

    def test_play_unit_parks_card_in_pending_then_choose_location_commits(self) -> None:
        engine, ready = self._drive_to_action_turn()
        # Pre-build Energy AND Power across every domain so the cost gate
        # (Energy + domain Power — covered separately in PlayUnitEnergyCostTests
        # and PlayUnitPowerCostTests) doesn't reject the play. We still
        # flood runes so the engine can show exhaust_rune options afterwards.
        _flood_runes(engine, RequiredTo.PLAYER_1)
        _bank_all_resources(engine, RequiredTo.PLAYER_1, 99)
        ready = engine.start()
        original_hand = list(ready.game_state.player_1_hand or [])
        self.assertGreaterEqual(len(original_hand), 1)

        unit_idx = self._first_unit_index(original_hand)
        unit_card = original_hand[unit_idx]
        hand_after_pop = original_hand[:unit_idx] + original_hand[unit_idx + 1 :]
        cost = engine.card_energy_cost(unit_card)
        energy_before = engine.player_energy(RequiredTo.PLAYER_1)

        # Step A — play_unit deducts Energy and parks the card in pending_play.
        played = engine.apply_action(
            action=f"play:play_unit:{unit_idx}", actor=RequiredTo.PLAYER_1
        )
        self.assertEqual(played.game_state.player_1_hand, hand_after_pop)
        self.assertEqual(played.game_state.player_1_units, [])
        self.assertIsNotNone(played.game_state.pending_play)
        self.assertEqual(played.game_state.pending_play.card, unit_card)
        self.assertEqual(played.game_state.pending_play.actor, RequiredTo.PLAYER_1)
        self.assertEqual(
            played.game_state.player_1_energy,
            energy_before - cost,
            "play_unit must deduct the card's Energy cost from the pool",
        )
        # Active player's options are gated by territory control: at match
        # start neither battlefield has a controller, so only the player's
        # own base is offered.
        self.assertEqual(played.player_1_options, ["play:choose_location:base"])

        # Can't double-play while a play is pending; can't end turn either;
        # can't exhaust more runes while a play is pending.
        with self.assertRaises(ValueError):
            engine.apply_action(
                action=f"play:play_unit:{unit_idx}", actor=RequiredTo.PLAYER_1
            )
        with self.assertRaises(ValueError):
            engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_1)
        with self.assertRaises(ValueError):
            engine.apply_action(action="play:exhaust_rune:0", actor=RequiredTo.PLAYER_1)
        # Wrong actor can't resolve the location.
        with self.assertRaises(ValueError):
            engine.apply_action(action="play:choose_location:base", actor=RequiredTo.PLAYER_2)
        # Unknown location rejected.
        with self.assertRaises(ValueError):
            engine.apply_action(action="play:choose_location:moon", actor=RequiredTo.PLAYER_1)
        # Battlefield_2 has no controller → rejected by the control rule.
        with self.assertRaises(ValueError):
            engine.apply_action(
                action="play:choose_location:battlefield_2", actor=RequiredTo.PLAYER_1
            )

        # Step B — base is always controlled by oneself, so this commits.
        settled = engine.apply_action(
            action="play:choose_location:base", actor=RequiredTo.PLAYER_1
        )
        self.assertIsNone(settled.game_state.pending_play)
        # pending_payment is no longer used by the new flow.
        self.assertIsNone(settled.game_state.pending_payment)
        self.assertEqual(len(settled.game_state.player_1_units), 1)
        self.assertEqual(settled.game_state.player_1_units[0].card, unit_card)
        self.assertEqual(settled.game_state.player_1_units[0].location, "base")

        # Options now list the remaining unique Unit cards (all still
        # affordable thanks to the Energy headroom), the remaining unique
        # Spell cards, and end_turn. Rune-resource actions are no longer
        # surfaced — clients drive payment through the shortcuts plan.
        # Duplicate cards in hand collapse to the leftmost occurrence.
        seen: set[str] = set()
        unit_opts: list[str] = []
        for i, card in enumerate(hand_after_pop):
            if card in seen:
                continue
            if card_type_of(card) != "Unit":
                continue
            seen.add(card)
            unit_opts.append(f"play:play_unit:{i}")
        spell_opts: list[str] = []
        for i, card in enumerate(hand_after_pop):
            if card in seen:
                continue
            if card_type_of(card) != "Spell":
                continue
            # Mirror the engine's requirement gate (a unit is now on the
            # board, so unit-target spells may or may not qualify depending
            # on their per-unit filters).
            if not spell_playable(settled.game_state, card):
                continue
            seen.add(card)
            spell_opts.append(f"play:play_spell:{i}")
        # Gears are offered after spells (same cost gates, no requirement).
        gear_opts: list[str] = []
        for i, card in enumerate(hand_after_pop):
            if card in seen:
                continue
            if card_type_of(card) != "Gear":
                continue
            seen.add(card)
            gear_opts.append(f"play:play_gear:{i}")
        # Champion still available + affordable (resources flooded) → offered.
        expected = unit_opts + spell_opts + gear_opts + ["play:play_champion", "play:end_turn"]
        self.assertEqual(settled.player_1_options, expected)

    def test_play_unit_rejects_bad_indices_and_inactive_player(self) -> None:
        engine, ready = self._drive_to_action_turn()
        hand = ready.game_state.player_1_hand or []
        unit_idx = self._first_unit_index(hand)
        with self.assertRaises(ValueError):
            engine.apply_action(action="play:play_unit:99", actor=RequiredTo.PLAYER_1)
        with self.assertRaises(ValueError):
            engine.apply_action(
                action=f"play:play_unit:{unit_idx}", actor=RequiredTo.PLAYER_2
            )

    def test_play_unit_rejects_non_unit_card_type(self) -> None:
        engine, ready = self._drive_to_action_turn()
        hand = ready.game_state.player_1_hand or []
        non_unit_idx = next(
            (i for i, card in enumerate(hand) if card_type_of(card) != "Unit"),
            None,
        )
        if non_unit_idx is None:
            self.skipTest("dealt hand has only Unit-type cards")
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(
                action=f"play:play_unit:{non_unit_idx}", actor=RequiredTo.PLAYER_1
            )
        self.assertIn("cannot be played as a unit", str(ctx.exception))

    def test_battlefield_control_unlocks_that_location_for_its_controller(self) -> None:
        engine, ready = self._drive_to_action_turn()
        # Give player_1 control of battlefield_1 directly. No rule exists yet
        # to gain control — this simulates a future control-granting effect.
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_1
        # Ample runes + banked Energy/Power so the cost gate (exercised in
        # PlayUnitEnergyCostTests and PlayUnitPowerCostTests) doesn't reject
        # the play.
        _flood_runes(engine, RequiredTo.PLAYER_1)
        _bank_all_resources(engine, RequiredTo.PLAYER_1, 99)

        hand = ready.game_state.player_1_hand or []
        unit_idx = self._first_unit_index(hand)
        played = engine.apply_action(
            action=f"play:play_unit:{unit_idx}", actor=RequiredTo.PLAYER_1
        )
        self.assertEqual(
            played.player_1_options,
            ["play:choose_location:base", "play:choose_location:battlefield_1"],
        )
        # battlefield_2 still uncontrolled → rejected.
        with self.assertRaises(ValueError):
            engine.apply_action(
                action="play:choose_location:battlefield_2", actor=RequiredTo.PLAYER_1
            )
        # battlefield_1 now allowed for P1.
        settled = engine.apply_action(
            action="play:choose_location:battlefield_1", actor=RequiredTo.PLAYER_1
        )
        self.assertEqual(settled.game_state.player_1_units[0].location, "battlefield_1")

    def test_opponent_cannot_play_on_a_battlefield_the_other_player_controls(self) -> None:
        engine, ready = self._drive_to_action_turn()
        # P2 controls battlefield_1 — P1 (active) must not be able to use it.
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_2
        _flood_runes(engine, RequiredTo.PLAYER_1)
        _bank_all_resources(engine, RequiredTo.PLAYER_1, 99)

        hand = ready.game_state.player_1_hand or []
        unit_idx = self._first_unit_index(hand)
        played = engine.apply_action(
            action=f"play:play_unit:{unit_idx}", actor=RequiredTo.PLAYER_1
        )
        # Only base is offered to P1; battlefield_1 belongs to the opponent.
        self.assertEqual(played.player_1_options, ["play:choose_location:base"])
        with self.assertRaises(ValueError):
            engine.apply_action(
                action="play:choose_location:battlefield_1", actor=RequiredTo.PLAYER_1
            )

    def _first_spell_index(self, hand: list[str]) -> int:
        """Pick the first Spell-type card in the hand, or skip the test if there are none."""
        for i, card in enumerate(hand):
            if card_type_of(card) == "Spell":
                return i
        raise unittest.SkipTest("dealt hand contains no Spell-type cards")

    def _first_directly_castable_spell_index(self, engine, hand: list[str]) -> int:
        """First Spell that commits immediately on play — i.e. its requirement
        forces no target pick (blank / min-0 / unknown selector) AND is
        satisfiable on the current board. Such a spell lands straight on the
        spell stack, which is what the cost/clear tests below assert. Spells
        that park in pending_spell_choice are skipped.

        "Forces no target pick" must match the engine's actual parking rule in
        action_turn/builtins.py::_play_spell, which is ``spell_target_plan(req)``
        being empty — NOT ``selectable_unit_requirement``, which reports None
        for multi-phrase trees that DO park (e.g. ANY UNIT (1)|ANY UNIT (1)).

        [Repeat] spells are also skipped: they pause on a repeat decision
        instead of landing straight on the chain, which would break the
        chain/clear assertions these helpers feed."""
        from riftbound_engine.csv_data import card_has_repeat

        for i, card in enumerate(hand):
            if card_type_of(card) != "Spell":
                continue
            if card_has_repeat(card):
                continue
            req = card_spell_requirement_of(card)
            if not spell_target_plan(req) and spell_playable(engine._game_state, card):
                return i
        raise unittest.SkipTest("dealt hand has no directly-castable (no-target-pick) spell")

    def test_play_spell_moves_card_from_hand_to_spells_and_deducts_cost(self) -> None:
        """play_spell is the Spell counterpart to play_unit: same Energy/Power
        cost gates, but the card lands in player_X_spells with no
        choose_location step in between."""
        engine, ready = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        _bank_all_resources(engine, RequiredTo.PLAYER_1, 99)
        ready = engine.start()
        hand_before = list(ready.game_state.player_1_hand or [])
        spell_idx = self._first_directly_castable_spell_index(engine, hand_before)
        spell_card = hand_before[spell_idx]

        energy_before = engine.player_energy(RequiredTo.PLAYER_1)
        energy_cost = engine.card_energy_cost(spell_card)

        out = engine.apply_action(
            action=f"play:play_spell:{spell_idx}", actor=RequiredTo.PLAYER_1
        )
        gs = out.game_state
        # Hand had the card removed at spell_idx.
        expected_hand = hand_before[:spell_idx] + hand_before[spell_idx + 1 :]
        self.assertEqual(gs.player_1_hand, expected_hand)
        # Energy was deducted up front, at play time.
        self.assertEqual(gs.player_1_energy, energy_before - energy_cost)
        self.assertIsNone(gs.pending_play)
        # The spell shows in the pile/overlay immediately on cast AND opens
        # the chain (priority window). Resolution just closes the chain.
        self.assertIsNotNone(gs.pending_chain)
        self.assertEqual([i.card for i in gs.pending_chain.items], [spell_card])
        self.assertEqual(len(gs.player_1_spells), 1)
        self.assertEqual(gs.player_1_spells[0].card, spell_card)

        engine.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        resolved = engine.apply_action(
            action="play:pass_priority", actor=RequiredTo.PLAYER_2
        )
        gs = resolved.game_state
        self.assertIsNone(gs.pending_chain)
        self.assertEqual(len(gs.player_1_spells), 1)
        self.assertEqual(gs.player_1_spells[0].card, spell_card)

    def test_play_spell_rejects_non_spell_cards_and_bad_indices(self) -> None:
        engine, ready = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        _bank_all_resources(engine, RequiredTo.PLAYER_1, 99)
        hand = ready.game_state.player_1_hand or []

        # Out-of-range index.
        with self.assertRaises(ValueError):
            engine.apply_action(action="play:play_spell:99", actor=RequiredTo.PLAYER_1)

        # Non-Spell card (e.g. a Unit) rejected.
        non_spell_idx = next(
            (i for i, card in enumerate(hand) if card_type_of(card) != "Spell"),
            None,
        )
        if non_spell_idx is None:
            self.skipTest("dealt hand contains no non-Spell cards to test against")
        with self.assertRaises(ValueError):
            engine.apply_action(
                action=f"play:play_spell:{non_spell_idx}", actor=RequiredTo.PLAYER_1
            )

        # Inactive player rejected.
        spell_idx = self._first_spell_index(hand)
        with self.assertRaises(ValueError):
            engine.apply_action(
                action=f"play:play_spell:{spell_idx}", actor=RequiredTo.PLAYER_2
            )

    def test_end_turn_clears_spell_piles_for_both_players(self) -> None:
        """Spells are visual-only for now and have no resolution step, but the
        right-side spell overlay should not accumulate cast spells past the
        turn boundary. ``_advance_turn`` (triggered by ``play:end_turn``)
        wipes both players' spell stacks so the overlay starts the next turn
        empty."""
        engine, ready = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        _bank_all_resources(engine, RequiredTo.PLAYER_1, 99)
        ready = engine.start()
        hand = list(ready.game_state.player_1_hand or [])
        spell_idx = self._first_directly_castable_spell_index(engine, hand)
        spell_card = hand[spell_idx]

        # P1 casts a spell — it goes on the chain — then both players pass so
        # the chain resolves it into P1's spell pile.
        engine.apply_action(
            action=f"play:play_spell:{spell_idx}", actor=RequiredTo.PLAYER_1
        )
        engine.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_1)
        out = engine.apply_action(action="play:pass_priority", actor=RequiredTo.PLAYER_2)
        self.assertIsNone(out.game_state.pending_chain)
        self.assertEqual(len(out.game_state.player_1_spells), 1)
        self.assertEqual(out.game_state.player_1_spells[0].card, spell_card)

        # Seed P2's pile too so we verify *both* sides clear. The engine
        # doesn't currently let P2 cast during P1's turn through the public
        # action API, so we mutate the state directly — this test is about
        # the end-of-turn wipe, not the cast path.
        engine._game_state.player_2_spells.append(PlayedSpell(card=spell_card))
        self.assertEqual(len(engine._game_state.player_2_spells), 1)

        after_end = engine.apply_action(
            action="play:end_turn", actor=RequiredTo.PLAYER_1
        )
        self.assertEqual(after_end.game_state.player_1_spells, [])
        self.assertEqual(after_end.game_state.player_2_spells, [])

    def test_channel_rune_count_follows_first_second_then_two_schedule(self) -> None:
        gs = GameState(global_channel_count=0)
        engine = GameEngine(game_state=gs)
        self.assertEqual(engine._channel_rune_count_for_next_channel(), 2)
        gs.global_channel_count = 1
        self.assertEqual(engine._channel_rune_count_for_next_channel(), 3)
        gs.global_channel_count = 2
        self.assertEqual(engine._channel_rune_count_for_next_channel(), 2)
        gs.global_channel_count = 99
        self.assertEqual(engine._channel_rune_count_for_next_channel(), 2)


class CardEnergyLookupTests(unittest.TestCase):
    """CSV-driven Energy lookup must return ints for real cards and None when
    the row has no numeric Energy (e.g. Battlefield/Legend/Rune rows)."""

    def test_known_units_have_integer_energy(self) -> None:
        # Pick the first Unit row in the CSV; it must report an int cost.
        unit = next((c for c in csv_cards() if c.card_type == "Unit"), None)
        self.assertIsNotNone(unit, "CSV must contain at least one Unit row")
        cost = card_energy_of(unit.name)
        self.assertIsInstance(cost, int)
        self.assertGreaterEqual(cost, 0)

    def test_full_name_suffix_resolves_like_card_type_of(self) -> None:
        # "Miss Fortune, Bounty Hunter" → CSV's "Bounty Hunter" lookup path.
        for c in csv_cards():
            if c.card_type != "Unit":
                continue
            decorated = f"Some Legend, {c.name}"
            if card_energy_of(c.name) is None:
                continue
            self.assertEqual(card_energy_of(decorated), card_energy_of(c.name))
            break

    def test_unknown_card_returns_none(self) -> None:
        self.assertIsNone(card_energy_of("Definitely Not A Real Card 1234"))
        self.assertIsNone(card_energy_of(""))


class PlayUnitEnergyCostTests(unittest.TestCase):
    """Cost gate, rune exhaustion flow, and Awake (A) ready behavior."""

    def _drive_to_action_turn(self) -> tuple[GameEngine, "EngineOutput"]:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)
        fifth = engine.apply_action(action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        engine.apply_action(action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:", actor=RequiredTo.PLAYER_1)
        ready = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:",
            actor=RequiredTo.PLAYER_2,
        )
        # Unused for now — kept in case future tests want the post-P1-deck output.
        _ = third
        return engine, ready

    def _first_affordable_unit_index(self, hand: list[str], ready_runes: int) -> int:
        for i, card in enumerate(hand):
            if card_type_of(card) != "Unit":
                continue
            cost = card_energy_of(card)
            if cost is None:
                cost = 0
            if cost <= ready_runes:
                return i
        raise unittest.SkipTest(
            f"dealt hand has no Unit affordable with {ready_runes} ready runes"
        )

    def test_play_unit_options_exclude_unaffordable_cards(self) -> None:
        engine, ready = self._drive_to_action_turn()
        # Force exactly 1 ready rune and pre-bank 1 Energy → any cost-2+
        # unit drops off the menu. No Power is banked, so units that also
        # require Power also drop off (covered separately in
        # PlayUnitPowerCostTests). Rune-resource actions are no longer
        # surfaced as options; the engine only emits the final play_*
        # verbs and lets the shortcut plan handle payment.
        pool = engine.runes_for(RequiredTo.PLAYER_1)
        pool.clear()
        pool.append(Rune(domain="Fury"))
        engine.add_energy(RequiredTo.PLAYER_1, 1)
        ready = engine.start()

        hand = ready.game_state.player_1_hand or []
        # The same affordability gate applies to Spells — only ones whose
        # Energy cost <= 1 and Power cost == 0 (since we banked no Power)
        # are offered. Identical cards in hand collapse to one option at
        # the leftmost index.
        seen: set[str] = set()
        unit_opts: list[str] = []
        for i, card in enumerate(hand):
            if card in seen:
                continue
            if card_type_of(card) != "Unit":
                continue
            if (card_energy_of(card) or 0) > 1:
                continue
            if (card_power_of(card) or 0) > 0:
                continue
            seen.add(card)
            unit_opts.append(f"play:play_unit:{i}")
        spell_opts: list[str] = []
        for i, card in enumerate(hand):
            if card in seen:
                continue
            if card_type_of(card) != "Spell":
                continue
            if (card_energy_of(card) or 0) > 1:
                continue
            if (card_power_of(card) or 0) > 0:
                continue
            # Beyond affordability, the spell must also have a valid target
            # (empty board here ⇒ unit-target spells are gated out).
            if not spell_playable(ready.game_state, card):
                continue
            seen.add(card)
            spell_opts.append(f"play:play_spell:{i}")
        expected = unit_opts + spell_opts + ["play:end_turn"]
        self.assertEqual(ready.player_1_options, expected)

    def test_play_unit_rejects_card_more_expensive_than_available_energy(self) -> None:
        engine, ready = self._drive_to_action_turn()
        # 0 Energy banked → any positive-cost unit is rejected by the gate
        # regardless of how many ready runes the player has.
        _flood_runes(engine, RequiredTo.PLAYER_1)
        ready = engine.start()

        hand = ready.game_state.player_1_hand or []
        expensive_idx = next(
            (
                i for i, card in enumerate(hand)
                if card_type_of(card) == "Unit" and (card_energy_of(card) or 0) > 0
            ),
            None,
        )
        if expensive_idx is None:
            self.skipTest("dealt hand has no positive-cost Unit cards")
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(
                action=f"play:play_unit:{expensive_idx}", actor=RequiredTo.PLAYER_1
            )
        self.assertIn("cannot play", str(ctx.exception))

    def test_choose_location_commits_unit_with_no_pending_payment(self) -> None:
        engine, ready = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        ready = engine.start()
        hand = ready.game_state.player_1_hand or []
        # Find a positive-Energy Unit with NO Power requirement so this
        # test isolates the Energy deduction path. Power gating is covered
        # in PlayUnitPowerCostTests.
        target_idx = next(
            (
                i for i, card in enumerate(hand)
                if card_type_of(card) == "Unit"
                and (card_energy_of(card) or 0) > 0
                and (card_power_of(card) or 0) == 0
            ),
            None,
        )
        if target_idx is None:
            self.skipTest("dealt hand has no positive-Energy Power-0 Unit cards")
        target_cost = card_energy_of(hand[target_idx]) or 0

        # Build exactly the cost in Energy first (the new flow).
        _build_energy(engine, RequiredTo.PLAYER_1, target_cost)
        self.assertEqual(engine.player_energy(RequiredTo.PLAYER_1), target_cost)

        engine.apply_action(action=f"play:play_unit:{target_idx}", actor=RequiredTo.PLAYER_1)
        # play_unit drains the pool.
        self.assertEqual(engine.player_energy(RequiredTo.PLAYER_1), 0)
        after_loc = engine.apply_action(
            action="play:choose_location:base", actor=RequiredTo.PLAYER_1
        )
        # Card is committed and pending_payment is never opened.
        self.assertIsNone(after_loc.game_state.pending_play)
        self.assertIsNone(after_loc.game_state.pending_payment)
        self.assertEqual(after_loc.game_state.player_1_energy, 0)
        # Player can immediately end the turn or take other actions
        # (subject to whatever ready runes remain).
        self.assertIn("play:end_turn", after_loc.player_1_options)

    def test_exhaust_rune_produces_energy_and_taps_runes(self) -> None:
        engine, ready = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        ready = engine.start()

        # Out-of-range and inactive-player paths still reject.
        with self.assertRaises(ValueError):
            engine.apply_action(action="play:exhaust_rune:99", actor=RequiredTo.PLAYER_1)
        with self.assertRaises(ValueError):
            engine.apply_action(action="play:exhaust_rune:0", actor=RequiredTo.PLAYER_2)

        # Exhausting three ready runes produces 3 Energy.
        for i in range(3):
            after = engine.apply_action(
                action=f"play:exhaust_rune:{i}", actor=RequiredTo.PLAYER_1
            )
        self.assertEqual(after.game_state.player_1_energy, 3)
        # The first three runes are exhausted; the rest remain ready.
        pool = engine.runes_for(RequiredTo.PLAYER_1)
        for i, rune in enumerate(pool):
            self.assertEqual(rune.exhausted, i < 3, f"rune {i} state")
        # Re-exhausting an already-exhausted rune is still rejected.
        with self.assertRaises(ValueError):
            engine.apply_action(action="play:exhaust_rune:0", actor=RequiredTo.PLAYER_1)

    def test_awake_step_readies_active_player_runes(self) -> None:
        engine, ready = self._drive_to_action_turn()
        # Pretend P1 already spent both channeled runes.
        for rune in engine.runes_for(RequiredTo.PLAYER_1):
            rune.exhausted = True
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_1)
        # Skip P2's turn back to P1; A runs implicitly at the next ABCD.
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_2)
        gs = engine.game_state
        # Back to P1, and now all P1's runes are ready again after Awake.
        self.assertEqual(gs.current_player, RequiredTo.PLAYER_1)
        for rune in gs.player_1_runes:
            self.assertFalse(rune.exhausted)

    def test_exhaust_rune_options_are_no_longer_surfaced(self) -> None:
        # Rune-resource actions are still callable via the action endpoint
        # (the shortcut plan composes them client-side), but the engine
        # no longer offers them as standalone options. This test pins the
        # new behavior so we don't regress and start surfacing them again.
        engine, ready = self._drive_to_action_turn()
        pool = engine.runes_for(RequiredTo.PLAYER_1)
        pool.clear()
        pool.extend(
            [
                Rune(domain="Fury"),
                Rune(domain="Fury"),
                Rune(domain="Fury"),
                Rune(domain="Body"),
                Rune(domain="Body"),
                Rune(domain="Mind"),
            ]
        )
        ready = engine.start()
        rune_opts = [
            opt for opt in ready.player_1_options
            if opt.startswith("play:exhaust_rune:")
            or opt.startswith("play:recycle_rune:")
            or opt.startswith("play:exhaust_and_recycle_rune:")
        ]
        self.assertEqual(rune_opts, [])
        # The action handler still works when called directly — the engine
        # only stopped *advertising* it, not *implementing* it.
        engine.apply_action(action="play:exhaust_rune:0", actor=RequiredTo.PLAYER_1)
        self.assertTrue(engine.runes_for(RequiredTo.PLAYER_1)[0].exhausted)

    def test_exhaust_rune_blocked_while_play_is_pending(self) -> None:
        engine, ready = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        # Bank enough Energy to play a positive-Energy Power-0 unit, then
        # start the play. Restricting to Power-0 keeps this test focused on
        # the pending-play gate (Power gating is covered elsewhere).
        ready = engine.start()
        hand = ready.game_state.player_1_hand or []
        target_idx = next(
            (
                i for i, card in enumerate(hand)
                if card_type_of(card) == "Unit"
                and (card_energy_of(card) or 0) > 0
                and (card_power_of(card) or 0) == 0
            ),
            None,
        )
        if target_idx is None:
            self.skipTest("dealt hand has no positive-Energy Power-0 Unit cards")
        cost = card_energy_of(hand[target_idx]) or 0
        _build_energy(engine, RequiredTo.PLAYER_1, cost)
        engine.apply_action(action=f"play:play_unit:{target_idx}", actor=RequiredTo.PLAYER_1)
        # pending_play is set → exhaust_rune is now blocked until location chosen.
        with self.assertRaises(ValueError):
            engine.apply_action(action="play:exhaust_rune:0", actor=RequiredTo.PLAYER_1)

    def test_inactive_player_cannot_exhaust_runes(self) -> None:
        engine, _ = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_2)
        # P1 is active; P2 trying to exhaust is rejected.
        with self.assertRaises(ValueError):
            engine.apply_action(action="play:exhaust_rune:0", actor=RequiredTo.PLAYER_2)

    def test_energy_clears_at_turn_end(self) -> None:
        engine, _ = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        # Bank a few Energy then end the turn without spending it.
        for i in range(3):
            engine.apply_action(action=f"play:exhaust_rune:{i}", actor=RequiredTo.PLAYER_1)
        self.assertEqual(engine.player_energy(RequiredTo.PLAYER_1), 3)
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_1)
        # Turn change wipes both pools.
        self.assertEqual(engine.player_energy(RequiredTo.PLAYER_1), 0)
        self.assertEqual(engine.player_energy(RequiredTo.PLAYER_2), 0)

    def test_recycle_options_are_no_longer_surfaced(self) -> None:
        # As with exhaust_rune, recycle_rune and exhaust_and_recycle_rune
        # are no longer offered as standalone options — the shortcuts
        # plan emits them as part of a payment chain instead.
        engine, _ = self._drive_to_action_turn()
        pool = engine.runes_for(RequiredTo.PLAYER_1)
        pool.clear()
        pool.extend(
            [
                Rune(domain="Fury"),
                Rune(domain="Fury"),
                Rune(domain="Body"),
                Rune(domain="Mind", exhausted=True),
            ]
        )
        ready = engine.start()
        recycle_opts = [
            opt for opt in ready.player_1_options
            if opt.startswith("play:recycle_rune:")
            or opt.startswith("play:exhaust_and_recycle_rune:")
        ]
        self.assertEqual(recycle_opts, [])

    def test_recycle_exhausted_rune_grants_power_no_energy(self) -> None:
        engine, _ = self._drive_to_action_turn()
        pool = engine.runes_for(RequiredTo.PLAYER_1)
        pool.clear()
        pool.append(Rune(domain="Mind", exhausted=True))
        # Pre-seed Energy so we can assert it doesn't change.
        engine.add_energy(RequiredTo.PLAYER_1, 5)
        library = engine.rune_library_for(RequiredTo.PLAYER_1)
        lib_size_before = len(library)

        after = engine.apply_action(
            action="play:recycle_rune:0", actor=RequiredTo.PLAYER_1
        )
        # Rune left the pool and was appended to the rune library bottom,
        # reset to ready.
        self.assertEqual(len(engine.runes_for(RequiredTo.PLAYER_1)), 0)
        self.assertEqual(len(library), lib_size_before + 1)
        self.assertEqual(library[-1].domain, "Mind")
        self.assertFalse(library[-1].exhausted)
        # +1 Mind Power; Energy unchanged (the rune was already exhausted).
        self.assertEqual(after.game_state.player_1_power, {"Mind": 1})
        self.assertEqual(after.game_state.player_1_energy, 5)

    def test_recycle_rejects_ready_rune(self) -> None:
        engine, _ = self._drive_to_action_turn()
        pool = engine.runes_for(RequiredTo.PLAYER_1)
        pool.clear()
        pool.append(Rune(domain="Fury"))  # ready
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(action="play:recycle_rune:0", actor=RequiredTo.PLAYER_1)
        self.assertIn("ready, not exhausted", str(ctx.exception))

    def test_exhaust_and_recycle_grants_energy_and_power(self) -> None:
        engine, _ = self._drive_to_action_turn()
        pool = engine.runes_for(RequiredTo.PLAYER_1)
        pool.clear()
        pool.append(Rune(domain="Body"))  # single ready Body
        library = engine.rune_library_for(RequiredTo.PLAYER_1)
        lib_size_before = len(library)

        after = engine.apply_action(
            action="play:exhaust_and_recycle_rune:0", actor=RequiredTo.PLAYER_1
        )
        # Rune left the pool and is at the bottom of the library, reset to
        # ready so it comes back fresh when Channel pulls it next.
        self.assertEqual(len(engine.runes_for(RequiredTo.PLAYER_1)), 0)
        self.assertEqual(len(library), lib_size_before + 1)
        self.assertEqual(library[-1].domain, "Body")
        self.assertFalse(library[-1].exhausted)
        # +1 Energy (from the exhaust) and +1 Body Power (from the recycle).
        self.assertEqual(after.game_state.player_1_energy, 1)
        self.assertEqual(after.game_state.player_1_power, {"Body": 1})

    def test_exhaust_and_recycle_rejects_exhausted_rune(self) -> None:
        engine, _ = self._drive_to_action_turn()
        pool = engine.runes_for(RequiredTo.PLAYER_1)
        pool.clear()
        pool.append(Rune(domain="Mind", exhausted=True))
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(
                action="play:exhaust_and_recycle_rune:0", actor=RequiredTo.PLAYER_1
            )
        self.assertIn("already exhausted", str(ctx.exception))

    def test_recycle_clears_power_at_turn_end(self) -> None:
        engine, _ = self._drive_to_action_turn()
        pool = engine.runes_for(RequiredTo.PLAYER_1)
        pool.clear()
        pool.extend([Rune(domain="Mind", exhausted=True), Rune(domain="Body")])
        engine.apply_action(action="play:recycle_rune:0", actor=RequiredTo.PLAYER_1)
        engine.apply_action(
            action="play:exhaust_and_recycle_rune:0", actor=RequiredTo.PLAYER_1
        )
        # 2 Power accumulated (1 Mind, 1 Body), 1 Energy from the exhaust.
        self.assertEqual(
            engine.player_power(RequiredTo.PLAYER_1), {"Mind": 1, "Body": 1}
        )
        self.assertEqual(engine.player_energy(RequiredTo.PLAYER_1), 1)
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_1)
        # Turn change wipes both pools for both players.
        self.assertEqual(engine.player_power(RequiredTo.PLAYER_1), {})
        self.assertEqual(engine.player_power(RequiredTo.PLAYER_2), {})
        self.assertEqual(engine.player_energy(RequiredTo.PLAYER_1), 0)

    def test_inactive_player_cannot_recycle(self) -> None:
        engine, _ = self._drive_to_action_turn()
        pool2 = engine.runes_for(RequiredTo.PLAYER_2)
        pool2.clear()
        pool2.append(Rune(domain="Mind", exhausted=True))
        with self.assertRaises(ValueError):
            engine.apply_action(action="play:recycle_rune:0", actor=RequiredTo.PLAYER_2)

    def test_recycle_blocked_while_play_is_pending(self) -> None:
        engine, ready = self._drive_to_action_turn()
        # Set up pool: one exhausted Mind to recycle, plenty of ready Fury
        # to bank Energy first.
        pool = engine.runes_for(RequiredTo.PLAYER_1)
        pool.clear()
        pool.append(Rune(domain="Mind", exhausted=True))
        pool.extend(Rune(domain="Fury") for _ in range(20))
        ready = engine.start()
        hand = ready.game_state.player_1_hand or []
        target_idx = next(
            (
                i for i, card in enumerate(hand)
                if card_type_of(card) == "Unit"
                and (card_energy_of(card) or 0) > 0
                and (card_power_of(card) or 0) == 0
            ),
            None,
        )
        if target_idx is None:
            self.skipTest("dealt hand has no positive-Energy Power-0 Unit cards")
        cost = card_energy_of(hand[target_idx]) or 0
        # Bank just enough Energy from the Fury runes (indices 1..1+cost-1).
        for offset in range(cost):
            engine.apply_action(
                action=f"play:exhaust_rune:{1 + offset}", actor=RequiredTo.PLAYER_1
            )
        engine.apply_action(action=f"play:play_unit:{target_idx}", actor=RequiredTo.PLAYER_1)
        # pending_play is set → recycle is now blocked until choose_location.
        with self.assertRaises(ValueError):
            engine.apply_action(action="play:recycle_rune:0", actor=RequiredTo.PLAYER_1)
        with self.assertRaises(ValueError):
            engine.apply_action(
                action=f"play:exhaust_and_recycle_rune:{1 + cost}", actor=RequiredTo.PLAYER_1
            )

    def test_play_unit_options_only_appear_once_energy_is_banked(self) -> None:
        engine, ready = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        ready = engine.start()
        hand = ready.game_state.player_1_hand or []
        # Find a positive-Energy Unit with NO Power requirement so this
        # test isolates the Energy gate. Power gating is covered separately
        # in PlayUnitPowerCostTests.
        target_idx = next(
            (
                i for i, card in enumerate(hand)
                if card_type_of(card) == "Unit"
                and (card_energy_of(card) or 0) > 0
                and (card_power_of(card) or 0) == 0
            ),
            None,
        )
        if target_idx is None:
            self.skipTest("dealt hand has no positive-Energy Power-0 Unit cards")
        cost = card_energy_of(hand[target_idx]) or 0
        self.assertNotIn(f"play:play_unit:{target_idx}", ready.player_1_options)
        # Build exactly enough Energy and the option must now appear.
        for i in range(cost):
            engine.apply_action(action=f"play:exhaust_rune:{i}", actor=RequiredTo.PLAYER_1)
        after = engine.start()
        self.assertIn(f"play:play_unit:{target_idx}", after.player_1_options)


class PlayUnitPowerCostTests(unittest.TestCase):
    """Power cost gate: a Unit with positive Power requires the matching
    domain Power in the player's pool, on top of any Energy cost."""

    def _drive_to_action_turn(self) -> tuple["GameEngine", "EngineOutput"]:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)
        fifth = engine.apply_action(action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        engine.apply_action(action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:", actor=RequiredTo.PLAYER_1)
        ready = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:",
            actor=RequiredTo.PLAYER_2,
        )
        _ = third
        return engine, ready

    def _inject_power_unit(self, engine: GameEngine) -> tuple[int, str, int, int, tuple[str, ...]]:
        """Inject a single-domain Unit with both Energy and Power cost into P1's hand.

        Returns (hand_index, name, energy_cost, power_cost, domains). Picks
        the unit deterministically from the CSV catalog so the test doesn't
        depend on the random deal."""
        unit = next(
            (
                c for c in csv_cards()
                if c.card_type == "Unit"
                and "," not in c.domain
                and (card_energy_of(c.name) or 0) > 0
                and (card_power_of(c.name) or 0) > 0
            ),
            None,
        )
        if unit is None:
            raise unittest.SkipTest("no single-domain Unit with Energy+Power in CSV")
        gs = engine._game_state
        gs.player_1_hand = (gs.player_1_hand or []) + [unit.name]
        idx = len(gs.player_1_hand) - 1
        return (
            idx,
            unit.name,
            card_energy_of(unit.name) or 0,
            card_power_of(unit.name) or 0,
            card_domains_of(unit.name),
        )

    def test_play_unit_options_exclude_cards_without_enough_power(self) -> None:
        engine, _ = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        # Plenty of Energy banked, but no Power at all — cards with a
        # positive Power requirement must NOT appear as play options.
        engine.add_energy(RequiredTo.PLAYER_1, 99)
        target_idx, _, _, _, _ = self._inject_power_unit(engine)
        ready = engine.start()
        self.assertNotIn(f"play:play_unit:{target_idx}", ready.player_1_options)

    def test_play_unit_rejects_card_with_unmet_power_cost(self) -> None:
        engine, _ = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        engine.add_energy(RequiredTo.PLAYER_1, 99)
        target_idx, _, _, _, _ = self._inject_power_unit(engine)
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(
                action=f"play:play_unit:{target_idx}", actor=RequiredTo.PLAYER_1
            )
        self.assertIn("Power", str(ctx.exception))

    def test_play_unit_deducts_both_energy_and_power(self) -> None:
        engine, _ = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        target_idx, _, energy_cost, power_cost, domains = self._inject_power_unit(engine)

        # Bank EXACTLY the costs.
        engine.add_energy(RequiredTo.PLAYER_1, energy_cost)
        engine.add_power(RequiredTo.PLAYER_1, domains[0], power_cost)
        engine.apply_action(
            action=f"play:play_unit:{target_idx}", actor=RequiredTo.PLAYER_1
        )
        # Both pools fully drained.
        self.assertEqual(engine.player_energy(RequiredTo.PLAYER_1), 0)
        self.assertEqual(engine.player_power(RequiredTo.PLAYER_1), {})

    def test_play_unit_option_appears_once_power_is_built(self) -> None:
        engine, _ = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        target_idx, _, energy_cost, power_cost, domains = self._inject_power_unit(engine)
        engine.add_energy(RequiredTo.PLAYER_1, energy_cost)
        # Still missing Power — the play option must be absent.
        before = engine.start()
        self.assertNotIn(f"play:play_unit:{target_idx}", before.player_1_options)
        # Bank the Power and the option must now appear.
        engine.add_power(RequiredTo.PLAYER_1, domains[0], power_cost)
        after = engine.start()
        self.assertIn(f"play:play_unit:{target_idx}", after.player_1_options)

    def test_multi_domain_card_power_uses_any_listed_domain(self) -> None:
        engine, _ = self._drive_to_action_turn()
        # Pick a known multi-domain Unit from the CSV. Tibbers is
        # Fury+Chaos; Daisy! is Calm+Order. We try Tibbers first and fall
        # back so the test stays useful if the CSV changes.
        multi_unit = next(
            (
                c.name for c in csv_cards()
                if c.card_type == "Unit"
                and "," in c.domain
                and (card_power_of(c.name) or 0) > 0
            ),
            None,
        )
        if multi_unit is None:
            self.skipTest("no multi-domain Unit with Power cost in CSV")

        # Inject directly into P1's hand so we don't need a particular deal.
        gs = engine._game_state
        gs.player_1_hand = (gs.player_1_hand or []) + [multi_unit]
        target_idx = len(gs.player_1_hand) - 1
        domains = card_domains_of(multi_unit)
        energy_cost = card_energy_of(multi_unit) or 0
        power_cost = card_power_of(multi_unit) or 0
        self.assertGreaterEqual(len(domains), 2)
        self.assertGreater(power_cost, 0)

        # Bank Energy and split the Power across the listed domains:
        # 1 of each domain in order until the cost is met, then top up the
        # first domain with any leftover. For a 2-cost Fury+Chaos card this
        # ends up as 1 Fury + 1 Chaos — exercising the "any combination"
        # affordance.
        engine.add_energy(RequiredTo.PLAYER_1, energy_cost)
        remaining = power_cost
        for dom in domains:
            if remaining <= 0:
                break
            engine.add_power(RequiredTo.PLAYER_1, dom, 1)
            remaining -= 1
        if remaining > 0:
            engine.add_power(RequiredTo.PLAYER_1, domains[0], remaining)

        # The option should appear and the play should succeed.
        ready = engine.start()
        self.assertIn(f"play:play_unit:{target_idx}", ready.player_1_options)
        engine.apply_action(
            action=f"play:play_unit:{target_idx}", actor=RequiredTo.PLAYER_1
        )
        # All Power drained to 0.
        self.assertEqual(engine.player_power(RequiredTo.PLAYER_1), {})
        self.assertEqual(engine.player_energy(RequiredTo.PLAYER_1), 0)


class UnitSummoningSicknessTests(unittest.TestCase):
    """Played units enter exhausted and ready on the owner's next Awake."""

    def _drive_to_action_turn(self) -> tuple[GameEngine, "EngineOutput"]:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)
        fifth = engine.apply_action(action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        engine.apply_action(action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:", actor=RequiredTo.PLAYER_1)
        ready = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:",
            actor=RequiredTo.PLAYER_2,
        )
        _ = third
        return engine, ready

    def _play_one_unit_to_base(self, engine: GameEngine) -> str:
        """Play the first affordable Unit from P1's hand to base.

        New flow: exhaust runes to bank Energy first, then ``play_unit``
        (which deducts both Energy and any domain Power cost), then
        ``choose_location`` (no payment phase any more).
        """
        _flood_runes(engine, RequiredTo.PLAYER_1)
        ready = engine.start()
        hand = ready.game_state.player_1_hand or []
        idx = next(
            (i for i, c in enumerate(hand) if card_type_of(c) == "Unit"),
            None,
        )
        if idx is None:
            raise unittest.SkipTest("hand has no Unit cards")
        card = hand[idx]
        energy_cost = engine.card_energy_cost(card)
        # Exhaust ``cost`` ready runes to produce exactly that much Energy.
        for i in range(energy_cost):
            engine.apply_action(action=f"play:exhaust_rune:{i}", actor=RequiredTo.PLAYER_1)
        # If the card also requires Power, top up directly (these summoning-
        # sickness tests focus on unit lifecycle, not the recycle flow).
        power_cost = engine.card_power_cost(card)
        if power_cost > 0:
            domains = engine.card_domains(card)
            if not domains:
                raise unittest.SkipTest(f"{card!r} requires Power but has no domain")
            engine.add_power(RequiredTo.PLAYER_1, domains[0], power_cost)
        engine.apply_action(action=f"play:play_unit:{idx}", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action="play:choose_location:base", actor=RequiredTo.PLAYER_1)
        return card

    def test_newly_played_unit_enters_exhausted(self) -> None:
        engine, _ = self._drive_to_action_turn()
        self._play_one_unit_to_base(engine)
        units = engine.game_state.player_1_units
        self.assertEqual(len(units), 1)
        self.assertTrue(units[0].exhausted, "freshly played unit must enter exhausted")

    def test_unit_stays_exhausted_through_opponent_turn(self) -> None:
        engine, _ = self._drive_to_action_turn()
        self._play_one_unit_to_base(engine)
        # P1 ends turn → P2's full ABCD runs → control returns waiting on P2's action.
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_1)
        # P1's unit is unaffected by P2's Awake; it must still be exhausted.
        units = engine.game_state.player_1_units
        self.assertEqual(len(units), 1)
        self.assertTrue(
            units[0].exhausted,
            "opponent's Awake must not ready our units",
        )

    def test_unit_readies_on_owners_next_awake(self) -> None:
        engine, _ = self._drive_to_action_turn()
        self._play_one_unit_to_base(engine)
        # Cycle around to P1's next turn.
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_2)
        # Back to P1 — Awake ran, units should be ready.
        gs = engine.game_state
        self.assertEqual(gs.current_player, RequiredTo.PLAYER_1)
        self.assertEqual(len(gs.player_1_units), 1)
        self.assertFalse(
            gs.player_1_units[0].exhausted,
            "unit must ready on its owner's next Awake",
        )


class MoveUnitTests(unittest.TestCase):
    """play:move_unit — ready units may move base ↔ a controlled battlefield;
    moving exhausts the unit and battlefield-to-battlefield jumps are
    rejected."""

    def _drive_to_action_turn(self) -> tuple["GameEngine", "EngineOutput"]:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)
        fifth = engine.apply_action(action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        engine.apply_action(action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:", actor=RequiredTo.PLAYER_1)
        ready = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:",
            actor=RequiredTo.PLAYER_2,
        )
        _ = third
        return engine, ready

    def _place_ready_unit(
        self, engine: GameEngine, location: str, *, card: str = "Test Unit"
    ) -> int:
        """Append a ready (un-exhausted) PlayedUnit to P1's units list and
        return its index. Bypasses the play_unit flow so the test stays
        focused on movement mechanics."""
        from riftbound_engine.engine import PlayedUnit

        units = engine._game_state.player_1_units
        units.append(PlayedUnit(card=card, location=location, exhausted=False))
        return len(units) - 1

    def test_move_options_for_ready_unit_at_base_include_both_battlefields(self) -> None:
        # Both BFs always appear as move destinations from base regardless
        # of who controls them. The handler opens a showdown when the
        # destination isn't already self-controlled (see
        # test_move_unit_to_opponent_controlled_battlefield_opens_showdown).
        engine, _ = self._drive_to_action_turn()
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_1
        engine._game_state.battlefield_2_controller = RequiredTo.PLAYER_2
        unit_idx = self._place_ready_unit(engine, "base", card="Sentinel")
        ready = engine.start()
        move_opts = [opt for opt in ready.player_1_options if opt.startswith("play:move_unit:")]
        self.assertEqual(
            move_opts,
            [
                f"play:move_unit:{unit_idx}:battlefield_1",
                f"play:move_unit:{unit_idx}:battlefield_2",
            ],
        )

    def test_move_options_include_uncontrolled_battlefields(self) -> None:
        # Uncontrolled BFs are valid move destinations — picking one opens
        # a showdown. This is how players take control of a battlefield in
        # the first place (since play_unit still requires control).
        engine, _ = self._drive_to_action_turn()
        unit_idx = self._place_ready_unit(engine, "base", card="Sentinel")
        ready = engine.start()
        move_opts = [opt for opt in ready.player_1_options if opt.startswith("play:move_unit:")]
        self.assertEqual(
            move_opts,
            [
                f"play:move_unit:{unit_idx}:battlefield_1",
                f"play:move_unit:{unit_idx}:battlefield_2",
            ],
        )

    def test_move_options_include_opponent_controlled_battlefields(self) -> None:
        # Opponent-controlled BFs ARE valid move destinations — moving in
        # opens a showdown (the "invade" path). This is distinct from
        # play_unit, which still rejects opponent-controlled locations
        # because a freshly-deployed unit has no business landing there
        # without first transiting through base.
        engine, _ = self._drive_to_action_turn()
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_2
        # Leave BF2 uncontrolled so both kinds of "contested" destinations
        # show up in the same option list.
        unit_idx = self._place_ready_unit(engine, "base", card="Sentinel")
        ready = engine.start()
        move_opts = [opt for opt in ready.player_1_options if opt.startswith("play:move_unit:")]
        self.assertEqual(
            move_opts,
            [
                f"play:move_unit:{unit_idx}:battlefield_1",
                f"play:move_unit:{unit_idx}:battlefield_2",
            ],
        )

    def test_move_options_for_ready_unit_at_battlefield_offers_base_only(self) -> None:
        engine, _ = self._drive_to_action_turn()
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_1
        unit_idx = self._place_ready_unit(engine, "battlefield_1", card="Sentinel")
        ready = engine.start()
        move_opts = [opt for opt in ready.player_1_options if opt.startswith("play:move_unit:")]
        self.assertEqual(move_opts, [f"play:move_unit:{unit_idx}:base"])

    def test_exhausted_unit_has_no_move_options(self) -> None:
        engine, _ = self._drive_to_action_turn()
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_1
        unit_idx = self._place_ready_unit(engine, "base", card="Sentinel")
        engine._game_state.player_1_units[unit_idx].exhausted = True
        ready = engine.start()
        move_opts = [opt for opt in ready.player_1_options if opt.startswith("play:move_unit:")]
        self.assertEqual(move_opts, [])

    def test_move_unit_base_to_battlefield_exhausts_the_unit(self) -> None:
        engine, _ = self._drive_to_action_turn()
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_1
        unit_idx = self._place_ready_unit(engine, "base", card="Sentinel")
        after = engine.apply_action(
            action=f"play:move_unit:{unit_idx}:battlefield_1", actor=RequiredTo.PLAYER_1
        )
        unit = after.game_state.player_1_units[unit_idx]
        self.assertEqual(unit.location, "battlefield_1")
        self.assertTrue(unit.exhausted, "moving must exhaust the unit")

    def test_move_unit_battlefield_to_base_exhausts_the_unit(self) -> None:
        engine, _ = self._drive_to_action_turn()
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_1
        unit_idx = self._place_ready_unit(engine, "battlefield_1", card="Sentinel")
        after = engine.apply_action(
            action=f"play:move_unit:{unit_idx}:base", actor=RequiredTo.PLAYER_1
        )
        unit = after.game_state.player_1_units[unit_idx]
        self.assertEqual(unit.location, "base")
        self.assertTrue(unit.exhausted)

    def test_move_unit_rejects_battlefield_to_battlefield(self) -> None:
        engine, _ = self._drive_to_action_turn()
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_1
        engine._game_state.battlefield_2_controller = RequiredTo.PLAYER_1
        unit_idx = self._place_ready_unit(engine, "battlefield_1", card="Sentinel")
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(
                action=f"play:move_unit:{unit_idx}:battlefield_2", actor=RequiredTo.PLAYER_1
            )
        self.assertIn("battlefield-to-battlefield", str(ctx.exception))

    def test_move_unit_to_uncontrolled_battlefield_opens_showdown(self) -> None:
        # The legacy "uncontrolled BF is rejected" behavior has been replaced
        # by the showdown mechanic: moving to an uncontrolled battlefield is
        # now allowed and opens a PendingShowdown. The unit relocates and
        # exhausts, but the BF stays uncontrolled until the showdown
        # resolves via mutual pass_showdown (see ShowdownTests).
        engine, _ = self._drive_to_action_turn()
        unit_idx = self._place_ready_unit(engine, "base", card="Sentinel")
        after = engine.apply_action(
            action=f"play:move_unit:{unit_idx}:battlefield_1", actor=RequiredTo.PLAYER_1
        )
        unit = after.game_state.player_1_units[unit_idx]
        self.assertEqual(unit.location, "battlefield_1")
        self.assertTrue(unit.exhausted)
        self.assertIsNotNone(after.game_state.pending_showdown)
        self.assertEqual(after.game_state.pending_showdown.battlefield, "battlefield_1")
        self.assertEqual(after.game_state.pending_showdown.initiator, RequiredTo.PLAYER_1)
        # BF controller is still None until the showdown resolves.
        self.assertIsNone(after.game_state.battlefield_1_controller)

    def test_move_unit_to_opponent_controlled_battlefield_opens_showdown(self) -> None:
        # "Invade" path: moving a ready unit from base onto a battlefield
        # the opponent currently controls must succeed (the unit relocates
        # and exhausts) AND must open a PendingShowdown initiated by the
        # active player. Battlefield control stays unchanged until the
        # showdown actually resolves via mutual pass_showdown.
        engine, _ = self._drive_to_action_turn()
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_2
        unit_idx = self._place_ready_unit(engine, "base", card="Sentinel")
        after = engine.apply_action(
            action=f"play:move_unit:{unit_idx}:battlefield_1", actor=RequiredTo.PLAYER_1
        )
        unit = after.game_state.player_1_units[unit_idx]
        self.assertEqual(unit.location, "battlefield_1")
        self.assertTrue(unit.exhausted, "invading must exhaust the unit")
        self.assertIsNotNone(after.game_state.pending_showdown)
        self.assertEqual(
            after.game_state.pending_showdown.battlefield, "battlefield_1"
        )
        self.assertEqual(
            after.game_state.pending_showdown.initiator, RequiredTo.PLAYER_1
        )
        # Defender still holds the BF until the showdown resolves.
        self.assertEqual(
            after.game_state.battlefield_1_controller, RequiredTo.PLAYER_2
        )

    def test_move_unit_rejects_exhausted_unit(self) -> None:
        engine, _ = self._drive_to_action_turn()
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_1
        unit_idx = self._place_ready_unit(engine, "base", card="Sentinel")
        engine._game_state.player_1_units[unit_idx].exhausted = True
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(
                action=f"play:move_unit:{unit_idx}:battlefield_1", actor=RequiredTo.PLAYER_1
            )
        self.assertIn("exhausted", str(ctx.exception))

    def test_move_unit_rejects_same_location(self) -> None:
        engine, _ = self._drive_to_action_turn()
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_1
        unit_idx = self._place_ready_unit(engine, "base", card="Sentinel")
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(
                action=f"play:move_unit:{unit_idx}:base", actor=RequiredTo.PLAYER_1
            )
        self.assertIn("already at", str(ctx.exception))

    def test_move_unit_rejects_inactive_player(self) -> None:
        engine, _ = self._drive_to_action_turn()
        # P1 is active. Place a ready unit on P2's side and have P2 try to move.
        from riftbound_engine.engine import PlayedUnit
        engine._game_state.player_2_units.append(
            PlayedUnit(card="Sentinel", location="base", exhausted=False)
        )
        engine._game_state.battlefield_2_controller = RequiredTo.PLAYER_2
        with self.assertRaises(ValueError):
            engine.apply_action(
                action="play:move_unit:0:battlefield_2", actor=RequiredTo.PLAYER_2
            )

    def test_move_unit_blocked_during_showdown(self) -> None:
        engine, _ = self._drive_to_action_turn()
        # First, open a showdown by moving a Sentinel onto BF1.
        first_unit = self._place_ready_unit(engine, "base", card="Sentinel")
        engine.apply_action(
            action=f"play:move_unit:{first_unit}:battlefield_1", actor=RequiredTo.PLAYER_1
        )
        # Place a second ready unit at base and try to move it — must be blocked.
        second_unit = self._place_ready_unit(engine, "base", card="Backup")
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(
                action=f"play:move_unit:{second_unit}:battlefield_2", actor=RequiredTo.PLAYER_1
            )
        self.assertIn("showdown", str(ctx.exception))

    def test_move_unit_blocked_while_play_is_pending(self) -> None:
        engine, ready = self._drive_to_action_turn()
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_1
        _flood_runes(engine, RequiredTo.PLAYER_1)
        unit_idx = self._place_ready_unit(engine, "base", card="Sentinel")
        # Start a play to set pending_play. Find a Power-0 unit so this
        # test isolates the pending-play gate.
        ready = engine.start()
        hand = ready.game_state.player_1_hand or []
        target = next(
            (
                i for i, card in enumerate(hand)
                if card_type_of(card) == "Unit"
                and (card_energy_of(card) or 0) > 0
                and (card_power_of(card) or 0) == 0
            ),
            None,
        )
        if target is None:
            self.skipTest("dealt hand has no positive-Energy Power-0 Unit cards")
        cost = card_energy_of(hand[target]) or 0
        for i in range(cost):
            engine.apply_action(action=f"play:exhaust_rune:{i}", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action=f"play:play_unit:{target}", actor=RequiredTo.PLAYER_1)
        # pending_play is set → move_unit is now blocked.
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(
                action=f"play:move_unit:{unit_idx}:battlefield_1", actor=RequiredTo.PLAYER_1
            )
        self.assertIn("waiting for a location", str(ctx.exception))


class ShowdownTests(unittest.TestCase):
    """play:pass_showdown — both players pass in turn to resolve a showdown
    opened when a unit moves onto an uncontrolled battlefield."""

    def _drive_to_action_turn(self) -> tuple["GameEngine", "EngineOutput"]:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)
        fifth = engine.apply_action(action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        engine.apply_action(action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:", actor=RequiredTo.PLAYER_1)
        ready = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:",
            actor=RequiredTo.PLAYER_2,
        )
        _ = third
        return engine, ready

    def _start_showdown_on_bf1(self, engine: GameEngine) -> int:
        """Open a showdown on battlefield_1 with P1 as the initiator.

        Returns the index of the unit that walked onto the BF."""
        from riftbound_engine.engine import PlayedUnit

        engine._game_state.player_1_units.append(
            PlayedUnit(card="Sentinel", location="base", exhausted=False)
        )
        unit_idx = len(engine._game_state.player_1_units) - 1
        engine.apply_action(
            action=f"play:move_unit:{unit_idx}:battlefield_1", actor=RequiredTo.PLAYER_1
        )
        return unit_idx

    def test_showdown_initiator_can_muster_or_play_before_passing(self) -> None:
        engine, _ = self._drive_to_action_turn()
        self._start_showdown_on_bf1(engine)
        out = engine.start()
        # Opponent (P2) waits; the initiator (P1) is on the clock.
        self.assertEqual(out.player_2_options, [])
        self.assertEqual(out.required_action.actor, RequiredTo.PLAYER_1)
        # Pass is always available, and it's the LAST option (muster/play
        # options come first).
        self.assertIn("play:pass_showdown", out.player_1_options)
        self.assertEqual(out.player_1_options[-1], "play:pass_showdown")
        # Every non-pass option is one of: mustering a unit onto the
        # contested battlefield, tapping a rune (to bank Energy/Power for a
        # spell), or playing an Action/Reaction spell — never an end_turn or
        # other off-limits action.
        for opt in out.player_1_options[:-1]:
            is_muster = opt.startswith("play:move_unit:") and opt.endswith(
                ":battlefield_1"
            )
            is_play = opt.startswith("play:play_spell:")
            is_rune = (
                opt.startswith("play:exhaust_rune:")
                or opt.startswith("play:recycle_rune:")
                or opt.startswith("play:exhaust_and_recycle_rune:")
            )
            self.assertTrue(
                is_muster or is_play or is_rune, f"unexpected showdown option: {opt}"
            )

    def test_opponent_cannot_pass_first(self) -> None:
        engine, _ = self._drive_to_action_turn()
        self._start_showdown_on_bf1(engine)
        # Focus starts with the initiator (P1); P2 can't pass focus yet.
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_2)
        self.assertIn("focus", str(ctx.exception))

    def test_after_initiator_passes_focus_goes_to_opponent(self) -> None:
        engine, _ = self._drive_to_action_turn()
        self._start_showdown_on_bf1(engine)
        engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        out = engine.start()
        # Focus is now with P2: only P2 has options, and they can at least
        # pass focus (plus any Action/Reaction spells in hand).
        self.assertEqual(out.player_1_options, [])
        self.assertIn("play:pass_showdown", out.player_2_options)
        self.assertEqual(out.player_2_options[-1], "play:pass_showdown")
        self.assertEqual(out.required_action.actor, RequiredTo.PLAYER_2)
        # Showdown still pending; focus passed once to P2.
        sd = out.game_state.pending_showdown
        self.assertIsNotNone(sd)
        self.assertEqual(sd.focus_holder, RequiredTo.PLAYER_2)
        self.assertEqual(sd.focus_passes, 1)

    def test_both_passes_assign_control_to_initiator_and_clear_showdown(self) -> None:
        engine, _ = self._drive_to_action_turn()
        self._start_showdown_on_bf1(engine)
        engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        out = engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_2)
        # Showdown cleared; BF1 controller is now P1 (the initiator).
        self.assertIsNone(out.game_state.pending_showdown)
        self.assertEqual(out.game_state.battlefield_1_controller, RequiredTo.PLAYER_1)
        # Active player (P1) is back to normal action options now.
        self.assertEqual(out.required_action.actor, RequiredTo.PLAYER_1)
        self.assertIn("play:end_turn", out.player_1_options)

    def test_initiator_cannot_pass_twice(self) -> None:
        engine, _ = self._drive_to_action_turn()
        self._start_showdown_on_bf1(engine)
        # P1 passes focus → focus is now with P2; P1 can't pass again.
        engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        self.assertIn("focus", str(ctx.exception))

    def test_off_limits_actions_blocked_during_showdown(self) -> None:
        engine, _ = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        self._start_showdown_on_bf1(engine)
        # end_turn is blocked while a showdown is unresolved.
        with self.assertRaises(ValueError, msg="end_turn should be blocked"):
            engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_1)
        # But the FOCUS holder (the initiator here) MAY tap runes mid-showdown
        # to bank Energy/Power for a spell — that's no longer blocked.
        engine.apply_action(action="play:exhaust_rune:0", actor=RequiredTo.PLAYER_1)
        self.assertEqual(engine.player_energy(RequiredTo.PLAYER_1), 1)

    def test_pass_showdown_rejected_when_no_showdown(self) -> None:
        engine, _ = self._drive_to_action_turn()
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        self.assertIn("no showdown", str(ctx.exception))

    def test_showdown_win_awards_one_point_to_initiator(self) -> None:
        engine, _ = self._drive_to_action_turn()
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_1), 0)
        self._start_showdown_on_bf1(engine)
        engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_2)
        # Initiator scored 1 (the BF they just gained); opponent unaffected.
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_1), 1)
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_2), 0)

    def test_subsequent_move_onto_resolved_bf_does_not_open_new_showdown(self) -> None:
        engine, _ = self._drive_to_action_turn()
        self._start_showdown_on_bf1(engine)
        engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_2)
        # P1 now controls BF1. Place a fresh ready unit and move it there —
        # the BF is no longer uncontrolled so no new showdown should open.
        from riftbound_engine.engine import PlayedUnit
        engine._game_state.player_1_units.append(
            PlayedUnit(card="Reinforcement", location="base", exhausted=False)
        )
        idx = len(engine._game_state.player_1_units) - 1
        after = engine.apply_action(
            action=f"play:move_unit:{idx}:battlefield_1", actor=RequiredTo.PLAYER_1
        )
        self.assertIsNone(after.game_state.pending_showdown)
        self.assertEqual(after.game_state.player_1_units[idx].location, "battlefield_1")

    def test_invasion_into_opponent_bf_transfers_control_on_both_pass(self) -> None:
        """End-to-end: P2 holds BF1; P1 moves a unit there (opens
        showdown); both pass; P1 takes control of BF1 and scores 1 point.
        This is the "invade an opponent-held battlefield" path that used
        to be blocked outright at the move handler."""
        from riftbound_engine.engine import PlayedUnit

        engine, _ = self._drive_to_action_turn()
        engine._game_state.battlefield_1_controller = RequiredTo.PLAYER_2
        engine._game_state.player_1_units.append(
            PlayedUnit(card="Sentinel", location="base", exhausted=False)
        )
        idx = len(engine._game_state.player_1_units) - 1

        # Invade.
        after_move = engine.apply_action(
            action=f"play:move_unit:{idx}:battlefield_1", actor=RequiredTo.PLAYER_1
        )
        self.assertIsNotNone(after_move.game_state.pending_showdown)
        self.assertEqual(
            after_move.game_state.battlefield_1_controller, RequiredTo.PLAYER_2,
            "defender still holds the BF until the showdown resolves",
        )

        # Resolve.
        engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        out = engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_2)

        self.assertIsNone(out.game_state.pending_showdown)
        # Control flips to the initiator (placeholder showdown model).
        self.assertEqual(
            out.game_state.battlefield_1_controller, RequiredTo.PLAYER_1
        )
        # Initiator scored 1 for taking the BF.
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_1), 1)
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_2), 0)


class ScoringTests(unittest.TestCase):
    """Match score: +1 per battlefield gained via showdown, +1 per held BF
    at B (ABCD step B). Capped at 1 point per battlefield per turn."""

    def _drive_to_action_turn(self) -> tuple["GameEngine", "EngineOutput"]:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        third = engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)
        fifth = engine.apply_action(action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        engine.apply_action(action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:", actor=RequiredTo.PLAYER_1)
        ready = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:",
            actor=RequiredTo.PLAYER_2,
        )
        _ = third
        return engine, ready

    def _open_and_win_showdown(
        self, engine: GameEngine, *, battlefield: str = "battlefield_1"
    ) -> None:
        """Place a ready unit on P1's side, move it onto an uncontrolled BF,
        and resolve the showdown via mutual pass. P1 ends up controlling it."""
        from riftbound_engine.engine import PlayedUnit

        engine._game_state.player_1_units.append(
            PlayedUnit(card="Sentinel", location="base", exhausted=False)
        )
        unit_idx = len(engine._game_state.player_1_units) - 1
        engine.apply_action(
            action=f"play:move_unit:{unit_idx}:{battlefield}", actor=RequiredTo.PLAYER_1
        )
        engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action="play:pass_showdown", actor=RequiredTo.PLAYER_2)

    def test_starting_scores_are_zero(self) -> None:
        engine, _ = self._drive_to_action_turn()
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_1), 0)
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_2), 0)

    def test_showdown_win_awards_one_point(self) -> None:
        engine, _ = self._drive_to_action_turn()
        self._open_and_win_showdown(engine, battlefield="battlefield_1")
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_1), 1)
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_2), 0)

    def test_b_phase_hold_scores_one_per_held_battlefield(self) -> None:
        engine, _ = self._drive_to_action_turn()
        # P1 takes BF1 on turn 1 (showdown) → +1 point.
        self._open_and_win_showdown(engine, battlefield="battlefield_1")
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_1), 1)
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_1)
        # P2's turn: holds nothing → still 0.
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_2), 0)
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_2)
        # Back on P1: B-phase HOLD awards +1 for BF1 they still control.
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_1), 2)

    def test_b_phase_scores_both_held_battlefields(self) -> None:
        engine, _ = self._drive_to_action_turn()
        # P1 takes BF1 turn 1.
        self._open_and_win_showdown(engine, battlefield="battlefield_1")
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_2)
        # Back to P1: HOLD score for BF1 awarded → 2 total.
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_1), 2)
        # P1 takes BF2 this turn (showdown) → +1, total 3.
        self._open_and_win_showdown(engine, battlefield="battlefield_2")
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_1), 3)
        # End P1's turn, end P2's turn → back to P1, HOLD for BOTH BFs → +2.
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_2)
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_1), 5)

    def test_per_bf_cap_blocks_second_award_same_turn(self) -> None:
        engine, _ = self._drive_to_action_turn()
        # First award: succeeds.
        first = engine.award_bf_point(RequiredTo.PLAYER_1, "battlefield_1")
        self.assertTrue(first)
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_1), 1)
        # Second award for the same BF this turn: rejected.
        second = engine.award_bf_point(RequiredTo.PLAYER_1, "battlefield_1")
        self.assertFalse(second)
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_1), 1)

    def test_scored_bfs_clears_on_turn_change(self) -> None:
        engine, _ = self._drive_to_action_turn()
        self._open_and_win_showdown(engine, battlefield="battlefield_1")
        # Mid-turn the BF is in the per-turn set.
        self.assertIn("battlefield_1", engine._game_state.scored_bfs_this_turn)
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_1)
        # After the turn flip the set is cleared so the same BF can score
        # again next turn via HOLD.
        self.assertEqual(engine._game_state.scored_bfs_this_turn, set())

    def test_opponent_holds_score_for_their_own_battlefields(self) -> None:
        engine, _ = self._drive_to_action_turn()
        # Hand BF2 to P2 directly (no showdown — testing pure B-phase scoring).
        engine._game_state.battlefield_2_controller = RequiredTo.PLAYER_2
        # End P1's turn so P2's ABCD runs and B-phase HOLDs BF2 for P2.
        engine.apply_action(action="play:end_turn", actor=RequiredTo.PLAYER_1)
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_2), 1)
        self.assertEqual(engine.player_score(RequiredTo.PLAYER_1), 0)


class PlayGearTests(unittest.TestCase):
    """Gears pay like units (Energy + domain Power) but differ in play:
    they enter READY, commit straight to base with no location step, and
    are never offered a move. See action_turn/builtins.py::_play_gear."""

    # CSV catalog entry: Card Type=Gear, Energy 3, no Power requirement.
    GEAR_CARD = "Boots of Swiftness"
    # CSV catalog entry: Card Type=Spell — used to prove the type gate.
    SPELL_CARD = "Stacked Deck"

    def _drive_to_action_turn(self) -> tuple[GameEngine, "EngineOutput"]:
        rolls = iter([6, 2])
        engine = GameEngine(dice_roller=lambda: next(rolls))
        first = engine.start()
        second = engine.apply_action(action=f"choose_deck:{first.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action=f"choose_deck:{second.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        fourth = engine.apply_action(action="choose_first_turn:player_1", actor=RequiredTo.PLAYER_1)
        fifth = engine.apply_action(action=f"choose_battlefield_1:{fourth.player_1_options[0]}", actor=RequiredTo.PLAYER_1)
        engine.apply_action(action=f"choose_battlefield_2:{fifth.player_2_options[0]}", actor=RequiredTo.PLAYER_2)
        engine.apply_action(action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:", actor=RequiredTo.PLAYER_1)
        ready = engine.apply_action(
            action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:",
            actor=RequiredTo.PLAYER_2,
        )
        return engine, ready

    def test_gear_card_is_recognized_as_gear_type(self) -> None:
        self.assertEqual(card_type_of(self.GEAR_CARD), "Gear")
        self.assertEqual(card_type_of(self.SPELL_CARD), "Spell")

    def test_play_gear_enters_ready_at_base_with_no_location_step(self) -> None:
        engine, _ = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        _bank_all_resources(engine, RequiredTo.PLAYER_1, 99)
        # Put a known Gear at the front of the hand so the index is stable.
        engine._game_state.player_1_hand = [self.GEAR_CARD] + list(
            engine._game_state.player_1_hand or []
        )
        energy_before = engine.player_energy(RequiredTo.PLAYER_1)
        cost = engine.card_energy_cost(self.GEAR_CARD)

        out = engine.apply_action(action="play:play_gear:0", actor=RequiredTo.PLAYER_1)

        # No location follow-up: unlike units, a gear never parks in pending_play.
        self.assertIsNone(out.game_state.pending_play)
        self.assertNotIn(
            "play:choose_location:base",
            out.player_1_options,
            "a gear must not open a location prompt",
        )
        # It lands in the gears list at base, READY, and not among units.
        gears = out.game_state.player_1_gears
        self.assertEqual(len(gears), 1)
        self.assertEqual(gears[0].card, self.GEAR_CARD)
        self.assertEqual(gears[0].location, "base")
        self.assertFalse(gears[0].exhausted, "gears enter ready, not exhausted")
        self.assertEqual(out.game_state.player_1_units, [])
        # The gear was consumed from hand and both costs were charged.
        self.assertEqual(out.game_state.player_1_hand.count(self.GEAR_CARD), 0)
        self.assertEqual(out.game_state.player_1_energy, energy_before - cost)

    def test_gear_is_offered_as_a_play_option(self) -> None:
        engine, _ = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        _bank_all_resources(engine, RequiredTo.PLAYER_1, 99)
        engine._game_state.player_1_hand = [self.GEAR_CARD] + list(
            engine._game_state.player_1_hand or []
        )
        out = engine.start()
        self.assertIn("play:play_gear:0", out.player_1_options)

    def test_gear_is_never_offered_a_move(self) -> None:
        engine, _ = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        _bank_all_resources(engine, RequiredTo.PLAYER_1, 99)
        engine._game_state.player_1_hand = [self.GEAR_CARD]
        out = engine.apply_action(action="play:play_gear:0", actor=RequiredTo.PLAYER_1)
        # The only thing on the board is the ready gear at base; if gears were
        # move-eligible like a ready base unit, base→battlefield moves would
        # appear here. They must not.
        self.assertFalse(
            any(o.startswith("play:move_unit:") for o in out.player_1_options),
            f"gears must never be offered a move, got: {out.player_1_options}",
        )

    def test_play_gear_rejects_a_non_gear_card(self) -> None:
        engine, _ = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        _bank_all_resources(engine, RequiredTo.PLAYER_1, 99)
        engine._game_state.player_1_hand = [self.SPELL_CARD] + list(
            engine._game_state.player_1_hand or []
        )
        with self.assertRaises(ValueError) as ctx:
            engine.apply_action(action="play:play_gear:0", actor=RequiredTo.PLAYER_1)
        self.assertIn("cannot be played as a gear", str(ctx.exception))

    def test_ready_all_gears_clears_a_later_exhaustion(self) -> None:
        # Gears enter ready, but a future tap-style ability could exhaust one.
        # Awake (step A) must re-ready it, mirroring units.
        engine, _ = self._drive_to_action_turn()
        _flood_runes(engine, RequiredTo.PLAYER_1)
        _bank_all_resources(engine, RequiredTo.PLAYER_1, 99)
        engine._game_state.player_1_hand = [self.GEAR_CARD]
        engine.apply_action(action="play:play_gear:0", actor=RequiredTo.PLAYER_1)
        engine._game_state.player_1_gears[0].exhausted = True
        engine._ready_all_gears(RequiredTo.PLAYER_1)
        self.assertFalse(engine._game_state.player_1_gears[0].exhausted)