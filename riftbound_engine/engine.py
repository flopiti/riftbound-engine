from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .decks import HARDCODED_DECKS
from .protocol import ApplyVerb, RequiredStep, apply_prefix


DECK_BATTLEFIELD_COUNT = 3
DECK_CHOSEN_CHAMPION_COUNT = 1
DECK_LEGEND_COUNT = 1
DECK_CARD_COUNT = 39
DECK_RUNE_COUNT = 12
MULLIGAN_DRAW_COUNT = 4
MULLIGAN_MAX_BOTTOM = 2
# Channel (ABCD — C): across the whole match, 1st channel = 2 runes, 2nd channel = 3, then 2 forever.
CHANNEL_RUNES_FIRST_IN_GAME = 2
CHANNEL_RUNES_SECOND_IN_GAME = 3
CHANNEL_RUNES_AFTER = 2


@dataclass(frozen=True)
class Rune:
    domain: str


@dataclass(frozen=True)
class Deck:
    battlefields: list[str]
    chosen_champion: str
    legend: str
    cards: tuple[str, ...]
    runes: tuple[Rune, ...]

    def __post_init__(self) -> None:
        if len(self.battlefields) != DECK_BATTLEFIELD_COUNT:
            raise ValueError(f"deck must contain {DECK_BATTLEFIELD_COUNT} battlefields")
        if not self.chosen_champion:
            raise ValueError("deck must contain a chosen champion")
        if not self.legend:
            raise ValueError("deck must contain a legend")
        if len(self.cards) != DECK_CARD_COUNT:
            raise ValueError(f"deck must contain {DECK_CARD_COUNT} cards")
        card_counts = Counter(self.cards)
        if any(count > 3 for count in card_counts.values()):
            raise ValueError("deck cards may not have more than 3 duplicates")
        if len(self.runes) != DECK_RUNE_COUNT:
            raise ValueError(f"deck must contain {DECK_RUNE_COUNT} runes")
        for rune in self.runes:
            if not rune.domain:
                raise ValueError("each rune must have a domain")


def build_default_deck(player_name: str) -> Deck:
    return Deck(
        battlefields=[f"{player_name} Battlefield {index}" for index in range(1, DECK_BATTLEFIELD_COUNT + 1)],
        chosen_champion=f"{player_name} Champion",
        legend=f"{player_name} Legend",
        cards=tuple(f"{player_name} Card {index}" for index in range(1, DECK_CARD_COUNT + 1)),
        runes=tuple(Rune(domain="Neutral") for _ in range(DECK_RUNE_COUNT)),
    )


def build_deck_from_id(deck_id: str) -> Deck:
    if deck_id not in HARDCODED_DECKS:
        raise ValueError(f"unknown deck id '{deck_id}'")
    deck_data = HARDCODED_DECKS[deck_id]
    runes_raw = deck_data["runes"]
    return Deck(
        battlefields=list(deck_data["battlefields"]),
        chosen_champion=str(deck_data["chosen_champion"]),
        legend=str(deck_data["legend"]),
        cards=tuple(deck_data["cards"]),
        runes=tuple(Rune(domain=str(r["domain"])) for r in runes_raw),
    )


@dataclass
class GameState:
    counter: int = 0
    first_turn_choice: RequiredTo | None = None
    first_turn: RequiredTo | None = None
    battlefield_1: str | None = None
    battlefield_2: str | None = None
    is_mulligan_done: bool = False
    player_1_library: list[str] | None = None
    player_2_library: list[str] | None = None
    player_1_mulligan_hand: list[str] | None = None
    player_2_mulligan_hand: list[str] | None = None
    mulligan_player_1_resolved: bool = False
    mulligan_player_2_resolved: bool = False
    player_1_hand: list[str] | None = None
    player_2_hand: list[str] | None = None
    player_1_runes: list[Rune] = field(default_factory=list)
    player_2_runes: list[Rune] = field(default_factory=list)
    #: Shuffled rune deck (remaining); draws from the front. Set when the game starts after mulligan.
    player_1_rune_library: list[Rune] | None = None
    player_2_rune_library: list[Rune] | None = None
    player_1_base: str | None = None
    player_2_base: str | None = None
    current_player: RequiredTo = field(default_factory=lambda: RequiredTo.BOTH)
    started: bool = False
    player_1_deck: Deck | None = None
    player_2_deck: Deck | None = None
    player_1_deck_id: str | None = None
    player_2_deck_id: str | None = None
    #: Counts each time a player begins their turn (first active turn after setup = 1).
    total_turn_number: int = 0
    #: How many turns this player has started (e.g. 5 = that player's 5th turn).
    player_1_turn_number: int = 0
    player_2_turn_number: int = 0
    abcd_a_done: bool = False
    abcd_b_done: bool = False
    abcd_c_done: bool = False
    abcd_d_done: bool = False
    #: How many Channel (C) steps have completed this match (both players; orders the 2 → 3 → 2… schedule).
    global_channel_count: int = 0


class RequiredTo(str, Enum):
    PLAYER_1 = "player_1"
    PLAYER_2 = "player_2"
    BOTH = "both"


def is_abcd_done(game_state: GameState) -> bool:
    return (
        game_state.abcd_a_done
        and game_state.abcd_b_done
        and game_state.abcd_c_done
        and game_state.abcd_d_done
    )


@dataclass
class RequiredAction:
    actor: RequiredTo
    name: str


def _required_action(actor: RequiredTo, step: RequiredStep) -> RequiredAction:
    """Store stable string names for clients (matches `RequiredStep` values)."""
    return RequiredAction(actor=actor, name=step.value)


@dataclass
class EngineOutput:
    game_state: GameState
    player_1_options: list[str] = field(default_factory=list)
    player_2_options: list[str] = field(default_factory=list)
    required_action: RequiredAction | None = None


class GameEngine:
    def __init__(
        self,
        game_state: GameState | None = None,
        max_counter: int | None = None,
        dice_roller: Callable[[], int] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._game_state = game_state or GameState()
        self._max_counter = max_counter
        self._dice_roller = dice_roller or (lambda: random.randint(1, 6))
        self._rng = rng or random.Random()

    @property
    def game_state(self) -> GameState:
        return GameState(
            counter=self._game_state.counter,
            first_turn_choice=self._game_state.first_turn_choice,
            first_turn=self._game_state.first_turn,
            battlefield_1=self._game_state.battlefield_1,
            battlefield_2=self._game_state.battlefield_2,
            is_mulligan_done=self._game_state.is_mulligan_done,
            player_1_library=list(self._game_state.player_1_library) if self._game_state.player_1_library else None,
            player_2_library=list(self._game_state.player_2_library) if self._game_state.player_2_library else None,
            player_1_mulligan_hand=list(self._game_state.player_1_mulligan_hand)
            if self._game_state.player_1_mulligan_hand
            else None,
            player_2_mulligan_hand=list(self._game_state.player_2_mulligan_hand)
            if self._game_state.player_2_mulligan_hand
            else None,
            mulligan_player_1_resolved=self._game_state.mulligan_player_1_resolved,
            mulligan_player_2_resolved=self._game_state.mulligan_player_2_resolved,
            player_1_hand=list(self._game_state.player_1_hand) if self._game_state.player_1_hand is not None else None,
            player_2_hand=list(self._game_state.player_2_hand) if self._game_state.player_2_hand is not None else None,
            player_1_runes=list(self._game_state.player_1_runes),
            player_2_runes=list(self._game_state.player_2_runes),
            player_1_rune_library=list(self._game_state.player_1_rune_library)
            if self._game_state.player_1_rune_library is not None
            else None,
            player_2_rune_library=list(self._game_state.player_2_rune_library)
            if self._game_state.player_2_rune_library is not None
            else None,
            player_1_base=self._game_state.player_1_base,
            player_2_base=self._game_state.player_2_base,
            current_player=self._game_state.current_player,
            started=self._game_state.started,
            player_1_deck=self._game_state.player_1_deck,
            player_2_deck=self._game_state.player_2_deck,
            player_1_deck_id=self._game_state.player_1_deck_id,
            player_2_deck_id=self._game_state.player_2_deck_id,
            total_turn_number=self._game_state.total_turn_number,
            player_1_turn_number=self._game_state.player_1_turn_number,
            player_2_turn_number=self._game_state.player_2_turn_number,
            abcd_a_done=self._game_state.abcd_a_done,
            abcd_b_done=self._game_state.abcd_b_done,
            abcd_c_done=self._game_state.abcd_c_done,
            abcd_d_done=self._game_state.abcd_d_done,
            global_channel_count=self._game_state.global_channel_count,
        )

    def _can_increment(self) -> bool:
        return self._max_counter is None or self._game_state.counter < self._max_counter

    def _resolve_first_turn_choice(self) -> RequiredTo:
        while True:
            player_1_roll = self._dice_roller()
            player_2_roll = self._dice_roller()
            if player_1_roll == player_2_roll:
                continue
            return RequiredTo.PLAYER_1 if player_1_roll > player_2_roll else RequiredTo.PLAYER_2

    def _prepare_mulligan_draws(self) -> None:
        if self._game_state.player_1_deck is None or self._game_state.player_2_deck is None:
            return
        if self._game_state.player_1_library is not None:
            return
        lib1 = list(self._game_state.player_1_deck.cards)
        lib2 = list(self._game_state.player_2_deck.cards)
        self._rng.shuffle(lib1)
        self._rng.shuffle(lib2)
        self._game_state.player_1_mulligan_hand = lib1[:MULLIGAN_DRAW_COUNT]
        self._game_state.player_2_mulligan_hand = lib2[:MULLIGAN_DRAW_COUNT]
        self._game_state.player_1_library = lib1[MULLIGAN_DRAW_COUNT:]
        self._game_state.player_2_library = lib2[MULLIGAN_DRAW_COUNT:]

    @staticmethod
    def _parse_mulligan_bottom_indices(payload: str) -> list[int]:
        payload = payload.strip()
        if not payload:
            return []
        indices: list[int] = []
        for part in re.split(r"[,\s]+", payload):
            part = part.strip()
            if not part:
                continue
            indices.append(int(part))
        if len(indices) > MULLIGAN_MAX_BOTTOM:
            raise ValueError(f"at most {MULLIGAN_MAX_BOTTOM} cards may be placed on the bottom")
        if len(set(indices)) != len(indices):
            raise ValueError("bottom indices must be distinct")
        for index in indices:
            if index not in range(MULLIGAN_DRAW_COUNT):
                raise ValueError("mulligan indices must be 0..3")
        return indices

    @staticmethod
    def _mulligan_hand_and_library(
        hand: list[str], rest: list[str], bottom_indices: list[int]
    ) -> tuple[list[str], list[str]]:
        """Kept cards stay in hand; each card sent to the bottom is replaced by a draw from the library."""
        bottom_set = set(bottom_indices)
        kept = [hand[index] for index in range(MULLIGAN_DRAW_COUNT) if index not in bottom_set]
        to_bottom = [hand[index] for index in sorted(bottom_indices)]
        replacement_count = len(to_bottom)
        if replacement_count > len(rest):
            raise ValueError("not enough cards in library to replace mulliganed cards")
        replacements = rest[:replacement_count]
        new_rest = rest[replacement_count:]
        new_hand = kept + replacements
        new_library = new_rest + to_bottom
        if len(new_hand) != MULLIGAN_DRAW_COUNT:
            raise ValueError("internal error: mulligan hand must stay at 4 cards")
        return new_hand, new_library

    def _reset_abcd_flags(self) -> None:
        self._game_state.abcd_a_done = False
        self._game_state.abcd_b_done = False
        self._game_state.abcd_c_done = False
        self._game_state.abcd_d_done = False

    def _finalize_setup_after_mulligan(self) -> None:
        self._game_state.is_mulligan_done = True
        self._game_state.player_1_mulligan_hand = None
        self._game_state.player_2_mulligan_hand = None
        self._game_state.started = True
        ft = self._game_state.first_turn
        if ft not in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
            raise ValueError("first_turn must be player_1 or player_2 before completing setup")
        self._game_state.current_player = ft
        self._game_state.total_turn_number = 1
        if ft == RequiredTo.PLAYER_1:
            self._game_state.player_1_turn_number = 1
            self._game_state.player_2_turn_number = 0
        else:
            self._game_state.player_2_turn_number = 1
            self._game_state.player_1_turn_number = 0
        self._reset_abcd_flags()
        if self._game_state.player_1_deck is not None:
            r1 = list(self._game_state.player_1_deck.runes)
            self._rng.shuffle(r1)
            self._game_state.player_1_rune_library = r1
        if self._game_state.player_2_deck is not None:
            r2 = list(self._game_state.player_2_deck.runes)
            self._rng.shuffle(r2)
            self._game_state.player_2_rune_library = r2
        if self._can_increment():
            self._game_state.counter += 1

    def _advance_turn(self) -> None:
        """Switch active player, bump turn counters, reset ABCD and action-turn flags for the new turn."""
        cp = self._game_state.current_player
        if cp not in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
            raise ValueError("current_player must be player_1 or player_2 during play")
        nxt = RequiredTo.PLAYER_2 if cp == RequiredTo.PLAYER_1 else RequiredTo.PLAYER_1
        self._game_state.current_player = nxt
        self._game_state.total_turn_number += 1
        if nxt == RequiredTo.PLAYER_1:
            self._game_state.player_1_turn_number += 1
        else:
            self._game_state.player_2_turn_number += 1
        self._reset_abcd_flags()

    def _channel_rune_count_for_next_channel(self) -> int:
        k = self._game_state.global_channel_count
        if k == 0:
            return CHANNEL_RUNES_FIRST_IN_GAME
        if k == 1:
            return CHANNEL_RUNES_SECOND_IN_GAME
        return CHANNEL_RUNES_AFTER

    def _execute_channel(self, actor: RequiredTo) -> None:
        """Channel (C): draw runes from the rune deck into the player's rune pool.

        Requests a scheduled count; if fewer remain, channels all remaining (e.g. wanted 2 but 1
        left → channel 1; none left → channel none). Always completes the Channel step.
        """
        if actor == RequiredTo.PLAYER_1:
            pile = self._game_state.player_1_rune_library
            pool = self._game_state.player_1_runes
        elif actor == RequiredTo.PLAYER_2:
            pile = self._game_state.player_2_rune_library
            pool = self._game_state.player_2_runes
        else:
            raise ValueError("channel requires player_1 or player_2")
        if pile is None:
            raise ValueError("rune deck is not initialized")
        requested = self._channel_rune_count_for_next_channel()
        n = min(requested, len(pile))
        for _ in range(n):
            pool.append(pile.pop(0))
        self._game_state.global_channel_count += 1

    def _execute_draw(self, actor: RequiredTo) -> None:
        """Draw (D): draw one card from the main deck (library) into hand."""
        if actor == RequiredTo.PLAYER_1:
            library = self._game_state.player_1_library
            hand = self._game_state.player_1_hand
        elif actor == RequiredTo.PLAYER_2:
            library = self._game_state.player_2_library
            hand = self._game_state.player_2_hand
        else:
            raise ValueError("draw requires player_1 or player_2")
        if library is None or hand is None:
            raise ValueError("library or hand is not initialized")
        if not library:
            raise ValueError("cannot draw: main deck is empty")
        hand.append(library.pop(0))

    def _apply_abcd_letter(self, letter: str, actor: RequiredTo) -> None:
        gs = self._game_state
        if actor != gs.current_player:
            raise ValueError("only the active player may advance ABCD")
        key = letter.strip().lower()
        if key == "a":
            if gs.abcd_a_done:
                raise ValueError("A already completed this turn")
            gs.abcd_a_done = True
        elif key == "b":
            if not gs.abcd_a_done:
                raise ValueError("A must be completed before B")
            if gs.abcd_b_done:
                raise ValueError("B already completed this turn")
            gs.abcd_b_done = True
        elif key == "c":
            if not gs.abcd_b_done:
                raise ValueError("B must be completed before C")
            if gs.abcd_c_done:
                raise ValueError("C already completed this turn")
            self._execute_channel(actor)
            gs.abcd_c_done = True
        elif key == "d":
            if not gs.abcd_c_done:
                raise ValueError("C must be completed before D")
            if gs.abcd_d_done:
                raise ValueError("D already completed this turn")
            self._execute_draw(actor)
            gs.abcd_d_done = True
        else:
            raise ValueError("letter must be a, b, c, or d")

    def _complete_abcd_for_current_player(self) -> None:
        """Run A→B→C→D for the active player in one shot (no separate client steps)."""
        gs = self._game_state
        actor = gs.current_player
        if actor not in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
            raise ValueError("ABCD requires active player_1 or player_2")
        for letter in ("a", "b", "c", "d"):
            self._apply_abcd_letter(letter, actor)

    def _deck_selection_output(self) -> EngineOutput | None:
        available_decks = list(HARDCODED_DECKS.keys())
        if self._game_state.player_1_deck is None:
            return EngineOutput(
                game_state=self.game_state,
                required_action=_required_action(RequiredTo.PLAYER_1, RequiredStep.CHOOSE_DECK),
                player_1_options=available_decks,
            )
        if self._game_state.player_2_deck is None:
            return EngineOutput(
                game_state=self.game_state,
                required_action=_required_action(RequiredTo.PLAYER_2, RequiredStep.CHOOSE_DECK),
                player_2_options=available_decks,
            )
        return None

    def start(self, required_to: RequiredTo = RequiredTo.BOTH) -> EngineOutput:
        deck_selection = self._deck_selection_output()
        if deck_selection is not None:
            return deck_selection

        if not self._game_state.started:
            if self._game_state.first_turn_choice is None:
                self._game_state.first_turn_choice = self._resolve_first_turn_choice()
            if self._game_state.first_turn is None:
                return EngineOutput(
                    game_state=self.game_state,
                    required_action=_required_action(
                        self._game_state.first_turn_choice or RequiredTo.PLAYER_1,
                        RequiredStep.CHOOSE_FIRST_TURN,
                    ),
                )
            if self._game_state.battlefield_1 is None or self._game_state.battlefield_2 is None:
                return EngineOutput(
                    game_state=self.game_state,
                    player_1_options=(
                        list(self._game_state.player_1_deck.battlefields) if self._game_state.battlefield_1 is None else []
                    ),
                    player_2_options=(
                        list(self._game_state.player_2_deck.battlefields) if self._game_state.battlefield_2 is None else []
                    ),
                    required_action=_required_action(RequiredTo.BOTH, RequiredStep.CHOOSE_BATTLEFIELDS),
                )
            if not self._game_state.is_mulligan_done:
                self._prepare_mulligan_draws()
                if not (
                    self._game_state.mulligan_player_1_resolved and self._game_state.mulligan_player_2_resolved
                ):
                    return EngineOutput(
                        game_state=self.game_state,
                        player_1_options=list(self._game_state.player_1_mulligan_hand or []),
                        player_2_options=list(self._game_state.player_2_mulligan_hand or []),
                        required_action=_required_action(RequiredTo.BOTH, RequiredStep.CHOOSE_MULLIGAN),
                    )
                self._finalize_setup_after_mulligan()

        if self._game_state.started:
            if not is_abcd_done(self._game_state):
                self._complete_abcd_for_current_player()
                return self.start()
            return EngineOutput(
                game_state=self.game_state,
                required_action=_required_action(self._game_state.current_player, RequiredStep.ACTION_TURN),
            )

        return EngineOutput(
            game_state=self.game_state,
            required_action=None,
        )

    def apply_action(self, action: str, actor: RequiredTo) -> EngineOutput:
        action = action.strip()
        if not action:
            raise ValueError("action must not be empty")

        if action.startswith(apply_prefix(ApplyVerb.CHOOSE_DECK)):
            deck_selection = self._deck_selection_output()
            if deck_selection is None:
                raise ValueError("decks are already selected for both players")
            if actor != deck_selection.required_action.actor:
                raise ValueError(f"{deck_selection.required_action.actor.value} must choose a deck first")
            deck_id = action.split(":", 1)[1]
            selected_deck = build_deck_from_id(deck_id)
            if actor == RequiredTo.PLAYER_1:
                self._game_state.player_1_deck = selected_deck
                self._game_state.player_1_deck_id = deck_id
            elif actor == RequiredTo.PLAYER_2:
                self._game_state.player_2_deck = selected_deck
                self._game_state.player_2_deck_id = deck_id
            else:
                raise ValueError("deck selection requires a specific player")
            return self.start()

        if action.startswith(apply_prefix(ApplyVerb.CHOOSE_FIRST_TURN)):
            if self._game_state.first_turn_choice is None:
                raise ValueError("first_turn_choice is not resolved yet")
            if actor != self._game_state.first_turn_choice:
                raise ValueError(f"{self._game_state.first_turn_choice.value} must choose first turn")
            first_turn_value = action.split(":", 1)[1].strip().lower()
            if first_turn_value not in (RequiredTo.PLAYER_1.value, RequiredTo.PLAYER_2.value):
                raise ValueError("first_turn must be player_1 or player_2")
            self._game_state.first_turn = (
                RequiredTo.PLAYER_1 if first_turn_value == RequiredTo.PLAYER_1.value else RequiredTo.PLAYER_2
            )
            return self.start()

        if action.startswith(apply_prefix(ApplyVerb.CHOOSE_BATTLEFIELD_1)):
            if actor != RequiredTo.PLAYER_1:
                raise ValueError("player_1 must choose battlefield_1")
            if self._game_state.player_1_deck is None:
                raise ValueError("player_1 deck must be selected first")
            value = action.split(":", 1)[1].strip()
            if value not in self._game_state.player_1_deck.battlefields:
                raise ValueError("battlefield_1 must be from player_1 deck battlefields")
            self._game_state.battlefield_1 = value
            self._game_state.player_1_base = value
            return self.start()

        if action.startswith(apply_prefix(ApplyVerb.CHOOSE_BATTLEFIELD_2)):
            if actor != RequiredTo.PLAYER_2:
                raise ValueError("player_2 must choose battlefield_2")
            if self._game_state.player_2_deck is None:
                raise ValueError("player_2 deck must be selected first")
            value = action.split(":", 1)[1].strip()
            if value not in self._game_state.player_2_deck.battlefields:
                raise ValueError("battlefield_2 must be from player_2 deck battlefields")
            self._game_state.battlefield_2 = value
            self._game_state.player_2_base = value
            return self.start()

        if action.startswith(apply_prefix(ApplyVerb.MULLIGAN_RESOLVE)):
            remainder = action.removeprefix(apply_prefix(ApplyVerb.MULLIGAN_RESOLVE))
            parts = remainder.split(":", 1)
            if len(parts) != 2:
                raise ValueError("mulligan_resolve requires mulligan_resolve:player_N:indices")
            who_raw, indices_raw = parts
            who = who_raw.strip().lower()
            bottom_indices = self._parse_mulligan_bottom_indices(indices_raw)
            if who == RequiredTo.PLAYER_1.value:
                if actor != RequiredTo.PLAYER_1:
                    raise ValueError("player_1 must resolve their own mulligan")
                if self._game_state.mulligan_player_1_resolved:
                    raise ValueError("player_1 mulligan already resolved")
                hand = self._game_state.player_1_mulligan_hand
                rest = self._game_state.player_1_library
                if hand is None or rest is None:
                    raise ValueError("mulligan is not active for player_1")
                new_hand, new_library = self._mulligan_hand_and_library(hand, rest, bottom_indices)
                self._game_state.player_1_hand = new_hand
                self._game_state.player_1_library = new_library
                self._game_state.player_1_mulligan_hand = None
                self._game_state.mulligan_player_1_resolved = True
            elif who == RequiredTo.PLAYER_2.value:
                if actor != RequiredTo.PLAYER_2:
                    raise ValueError("player_2 must resolve their own mulligan")
                if self._game_state.mulligan_player_2_resolved:
                    raise ValueError("player_2 mulligan already resolved")
                hand = self._game_state.player_2_mulligan_hand
                rest = self._game_state.player_2_library
                if hand is None or rest is None:
                    raise ValueError("mulligan is not active for player_2")
                new_hand, new_library = self._mulligan_hand_and_library(hand, rest, bottom_indices)
                self._game_state.player_2_hand = new_hand
                self._game_state.player_2_library = new_library
                self._game_state.player_2_mulligan_hand = None
                self._game_state.mulligan_player_2_resolved = True
            else:
                raise ValueError("mulligan_resolve who must be player_1 or player_2")
            return self.start()

        if action.startswith(apply_prefix(ApplyVerb.ABCD)):
            if not self._game_state.started:
                raise ValueError("game has not started yet")
            letter = action.split(":", 1)[1]
            self._apply_abcd_letter(letter, actor)
            return self.start()

        if action.startswith(apply_prefix(ApplyVerb.PLAY)):
            from .action_turn.registry import dispatch_turn_play

            dispatch_turn_play(self, actor, action)
            return self.start()

        raise ValueError("unknown action")
