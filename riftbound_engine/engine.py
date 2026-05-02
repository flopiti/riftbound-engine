from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .decks import HARDCODED_DECKS


DECK_BATTLEFIELD_COUNT = 3
DECK_CHOSEN_CHAMPION_COUNT = 1
DECK_LEGEND_COUNT = 1
DECK_CARD_COUNT = 39
DECK_RUNE_COUNT = 12
MULLIGAN_DRAW_COUNT = 4
MULLIGAN_MAX_BOTTOM = 2


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
    current_player: RequiredTo = field(default_factory=lambda: RequiredTo.BOTH)
    started: bool = False
    player_1_deck: Deck | None = None
    player_2_deck: Deck | None = None
    player_1_deck_id: str | None = None
    player_2_deck_id: str | None = None


class RequiredTo(str, Enum):
    PLAYER_1 = "player_1"
    PLAYER_2 = "player_2"
    BOTH = "both"


@dataclass
class RequiredAction:
    actor: RequiredTo
    name: str


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
            current_player=self._game_state.current_player,
            started=self._game_state.started,
            player_1_deck=self._game_state.player_1_deck,
            player_2_deck=self._game_state.player_2_deck,
            player_1_deck_id=self._game_state.player_1_deck_id,
            player_2_deck_id=self._game_state.player_2_deck_id,
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

    def _finalize_setup_after_mulligan(self) -> None:
        self._game_state.is_mulligan_done = True
        self._game_state.player_1_mulligan_hand = None
        self._game_state.player_2_mulligan_hand = None
        self._game_state.started = True
        self._game_state.current_player = self._game_state.first_turn
        if self._can_increment():
            self._game_state.counter += 1

    def _deck_selection_output(self) -> EngineOutput | None:
        available_decks = list(HARDCODED_DECKS.keys())
        if self._game_state.player_1_deck is None:
            return EngineOutput(
                game_state=self.game_state,
                required_action=RequiredAction(actor=RequiredTo.PLAYER_1, name="choose_deck"),
                player_1_options=available_decks,
            )
        if self._game_state.player_2_deck is None:
            return EngineOutput(
                game_state=self.game_state,
                required_action=RequiredAction(actor=RequiredTo.PLAYER_2, name="choose_deck"),
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
                    required_action=RequiredAction(
                        actor=self._game_state.first_turn_choice or RequiredTo.PLAYER_1,
                        name="choose_first_turn",
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
                    required_action=RequiredAction(
                        actor=RequiredTo.BOTH,
                        name="choose_battlefields",
                    ),
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
                        required_action=RequiredAction(actor=RequiredTo.BOTH, name="choose_mulligan"),
                    )
                self._finalize_setup_after_mulligan()

        return EngineOutput(
            game_state=self.game_state,
            required_action=None,
        )

    def apply_action(self, action: str, actor: RequiredTo) -> EngineOutput:
        if action.startswith("choose_deck:"):
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

        if action.startswith("choose_first_turn:"):
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

        if action.startswith("choose_battlefield_1:"):
            if actor != RequiredTo.PLAYER_1:
                raise ValueError("player_1 must choose battlefield_1")
            if self._game_state.player_1_deck is None:
                raise ValueError("player_1 deck must be selected first")
            value = action.split(":", 1)[1].strip()
            if value not in self._game_state.player_1_deck.battlefields:
                raise ValueError("battlefield_1 must be from player_1 deck battlefields")
            self._game_state.battlefield_1 = value
            return self.start()

        if action.startswith("choose_battlefield_2:"):
            if actor != RequiredTo.PLAYER_2:
                raise ValueError("player_2 must choose battlefield_2")
            if self._game_state.player_2_deck is None:
                raise ValueError("player_2 deck must be selected first")
            value = action.split(":", 1)[1].strip()
            if value not in self._game_state.player_2_deck.battlefields:
                raise ValueError("battlefield_2 must be from player_2 deck battlefields")
            self._game_state.battlefield_2 = value
            return self.start()

        if action.startswith("mulligan_resolve:"):
            remainder = action.removeprefix("mulligan_resolve:")
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

        raise ValueError("no gameplay actions are implemented yet")
