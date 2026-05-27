from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .csv_data import card_domains_of, card_energy_of, card_power_of, card_type_of
from .deck_files import deck_data_for_id, list_deck_ids
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


@dataclass
class Rune:
    """A rune in a player's rune pool.

    A rune is ``ready`` (face up / vertical) until it's spent to pay an Energy
    cost, at which point it becomes ``exhausted`` (tapped / horizontal). All
    of the active player's exhausted runes ready again during step A (Awake)
    of their next turn.

    Note: this dataclass is intentionally **mutable** (not frozen) so the
    engine can flip `exhausted` in place. Equality is still by value.
    """

    domain: str
    exhausted: bool = False


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
    available = list_deck_ids()
    if deck_id not in available:
        raise ValueError(f"unknown deck id '{deck_id}' (available: {', '.join(available)})")
    deck_data = deck_data_for_id(deck_id)
    runes_raw = deck_data["runes"]
    return Deck(
        battlefields=list(deck_data["battlefields"]),
        chosen_champion=str(deck_data["chosen_champion"]),
        legend=str(deck_data["legend"]),
        cards=tuple(deck_data["cards"]),
        runes=tuple(Rune(domain=str(r["domain"])) for r in runes_raw),
    )


#: Legal destinations for a played unit. The engine accepts only these strings.
UNIT_LOCATIONS: tuple[str, ...] = ("base", "battlefield_1", "battlefield_2")


@dataclass
class PlayedUnit:
    """A card that has been played as a unit and committed to a location.

    Newly played units enter ``exhausted=True`` (summoning sickness), and
    are readied along with the player's runes during step A (Awake) of the
    owner's next turn. Other gameplay effects may also exhaust a unit later
    in the match.
    """

    card: str
    #: One of UNIT_LOCATIONS.
    location: str
    exhausted: bool = True


@dataclass
class PlayedSpell:
    """A Spell-type card that has been played by paying its Energy/Power cost.

    Unlike Units, Spells don't go to a location — they just go to the
    player's spell stack. The UI renders them off to the right side of the
    screen. The engine currently has no resolution/effect step for spells:
    the card simply sits in ``player_X_spells`` for the rest of the match.
    """

    card: str


@dataclass
class PendingPlay:
    """A play that has been started but is waiting for the active player to pick a location.

    The card has already been removed from `hand` when this is set — it lives
    in pending_play until choose_location commits it to units (or is rolled
    back, which we don't support yet).
    """

    actor: RequiredTo
    card: str


@dataclass
class PendingShowdown:
    """An active showdown over an uncontrolled battlefield.

    Opened when the active player moves a unit onto a battlefield with no
    controller. While set, both players' options collapse to a single
    ``play:pass_showdown`` action — first the initiator passes, then the
    opponent. When both have passed the showdown resolves: the battlefield's
    controller is set to whoever still has units remaining on it (in the
    current minimal model that's just the initiator), and this field is
    cleared.
    """

    battlefield: str  # "battlefield_1" or "battlefield_2"
    initiator: RequiredTo
    initiator_passed: bool = False
    opponent_passed: bool = False


@dataclass
class PendingPayment:
    """A unit has been committed to a location and is now waiting for the
    active player to pick which ready runes to exhaust to pay its Energy cost.

    Wire format: each pick is ``play:exhaust_rune:<index>`` where ``index`` is
    the position of the rune in the active player's rune pool. Picks must be
    of currently ready runes (already-exhausted runes are rejected).

    When ``remaining`` reaches 0 the payment is complete and the field is
    cleared, returning the player to normal action-turn options.
    """

    actor: RequiredTo
    remaining: int


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
    #: Cards played as units this match, in play order, each tagged with its location.
    player_1_units: list[PlayedUnit] = field(default_factory=list)
    player_2_units: list[PlayedUnit] = field(default_factory=list)
    #: Cards played as spells this match, in play order. Spells have no
    #: location — the UI renders them on the right side of the screen.
    #: The engine doesn't resolve spell effects yet; the card just sits here.
    player_1_spells: list[PlayedSpell] = field(default_factory=list)
    player_2_spells: list[PlayedSpell] = field(default_factory=list)
    #: Set while a `play:play_unit:*` is waiting for `play:choose_location:*`.
    pending_play: PendingPlay | None = None
    #: Set after a play has been committed to a location but the active player
    #: still owes ``remaining`` Energy in exhausted runes (cost > 0). Cleared
    #: when the last rune is exhausted.
    pending_payment: PendingPayment | None = None
    #: Set when the active player moves a unit onto an uncontrolled battlefield.
    #: While set, both players' options are limited to ``play:pass_showdown`` —
    #: first the initiator passes, then the opponent. See PendingShowdown.
    pending_showdown: PendingShowdown | None = None
    #: Who controls each contested territory. None ⇒ uncontrolled (nobody may
    #: play units there yet). Each player always controls their own `base`,
    #: which is not tracked here.
    battlefield_1_controller: RequiredTo | None = None
    battlefield_2_controller: RequiredTo | None = None
    player_1_runes: list[Rune] = field(default_factory=list)
    player_2_runes: list[Rune] = field(default_factory=list)
    #: Energy a player has produced this turn by exhausting runes. Each
    #: ``play:exhaust_rune:*`` adds 1; each ``play:play_unit:*`` deducts the
    #: card's Energy cost. The pool is reset to 0 when the player's turn
    #: ends (it does NOT persist across turns).
    player_1_energy: int = 0
    player_2_energy: int = 0
    #: Power a player has produced this turn by recycling runes — keyed by
    #: rune ``domain`` (e.g. {"Mind": 1, "Fury": 2}). Each ``play:recycle_rune:*``
    #: (already-exhausted rune) and ``play:exhaust_and_recycle_rune:*`` (ready
    #: rune, also producing 1 Energy) adds 1 of the rune's domain. Like
    #: Energy, Power resets at turn end — it does NOT persist across turns.
    player_1_power: dict[str, int] = field(default_factory=dict)
    player_2_power: dict[str, int] = field(default_factory=dict)
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
    #: Match score per player. Players score by gaining control of a
    #: battlefield (winning a showdown) and by HOLDing a battlefield at
    #: the start of B (ABCD step B). The per-turn cap is enforced by
    #: ``scored_bfs_this_turn`` below: at most one point per battlefield
    #: per turn, regardless of the source.
    player_1_score: int = 0
    player_2_score: int = 0
    #: Battlefields that have already awarded a point this turn (e.g. via
    #: B-phase HOLD or a showdown win). Cleared on ``_advance_turn`` so
    #: each new turn starts fresh.
    scored_bfs_this_turn: set[str] = field(default_factory=set)
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
            player_1_units=[
                PlayedUnit(card=u.card, location=u.location, exhausted=u.exhausted)
                for u in self._game_state.player_1_units
            ],
            player_2_units=[
                PlayedUnit(card=u.card, location=u.location, exhausted=u.exhausted)
                for u in self._game_state.player_2_units
            ],
            player_1_spells=[
                PlayedSpell(card=s.card) for s in self._game_state.player_1_spells
            ],
            player_2_spells=[
                PlayedSpell(card=s.card) for s in self._game_state.player_2_spells
            ],
            pending_play=(
                None
                if self._game_state.pending_play is None
                else PendingPlay(
                    actor=self._game_state.pending_play.actor,
                    card=self._game_state.pending_play.card,
                )
            ),
            pending_payment=(
                None
                if self._game_state.pending_payment is None
                else PendingPayment(
                    actor=self._game_state.pending_payment.actor,
                    remaining=self._game_state.pending_payment.remaining,
                )
            ),
            pending_showdown=(
                None
                if self._game_state.pending_showdown is None
                else PendingShowdown(
                    battlefield=self._game_state.pending_showdown.battlefield,
                    initiator=self._game_state.pending_showdown.initiator,
                    initiator_passed=self._game_state.pending_showdown.initiator_passed,
                    opponent_passed=self._game_state.pending_showdown.opponent_passed,
                )
            ),
            battlefield_1_controller=self._game_state.battlefield_1_controller,
            battlefield_2_controller=self._game_state.battlefield_2_controller,
            # Deep-copy Rune instances — Rune is mutable so exhausted-state
            # mutations on the engine's runes must not bleed into snapshots.
            player_1_runes=[Rune(domain=r.domain, exhausted=r.exhausted) for r in self._game_state.player_1_runes],
            player_2_runes=[Rune(domain=r.domain, exhausted=r.exhausted) for r in self._game_state.player_2_runes],
            player_1_energy=self._game_state.player_1_energy,
            player_2_energy=self._game_state.player_2_energy,
            player_1_power=dict(self._game_state.player_1_power),
            player_2_power=dict(self._game_state.player_2_power),
            player_1_score=self._game_state.player_1_score,
            player_2_score=self._game_state.player_2_score,
            scored_bfs_this_turn=set(self._game_state.scored_bfs_this_turn),
            player_1_rune_library=(
                [Rune(domain=r.domain, exhausted=r.exhausted) for r in self._game_state.player_1_rune_library]
                if self._game_state.player_1_rune_library is not None
                else None
            ),
            player_2_rune_library=(
                [Rune(domain=r.domain, exhausted=r.exhausted) for r in self._game_state.player_2_rune_library]
                if self._game_state.player_2_rune_library is not None
                else None
            ),
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
            # Fresh Rune instances so later mutations (exhausted/ready)
            # don't leak into the deck tuple, which other snapshots reference.
            r1 = [Rune(domain=r.domain) for r in self._game_state.player_1_deck.runes]
            self._rng.shuffle(r1)
            self._game_state.player_1_rune_library = r1
        if self._game_state.player_2_deck is not None:
            r2 = [Rune(domain=r.domain) for r in self._game_state.player_2_deck.runes]
            self._rng.shuffle(r2)
            self._game_state.player_2_rune_library = r2
        if self._can_increment():
            self._game_state.counter += 1

    def _advance_turn(self) -> None:
        """Switch active player, bump turn counters, reset ABCD and action-turn flags for the new turn."""
        cp = self._game_state.current_player
        if cp not in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
            raise ValueError("current_player must be player_1 or player_2 during play")
        if self._game_state.pending_play is not None:
            raise ValueError("cannot end the turn while a play is waiting for a location")
        if self._game_state.pending_showdown is not None:
            raise ValueError("cannot end the turn while a showdown is in progress")
        # Energy and Power are per-turn: clear both players' pools so nothing
        # carries into the next turn.
        self._game_state.player_1_energy = 0
        self._game_state.player_2_energy = 0
        self._game_state.player_1_power = {}
        self._game_state.player_2_power = {}
        # The per-BF scoring cap is also per-turn: clear the set so each
        # battlefield can score again on the new turn (if conditions hold).
        self._game_state.scored_bfs_this_turn = set()
        nxt = RequiredTo.PLAYER_2 if cp == RequiredTo.PLAYER_1 else RequiredTo.PLAYER_1
        self._game_state.current_player = nxt
        self._game_state.total_turn_number += 1
        if nxt == RequiredTo.PLAYER_1:
            self._game_state.player_1_turn_number += 1
        else:
            self._game_state.player_2_turn_number += 1
        self._reset_abcd_flags()

    @staticmethod
    def opponent_of(actor: RequiredTo) -> RequiredTo:
        """The other player in a head-to-head match (raises for ``BOTH``)."""
        if actor == RequiredTo.PLAYER_1:
            return RequiredTo.PLAYER_2
        if actor == RequiredTo.PLAYER_2:
            return RequiredTo.PLAYER_1
        raise ValueError("opponent_of requires player_1 or player_2")

    def _locations_controlled_by(self, actor: RequiredTo) -> list[str]:
        """Locations where `actor` may currently place a unit.

        - Each player always controls their own `base`.
        - A battlefield is available only if `<bf>_controller == actor`.
          When no one controls a battlefield (the default at match start),
          it is unavailable to both players.
        """
        out: list[str] = ["base"]
        if self._game_state.battlefield_1_controller == actor:
            out.append("battlefield_1")
        if self._game_state.battlefield_2_controller == actor:
            out.append("battlefield_2")
        return out

    def player_controls_location(self, actor: RequiredTo, location: str) -> bool:
        """Public predicate used by the choose_location handler."""
        return location in self._locations_controlled_by(actor)

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

    def runes_for(self, actor: RequiredTo) -> list[Rune]:
        """Active rune pool for ``actor`` (the live list — caller may mutate)."""
        if actor == RequiredTo.PLAYER_1:
            return self._game_state.player_1_runes
        if actor == RequiredTo.PLAYER_2:
            return self._game_state.player_2_runes
        raise ValueError("runes_for requires player_1 or player_2")

    def ready_rune_count(self, actor: RequiredTo) -> int:
        """How many of ``actor``'s runes are currently ready (not exhausted)."""
        return sum(1 for r in self.runes_for(actor) if not r.exhausted)

    def exhausted_rune_count(self, actor: RequiredTo) -> int:
        """How many of ``actor``'s runes are currently exhausted (tapped)."""
        return sum(1 for r in self.runes_for(actor) if r.exhausted)

    def player_energy(self, actor: RequiredTo) -> int:
        """Energy ``actor`` has produced this turn (cleared on turn change)."""
        if actor == RequiredTo.PLAYER_1:
            return self._game_state.player_1_energy
        if actor == RequiredTo.PLAYER_2:
            return self._game_state.player_2_energy
        raise ValueError("player_energy requires player_1 or player_2")

    def add_energy(self, actor: RequiredTo, amount: int) -> None:
        """Add ``amount`` to ``actor``'s energy pool (amount may be negative to spend)."""
        if actor == RequiredTo.PLAYER_1:
            new_value = self._game_state.player_1_energy + amount
            if new_value < 0:
                raise ValueError("energy pool cannot go negative")
            self._game_state.player_1_energy = new_value
        elif actor == RequiredTo.PLAYER_2:
            new_value = self._game_state.player_2_energy + amount
            if new_value < 0:
                raise ValueError("energy pool cannot go negative")
            self._game_state.player_2_energy = new_value
        else:
            raise ValueError("add_energy requires player_1 or player_2")

    def player_score(self, actor: RequiredTo) -> int:
        """Match score for ``actor``."""
        if actor == RequiredTo.PLAYER_1:
            return self._game_state.player_1_score
        if actor == RequiredTo.PLAYER_2:
            return self._game_state.player_2_score
        raise ValueError("player_score requires player_1 or player_2")

    def add_score(self, actor: RequiredTo, amount: int) -> None:
        """Add ``amount`` to ``actor``'s match score."""
        if actor == RequiredTo.PLAYER_1:
            self._game_state.player_1_score += amount
        elif actor == RequiredTo.PLAYER_2:
            self._game_state.player_2_score += amount
        else:
            raise ValueError("add_score requires player_1 or player_2")

    def award_bf_point(self, actor: RequiredTo, battlefield: str) -> bool:
        """Award ``actor`` 1 point for ``battlefield`` if it hasn't already
        scored this turn. Returns ``True`` when a point was awarded, ``False``
        when the per-BF-per-turn cap suppressed it.

        Used by both B-phase HOLD scoring and showdown wins so the cap is
        enforced uniformly regardless of source.
        """
        if battlefield in self._game_state.scored_bfs_this_turn:
            return False
        self._game_state.scored_bfs_this_turn.add(battlefield)
        self.add_score(actor, 1)
        return True

    def player_power(self, actor: RequiredTo) -> dict[str, int]:
        """Power ``actor`` has produced this turn, keyed by rune domain (live dict)."""
        if actor == RequiredTo.PLAYER_1:
            return self._game_state.player_1_power
        if actor == RequiredTo.PLAYER_2:
            return self._game_state.player_2_power
        raise ValueError("player_power requires player_1 or player_2")

    def add_power(self, actor: RequiredTo, domain: str, amount: int = 1) -> None:
        """Add ``amount`` to ``actor``'s power pool for ``domain``."""
        if not domain:
            raise ValueError("domain must not be empty")
        pool = self.player_power(actor)
        new_value = pool.get(domain, 0) + amount
        if new_value < 0:
            raise ValueError(f"power pool for {domain!r} cannot go negative")
        if new_value == 0:
            pool.pop(domain, None)
        else:
            pool[domain] = new_value

    def rune_library_for(self, actor: RequiredTo) -> list[Rune]:
        """Live rune library list for ``actor`` (recycling appends to the bottom)."""
        if actor == RequiredTo.PLAYER_1:
            lib = self._game_state.player_1_rune_library
        elif actor == RequiredTo.PLAYER_2:
            lib = self._game_state.player_2_rune_library
        else:
            raise ValueError("rune_library_for requires player_1 or player_2")
        if lib is None:
            raise ValueError("rune library is not initialized yet")
        return lib

    def card_energy_cost(self, card: str) -> int:
        """Energy cost for ``card`` from CSV, falling back to 0 when the CSV has
        no numeric Energy for that name (e.g. test fixtures, unknown cards).

        Returning 0 on missing data keeps tests and unknown-card paths working;
        anything in the actual CSV has an integer cost.
        """
        value = card_energy_of(card)
        return value if value is not None else 0

    def card_power_cost(self, card: str) -> int:
        """Power cost for ``card`` from CSV (0 when blank/missing).

        Power is paid out of the active player's Power pool. The card's
        Domain field determines which Power domains may be used to satisfy
        the cost — see :meth:`card_domains` and :meth:`can_afford_power_cost`.
        """
        value = card_power_of(card)
        return value if value is not None else 0

    def card_domains(self, card: str) -> tuple[str, ...]:
        """Domains the card belongs to (e.g. ``("Fury",)`` or ``("Fury", "Chaos")``)."""
        return card_domains_of(card)

    def can_afford_power_cost(self, actor: RequiredTo, card: str) -> bool:
        """True iff ``actor`` has enough Power across the card's listed domains.

        - Power cost 0 → always affordable (this method just returns True).
        - Single-domain card → needs ``cost`` Power of that exact domain.
        - Multi-domain card (e.g. "Fury, Chaos") → Power may come from any
          combination of the listed domains; we just sum the available
          Power across those domains and compare to the cost.
        - Card with positive Power cost but no known domain (e.g. unknown
          fixture) → unplayable.
        """
        cost = self.card_power_cost(card)
        if cost <= 0:
            return True
        domains = self.card_domains(card)
        if not domains:
            return False
        pool = self.player_power(actor)
        available = sum(pool.get(d, 0) for d in domains)
        return available >= cost

    def _deduct_power_cost(self, actor: RequiredTo, card: str) -> None:
        """Consume the card's Power cost from ``actor``'s Power pool.

        Greedy: spends Power in the order the card's domains are listed in
        the CSV. For a single-domain card this just drains that domain. For
        a multi-domain card it drains the first listed domain dry, then the
        second, and so on. Caller must verify affordability with
        :meth:`can_afford_power_cost` first — this raises if it runs out.
        """
        remaining = self.card_power_cost(card)
        if remaining <= 0:
            return
        pool = self.player_power(actor)
        for domain in self.card_domains(card):
            if remaining <= 0:
                break
            have = pool.get(domain, 0)
            if have <= 0:
                continue
            take = min(have, remaining)
            self.add_power(actor, domain, -take)
            remaining -= take
        if remaining > 0:
            raise ValueError(
                f"internal error: still {remaining} Power short after deducting from "
                f"{', '.join(self.card_domains(card)) or '<no domains>'}"
            )

    def _ready_all_runes(self, actor: RequiredTo) -> None:
        """Step A (Awake): flip every exhausted rune in the active player's pool back to ready."""
        if actor == RequiredTo.PLAYER_1:
            pool = self._game_state.player_1_runes
        elif actor == RequiredTo.PLAYER_2:
            pool = self._game_state.player_2_runes
        else:
            return
        for rune in pool:
            rune.exhausted = False

    def _ready_all_units(self, actor: RequiredTo) -> None:
        """Step A (Awake): flip every exhausted unit owned by ``actor`` back to ready.

        Units enter the battlefield exhausted (summoning sickness) and ready
        here on the owner's next turn. Effects that exhaust a unit later in
        a match are likewise cleared here.
        """
        if actor == RequiredTo.PLAYER_1:
            units = self._game_state.player_1_units
        elif actor == RequiredTo.PLAYER_2:
            units = self._game_state.player_2_units
        else:
            return
        for unit in units:
            unit.exhausted = False

    def _apply_abcd_letter(self, letter: str, actor: RequiredTo) -> None:
        gs = self._game_state
        if actor != gs.current_player:
            raise ValueError("only the active player may advance ABCD")
        key = letter.strip().lower()
        if key == "a":
            if gs.abcd_a_done:
                raise ValueError("A already completed this turn")
            # A = Awake: ready all the active player's exhausted runes and units.
            self._ready_all_runes(actor)
            self._ready_all_units(actor)
            gs.abcd_a_done = True
        elif key == "b":
            if not gs.abcd_a_done:
                raise ValueError("A must be completed before B")
            if gs.abcd_b_done:
                raise ValueError("B already completed this turn")
            # HOLD scoring: at the start of B, score 1 point per battlefield
            # the active player currently controls. The per-BF-per-turn cap
            # is enforced via award_bf_point — same cap applies if the same
            # battlefield later changes hands and scores via showdown.
            if gs.battlefield_1_controller == actor:
                self.award_bf_point(actor, "battlefield_1")
            if gs.battlefield_2_controller == actor:
                self.award_bf_point(actor, "battlefield_2")
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
        available_decks = list(list_deck_ids())
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
            active = self._game_state.current_player
            showdown = self._game_state.pending_showdown
            if showdown is not None:
                # Mid-showdown: collapse both players' menus to a single
                # pass_showdown for whichever side is next on the clock
                # (initiator first, then opponent). All other actions are
                # suppressed until the showdown resolves.
                if not showdown.initiator_passed:
                    next_actor = showdown.initiator
                else:
                    next_actor = self.opponent_of(showdown.initiator)
                return EngineOutput(
                    game_state=self.game_state,
                    player_1_options=(
                        ["play:pass_showdown"] if next_actor == RequiredTo.PLAYER_1 else []
                    ),
                    player_2_options=(
                        ["play:pass_showdown"] if next_actor == RequiredTo.PLAYER_2 else []
                    ),
                    required_action=_required_action(next_actor, RequiredStep.ACTION_TURN),
                )

            pending = self._game_state.pending_play
            if pending is not None and pending.actor == active:
                # Mid-play: the active player must pick a location before doing
                # anything else. End turn and other plays are suppressed.
                # Only the locations the active player controls are offered —
                # base is always self-controlled, battlefields only when their
                # controller matches the active player.
                options = [
                    f"play:choose_location:{loc}"
                    for loc in self._locations_controlled_by(active)
                ]
            else:
                active_hand = (
                    self._game_state.player_1_hand
                    if active == RequiredTo.PLAYER_1
                    else self._game_state.player_2_hand
                )
                # Option order:
                #  1) ``play:play_unit:<i>`` for each Unit in hand whose
                #     Energy and domain-Power costs are ≤ the player's
                #     current pools.
                #  2) ``play:play_spell:<i>`` for each Spell in hand under
                #     the same cost gates.
                #  3) ``play:move_unit:<i>:<dest>`` for movement (below).
                #  4) ``play:end_turn``.
                #
                # Standalone rune actions (``play:exhaust_rune:*``,
                # ``play:recycle_rune:*``, ``play:exhaust_and_recycle_rune:*``)
                # are intentionally NOT surfaced here — they only make
                # sense as the rune-payment part of playing a costed card,
                # so the snapshot's `player_X_intents` field carries
                # complete payment plans (computed server-side in
                # shortcuts.py) and the branch view's two-tier picker
                # turns one card chip into the right chain of rune
                # actions. The underlying action handlers stay registered
                # so /branch/forward can apply them as parts of a chain.
                #
                # Duplicate hand entries (same card name at multiple indices)
                # collapse to a single option pointing at the leftmost copy:
                # only one action can be taken at a time, so emitting
                # ``play:play_unit:0`` and ``play:play_unit:3`` for the same
                # card is noise.
                options = []

                # Only Unit-type cards can be played via play_unit (see
                # action_turn/builtins.py::_play_unit). Non-units stay in hand
                # and are simply not offered as play options. Affordability is
                # gated by BOTH the player's current Energy pool AND domain
                # Power pool — the handler repeats both checks as last-line
                # defense for clients that bypass the options list.
                energy = self.player_energy(active)
                seen_play_card: set[str] = set()
                for i, card in enumerate(active_hand or []):
                    if card in seen_play_card:
                        continue
                    if card_type_of(card) != "Unit":
                        continue
                    if self.card_energy_cost(card) > energy:
                        continue
                    if not self.can_afford_power_cost(active, card):
                        continue
                    seen_play_card.add(card)
                    options.append(f"play:play_unit:{i}")

                # Spells are gated by the same Energy + domain Power cost
                # gates as units. Unlike units they don't go to a location;
                # the handler places them directly into ``player_X_spells``.
                # See action_turn/builtins.py::_play_spell.
                for i, card in enumerate(active_hand or []):
                    if card in seen_play_card:
                        continue
                    if card_type_of(card) != "Spell":
                        continue
                    if self.card_energy_cost(card) > energy:
                        continue
                    if not self.can_afford_power_cost(active, card):
                        continue
                    seen_play_card.add(card)
                    options.append(f"play:play_spell:{i}")

                # Movement: each READY unit owned by the active player can
                # move base ↔ a battlefield, as long as the destination
                # isn't controlled by the opponent. Moving onto an
                # uncontrolled battlefield opens a showdown (see
                # PendingShowdown); moving onto our own controlled
                # battlefield just relocates. BF ↔ BF is not allowed.
                # Moving always exhausts the unit — see
                # action_turn/builtins.py::_move_unit.
                active_units = (
                    self._game_state.player_1_units
                    if active == RequiredTo.PLAYER_1
                    else self._game_state.player_2_units
                )
                bf_controllers = {
                    "battlefield_1": self._game_state.battlefield_1_controller,
                    "battlefield_2": self._game_state.battlefield_2_controller,
                }
                for unit_idx, unit in enumerate(active_units):
                    if unit.exhausted:
                        continue
                    if unit.location == "base":
                        # Base → any BF not controlled by the opponent
                        # (uncontrolled BFs are allowed; they trigger a
                        # showdown when the move resolves).
                        for dest in ("battlefield_1", "battlefield_2"):
                            controller = bf_controllers[dest]
                            if controller is None or controller == active:
                                options.append(f"play:move_unit:{unit_idx}:{dest}")
                    else:
                        # Battlefield → base only. (BF ↔ BF rejected.)
                        options.append(f"play:move_unit:{unit_idx}:base")

                options.append("play:end_turn")
            return EngineOutput(
                game_state=self.game_state,
                player_1_options=options if active == RequiredTo.PLAYER_1 else [],
                player_2_options=options if active == RequiredTo.PLAYER_2 else [],
                required_action=_required_action(active, RequiredStep.ACTION_TURN),
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
