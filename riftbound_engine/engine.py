from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from . import abilities as _abilities
from . import effects as _effects
from .csv_data import card_domains_of, card_energy_of, card_power_of, card_type_of
from .deck_files import deck_data_for_id, list_deck_ids
from .protocol import ApplyVerb, RequiredStep, apply_prefix
from .triggers import EventLogEntry, GameEvent, TriggeredEffect, trigger_matches


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
    #: Persistent Might modifier applied by resolved effects (e.g. a
    #: "+1 Might" buff). Added on top of the card's printed Might wherever
    #: combat strength is computed (see ``might_at_battlefield``). Defaults
    #: to 0 so untouched units behave exactly as before.
    bonus_might: int = 0
    #: This-TURN [Shield] granted by effects (e.g. "give a unit [Shield 2] this
    #: turn"). Like ``bonus_might`` it resets at end of turn. Counts toward the
    #: unit's Shield total ONLY while it is defending (see effective_unit_might).
    #: Printed [Shield] and equipment [Shield] are NOT stored here — they're read
    #: live from the card / attached gear.
    bonus_shield: int = 0
    #: Stable per-game identity, assigned when the unit enters play and never
    #: reused. Positional refs ("player_1:0") shift when other units leave
    #: play, so a target chosen at cast time is captured BY UID and re-located
    #: by uid when the spell resolves later — see _run_spell_effects. 0 means
    #: "not yet assigned"; _backfill_unit_uids gives every in-play unit a real
    #: id before anything references it.
    uid: int = 0


@dataclass
class PlayedSpell:
    """A Spell-type card that has been played by paying its Energy/Power cost.

    Unlike Units, Spells don't go to a location — they just go to the
    player's spell stack. The UI renders them off to the right side of the
    screen. The engine has no per-spell resolution/effect step yet, but
    ``_advance_turn`` clears both players' spell stacks at end of turn so
    the overlay only shows spells cast during the current turn.

    ``targets`` records the units chosen to satisfy the card's Spell Choice
    Requirement (see ``PendingSpellChoice``), as ``"player_1:0"`` style
    tokens. Empty when the card required no choice.

    ``order`` is a per-turn cast index (0,1,2,…) assigned across BOTH players
    when the spell is cast, so the shared spell overlay can stack every
    spell in true chronological order regardless of who cast it. It resets
    each turn because the spell piles are cleared at end of turn.
    """

    card: str
    targets: list[str] = field(default_factory=list)
    order: int = 0
    #: Extra target lists from [Repeat] rounds beyond the base cast — one inner
    #: list per repeat the caster paid for (the base cast's targets live in
    #: ``targets``). Empty for non-repeated spells.
    repeat_targets: list[list[str]] = field(default_factory=list)


@dataclass
class PlayedGear:
    """A Gear-type card that has been played by paying its Energy/Power cost.

    Gears pay the same Energy + domain Power cost as Units, but they differ in
    two ways:

      * they enter **ready** (``exhausted=False``) — no summoning sickness,
        unlike Units which enter exhausted; and
      * they **cannot move**. A Gear is permanently anchored at the owner's
        ``base`` and is never offered a ``play:move_unit`` option, so its
        location stays ``"base"`` for the whole match.

    Because a Gear never leaves base it never participates in battlefield
    showdowns or combat. ``location`` is kept (always ``"base"``) for parity
    with ``PlayedUnit`` so the UI can render gears in the base zone, and
    ``exhausted`` is kept so a later tap-style ability could exhaust one (it
    re-readies on the owner's Awake step alongside units).
    """

    card: str
    #: Always ``"base"`` — gears do not move.
    location: str = "base"
    exhausted: bool = False
    #: For Equipment gears: the STABLE uid (PlayedUnit.uid) of the unit it's
    #: attached to, or None if unequipped. This is the attachment's identity —
    #: positional ``"controller:index"`` refs drift when other units leave
    #: play, so everything resolves the host by uid (mirrors how delayed spell
    #: targets are relocated). ``attached_to`` below is a derived display ref.
    attached_uid: int | None = None
    #: Last-known ``"controller:index"`` position of the host, for display/back-
    #: compat only. Identity lives in ``attached_uid``; the serializer recomputes
    #: this from the uid so it's always the host's CURRENT position.
    attached_to: str | None = None
    #: ``total_turn_number`` on which this gear was (re-)attached, or None if
    #: unequipped. Lets continuous "while attached THIS turn" conditions (e.g.
    #: Brutalizer's extra +2 Might) compare it to the current turn.
    attached_on_turn: int | None = None


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
class PendingSpellChoice:
    """A Spell whose cost is paid and is now waiting for the caster to choose
    the targets its Spell Choice Requirement demands.

    Set by ``play:play_spell:*`` when the card's requirement parses to a
    single ANY-UNIT phrase that forces a pick (minimum >= 1). While set, the
    caster's options collapse to the enumerated
    ``play:choose_spell_targets:<refs>`` sets (one per valid target
    combination); all other actions are suppressed. Cleared by
    ``play:choose_spell_targets:*`` once a valid set is chosen, which is when
    the card finally lands on the spell stack (with ``PlayedSpell.targets``
    recorded). ``requirement`` is the raw CSV requirement string so the
    engine can re-derive the parsed phrase when enumerating / validating.

    Multi-phrase requirements (a pure-AND tree like ``ANY UNIT (1[BF])|ANY
    UNIT (1)``) force one pick per phrase, made sequentially. ``chosen``
    records the picks committed so far — one inner list of ``p1-0`` / ``p2-1``
    wire tokens per resolved phrase. The phrase currently being chosen is
    ``spell_target_plan(requirement)[len(chosen)]``; the spell only lands once
    ``len(chosen)`` reaches the plan length. Single-phrase spells resolve on
    the first pick, exactly as before.
    """

    actor: RequiredTo
    card: str
    requirement: str
    chosen: list[list[str]] = field(default_factory=list)
    #: For MOVE phrases, the destination location chosen for each moved unit,
    #: in the same order as ``requirements.moved_unit_refs``. Collected AFTER
    #: all unit picks via ``play:choose_spell_destination:<location>``; the
    #: spell only lands once every moved unit has a destination.
    destinations: list[str] = field(default_factory=list)
    #: For BATTLEFIELD phrases, the battlefield slot chosen for each (in
    #: ``requirements.battlefield_picks`` order). Collected via
    #: ``play:choose_spell_battlefield:<slot>``.
    battlefields: list[str] = field(default_factory=list)
    #: For GEAR phrases, the chosen gear refs ("g1-0" / "g2-0"). Collected
    #: via ``play:choose_spell_gear:<gref>``.
    gears: list[str] = field(default_factory=list)
    #: For TRASH phrases, the chosen trash refs ("t1-0" / "t2-0"). Collected
    #: via ``play:choose_spell_trash:<tref>``.
    trash: list[str] = field(default_factory=list)
    #: For SPELL phrases, the chosen chain-spell refs ("c-0" = top of chain).
    #: Collected via ``play:choose_spell_chain:<cref>``.
    spells: list[str] = field(default_factory=list)
    #: For LOCATION phrases, the chosen location(s) ("base"/"battlefield_1"/…).
    #: Collected last, via ``play:choose_spell_location:<loc>``.
    locations: list[str] = field(default_factory=list)
    #: Finalized target lists from PRIOR selection rounds — non-empty only when
    #: this choice is a repeat round (the [Repeat] keyword re-opened selection
    #: after the caster paid the additional cost). Threaded through so the
    #: finalizer can accumulate every round's targets onto the one spell.
    prior_rounds: list[list[str]] = field(default_factory=list)
    #: ACTIVATED-ABILITY mode: when set, this target pick is for an in-play
    #: permanent's activated ability (not a cast spell). ``activation_source`` is
    #: the permanent's ref ("player_1:0" / "gear:player_1:0"), ``activation_effects``
    #: its effect codes. On finalize the chosen targets are put on the chain as
    #: an EFFECT item (not a spell → no trash, no ON_PLAY_SPELL).
    activation_source: str | None = None
    activation_effects: tuple[str, ...] = ()


@dataclass
class PendingEffectChoice:
    """A resolving triggered ability waiting for a player decision.

    Set by ``GameEngine._run_effect_codes`` when a CHOICE effect comes up —
    e.g. Abandoned Hall's "they may give a unit they control here +1 might
    this turn". While set, the chooser's options collapse to
    ``play:choose_effect_target:<token>`` (one per valid unit, ``p1-0`` wire
    tokens — the same format spell targets use) plus
    ``play:choose_effect_target:pass`` (the "may" opt-out); everything else
    is suppressed, exactly like spell targeting. Cleared by the
    ``choose_effect_target`` handler, which applies the pick and then runs
    ``remaining_effects`` (which may pause on a new choice).
    """

    #: Who decides — the controller of the resolved ability (for battlefield
    #: abilities phrased "they may …", that's the player the event was about,
    #: e.g. the spell caster).
    actor: RequiredTo
    #: The choice-effect code being resolved (e.g. ``MAY_GIVE_UNIT_HERE_+1M``).
    code: str
    #: The owning card's ref — a battlefield slot ("battlefield_1") or a unit
    #: ref ("player_1:0").
    source: str | None
    #: The owning card's NAME (resolved from ``source``), for labels.
    source_card: str | None
    #: Valid pick tokens ("p1-0" / "p2-1"), enumerated when the choice opened.
    options: list[str] = field(default_factory=list)
    #: Effect codes of the same ability still to run after this choice.
    remaining_effects: list[str] = field(default_factory=list)
    #: Chain label of the resolving ability, for the event feed.
    label: str = ""
    #: Trigger / event metadata carried through for the continuation context.
    trigger: str = ""
    event_kind: str = ""
    #: True ⇒ the chooser may decline (a "may" effect, e.g. Abandoned Hall):
    #: ``play:choose_effect_target:pass`` is offered. False ⇒ a FORCED choice
    #: (e.g. "discard 1"): the player must pick, no pass option.
    optional: bool = True
    #: Targets carried into the continuation's effect codes (e.g. a spell's
    #: chosen units), so codes after the choice still see them.
    continuation_targets: tuple[str, ...] = ()


@dataclass
class PendingSpellRepeat:
    """A just-played [Repeat] spell waiting for the caster to decide whether
    to pay the additional cost and repeat its effect.

    Set after a selection round finalizes (or immediately after cast for a
    no-target spell). While set, the caster may bank Energy/Power by
    exhausting/recycling runes — the same "shortcut payment way" the base
    cost is paid — and then either ``play:choose_repeat:yes`` (pay the cost,
    re-open selection for another round) or ``play:choose_repeat:no`` (push
    the spell, with every round's targets, onto the chain).

    ``rounds`` accumulates the finalized target list from each completed
    selection round (the base cast plus each repeat so far). ``cost`` is the
    parsed Repeat cost (``card_repeat_cost`` shape).
    """

    actor: RequiredTo
    card: str
    cost: dict
    rounds: list[list[str]] = field(default_factory=list)


@dataclass
class PendingAccelerate:
    """A just-played [Accelerate] unit waiting for its controller to decide
    whether to pay the additional cost to enter READY.

    Modeled on :class:`PendingSpellRepeat`: it appears AFTER the unit's
    location is chosen. While set, the controller may bank Energy/Power by
    exhausting/recycling runes (the same shortcut-payment way), then either
    ``play:choose_accelerate:yes`` (pay the cost, ready the unit) or
    ``play:choose_accelerate:no`` (decline; the unit stays exhausted).

    The unit's "when you play me" event is DEFERRED until this decision
    resolves, so its trigger/chain doesn't interleave with the decision —
    ``source_ref`` / ``battlefield`` carry what the emit needs.
    """

    actor: RequiredTo
    card: str
    unit_index: int
    cost: dict
    source_ref: str
    battlefield: str | None = None


@dataclass
class PendingAbilityCost:
    """A triggered ability whose trigger just fired but which carries a payable
    cost (today: ``EXHAUST_THIS``). The controller MAY pay the cost to put the
    ability's effect on the chain; declining means the effect never happens.
    Set during trigger drain, BEFORE the effect is pushed — the effect only
    reaches the chain if the player pays (``play:choose_ability_cost:yes``).

    Carries everything needed to rebuild the TriggeredEffect and push it on pay
    (so deep-copy/serialize stay flat — no nested dataclass)."""

    actor: RequiredTo
    source: str | None
    source_card: str | None
    trigger: str
    event_kind: str
    effects: tuple[str, ...]
    conditions: tuple[str, ...] = ()
    context_card: str | None = None
    label: str = ""


@dataclass
class ChainItem:
    """One spell sitting on the chain (the priority stack).

    ``actor`` is who cast it, ``targets`` the units chosen for its
    requirement (``"player_1:0"`` tokens, same as PlayedSpell.targets). The
    engine has no per-spell effect yet, so resolving the chain just moves
    each item into its caster's spell pile — but the chain models the
    priority window where Reaction spells can respond.
    """

    actor: RequiredTo
    #: For a cast spell, the spell's card name. ``None`` for a triggered
    #: ability item (which carries an ``effect`` instead).
    card: str | None = None
    targets: list[str] = field(default_factory=list)
    #: Extra [Repeat]-round target lists beyond the base cast (see PlayedSpell).
    repeat_targets: list[list[str]] = field(default_factory=list)
    #: Set when this item is a TRIGGERED ABILITY rather than a cast spell.
    #: On resolution the engine runs each of ``effect.effects`` via the
    #: effect registry (see ``_resolve_chain``); a spell item (``effect is
    #: None``) still resolves as the historical no-op.
    effect: "TriggeredEffect | None" = None
    #: Human-readable label for the UI ("Garen — WHEN_YOU_PLAY_ME").
    label: str = ""
    #: The card's raw Spell Choice Requirement, captured at cast time so the
    #: chosen targets can be RE-VALIDATED against current state when the spell
    #: resolves (reactions may have changed the board in between — e.g. a unit
    #: buffed past Gust's "3 Might or less"). Empty ⇒ nothing to re-check.
    requirement: str = ""
    #: Stable uid (PlayedUnit.uid) for each entry in ``targets`` — None for a
    #: non-unit token ("bf:…", "move_dest:…"). Captured at cast time; at
    #: resolution the unit ref is RE-LOCATED by uid (its positional index may
    #: have shifted), and a target whose uid is gone is treated as illegal.
    target_uids: list[int | None] = field(default_factory=list)
    #: Per-[Repeat]-round parallel of ``target_uids`` for ``repeat_targets``.
    repeat_target_uids: list[list[int | None]] = field(default_factory=list)
    #: Stable per-game chain-item id, assigned when the item is pushed onto the
    #: chain. Positional indices into ``pending_chain.items`` shift as items
    #: resolve/pop, so a counterspell captures its target BY cid and re-locates
    #: it at resolution (the chain analog of PlayedUnit.uid). 0 = unassigned.
    cid: int = 0


@dataclass
class PendingChain:
    """The priority stack opened whenever a spell is played.

    ``items`` is ordered with the most recently added spell FIRST (index 0 =
    top of the chain). ``priority`` is the player who may currently act:
    they can cast a Reaction spell (which pushes onto the chain and hands
    priority back to that caster, resetting the pass count) or pass. When
    ``consecutive_passes`` reaches 2 (both players passed in a row with no
    new spell), only the TOP item resolves (LIFO) — the chain shrinks by
    one and the OWNER of the newly-revealed top item gains priority to
    respond, with the pass counter reset. The chain closes once its last
    item resolves; play then returns to where it was before the first cast.
    """

    items: list[ChainItem]
    priority: RequiredTo
    consecutive_passes: int = 0


@dataclass
class PendingShowdown:
    """An active showdown over a contested battlefield.

    Opened when the active player moves a unit onto a battlefield they
    don't already control — either an uncontrolled BF or one held by the
    opponent.

    MUSTER window: right after the first unit walks in, the showdown is
    "unlocked" (``locked == False``). While unlocked, the initiator hasn't
    committed yet and may keep MOVING additional ready units onto the same
    battlefield (reinforce before the fight) instead of being forced to
    pass immediately. The initiator may also play an [Action]/[Reaction]
    spell during this window; doing so LOCKS the showdown (``locked =
    True``) — the fight has begun, so no more units can be mustered in.
    Passing also ends mustering.

    FOCUS (the showdown's equivalent of priority): exactly one player holds
    focus at a time, starting with the initiator. The focus holder may play
    [Action]/[Reaction] spells (each opens a chain that runs the normal
    priority/Reaction sub-loop), and — if they're the initiator and the
    fight hasn't started — muster more units. When they're done they PASS
    FOCUS to the opponent. Playing a spell resets the focus-pass counter
    (the fight continues); two consecutive focus passes (``focus_passes``
    reaches 2) end the showdown: if only one side has units it's a clean
    conquest, if both do it opens a ``PendingCombat`` damage step. The
    field is cleared on resolution.
    """

    battlefield: str  # "battlefield_1" or "battlefield_2"
    initiator: RequiredTo
    #: Who currently holds FOCUS. ``None`` is normalized to the initiator
    #: (the starting holder) via :attr:`focus_holder`.
    focus: "RequiredTo | None" = None
    #: Consecutive focus passes with no intervening play. Reaches 2 ⇒ both
    #: players passed focus in a row ⇒ the showdown resolves.
    focus_passes: int = 0
    #: True once the fight has "started" — a card was played in the
    #: showdown. While False (and focus is with the initiator) the
    #: initiator may still muster additional units onto the battlefield.
    locked: bool = False

    @property
    def focus_holder(self) -> RequiredTo:
        """The player on the clock — defaults to the initiator when unset."""
        return self.focus if self.focus is not None else self.initiator


@dataclass
class PendingCombat:
    """Simultaneous damage-distribution state after a contested showdown.

    When both players have passed on a ``PendingShowdown`` over a battlefield
    where BOTH have units, the engine moves into this state instead of
    resolving the showdown directly. Each player independently picks which
    OPPONENT units to kill using their own total Might at the battlefield;
    distributions are "blind" (one player can't see what the other has
    committed yet) and applied simultaneously once both have committed.

    Damage rule (Riftbound combat): a player's damage budget is the SUM of
    the Might of every unit they control at the battlefield. Damage is
    ASSIGNED unit-by-unit and you must assign LETHAL damage to one enemy
    unit before moving on to the next. A unit's lethal threshold is
    ``max(Might, 1)`` — a 0-Might unit still needs 1 damage to die, so it
    is never free to kill. You may not over-assign past lethal while another
    enemy unit could still be assigned to. Consequence: a player kills a
    *maximal* set of enemy units — any subset ``S`` whose summed lethal cost
    ≤ budget AND where the leftover (``budget − sum(S)``) is smaller than
    every surviving enemy unit's lethal threshold (so no further kill was
    possible). Leftover that can't finish off another unit is dealt as
    non-lethal damage and, since this game keeps no persistent damage,
    simply has no effect.

    Assigning is not dealing: both players commit their assignments BLIND
    (one can't see the other's), then all assignments are applied
    SIMULTANEOUSLY. Killed units leave play and go to their owner's trash.

    ``player_X_targets`` stores the indices (into the OPPONENT's
    ``player_X_units`` list at the moment combat opened) of the units that
    player assigned lethal damage to — i.e. the units they kill. ``None``
    means not committed yet; ``[]`` means committed to killing nothing
    (only legal when the budget can't kill any enemy unit). A 0-might
    attacker auto-commits to ``[]`` on entry.
    """

    battlefield: str  # "battlefield_1" or "battlefield_2"
    player_1_might: int  # P1's total Might at the BF when combat opened
    player_2_might: int  # P2's total Might at the BF when combat opened
    player_1_targets: list[int] | None = None  # indices into player_2_units
    player_2_targets: list[int] | None = None  # indices into player_1_units
    #: Who INITIATED the showdown that led to this combat (the attacker). The
    #: other player is the DEFENDER, whose units get their [Shield] Might while
    #: the fight is live. None only for legacy/test combats built without it.
    initiator: "RequiredTo | None" = None


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
class ActionLogEntry:
    """One committed engine action.

    Appended by ``GameEngine.apply_action`` after the action successfully
    mutates the state. Lives on ``GameState`` (not on the engine instance)
    so it travels with branch-tree snapshots and ``/goto`` restoration:
    rewinding to an earlier state automatically truncates the log to that
    point's history. Reset starts a fresh GameState ⇒ empty log.

    ``actor`` and ``action`` are the same strings the engine accepts on
    ``apply_action`` — the UI can humanise them with its own formatter.
    ``sequence`` is the action's 0-indexed position in the log, useful for
    React keys and stable ordering even if entries get re-serialised.
    """

    sequence: int
    actor: str
    action: str


@dataclass
class GameState:
    counter: int = 0
    #: Next stable unit uid to hand out (see PlayedUnit.uid). Monotonic for the
    #: life of a game; travels with branch snapshots so re-located targets stay
    #: consistent across goto/restore.
    next_unit_uid: int = 1
    #: Next stable chain-item cid to hand out (see ChainItem.cid). Monotonic for
    #: the life of a game; travels with snapshots so a counterspell's captured
    #: target stays consistent across goto/restore.
    next_chain_cid: int = 1
    #: Ordered list of every committed action that produced this state.
    #: Appended to inside ``GameEngine.apply_action`` AFTER the action
    #: succeeds, so failed/raised actions do not pollute it. Survives
    #: deep-copy (branch-tree snapshots) automatically; reset gets a
    #: fresh GameState with the log empty.
    action_log: list[ActionLogEntry] = field(default_factory=list)
    #: On-screen feed of trigger/effect activity (events fired, abilities that
    #: went on the chain, effects that resolved). Like ``action_log`` it lives
    #: on the state so it travels with branch snapshots and goto/restore.
    event_feed: list[EventLogEntry] = field(default_factory=list)
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
    #: Cards played as gears this match, in play order. Gears are paid like
    #: units (Energy + domain Power) but enter READY and stay anchored at the
    #: owner's base — they never move and never enter ``pending_play``. See
    #: PlayedGear and action_turn/builtins.py::_play_gear.
    player_1_gears: list[PlayedGear] = field(default_factory=list)
    player_2_gears: list[PlayedGear] = field(default_factory=list)
    #: Units that have died (e.g. killed in combat) go here, in death order,
    #: by card name. A dead unit leaves play entirely — it's removed from
    #: ``player_X_units`` and appended to its owner's trash. See
    #: ``resolve_combat``.
    player_1_trash: list[str] = field(default_factory=list)
    player_2_trash: list[str] = field(default_factory=list)
    #: Set while a `play:play_unit:*` is waiting for `play:choose_location:*`.
    pending_play: PendingPlay | None = None
    #: Set while a `play:play_spell:*` whose requirement forces a target pick
    #: is waiting for `play:choose_spell_targets:*`. See PendingSpellChoice.
    pending_spell_choice: "PendingSpellChoice | None" = None
    #: Set while a resolving triggered ability waits for its controller to
    #: pick an effect target (or pass). See PendingEffectChoice.
    pending_effect_choice: "PendingEffectChoice | None" = None
    #: Set while a just-played [Repeat] spell waits for the caster to decide
    #: whether to pay the additional cost and repeat. See PendingSpellRepeat.
    pending_spell_repeat: "PendingSpellRepeat | None" = None
    #: Set while a just-played [Accelerate] unit waits for its controller to
    #: decide whether to pay the extra cost to enter ready. See PendingAccelerate.
    pending_accelerate: "PendingAccelerate | None" = None
    #: Set while a fired triggered ability waits for its controller to decide
    #: whether to pay its cost (EXHAUST_THIS) to put the effect on the chain.
    #: See PendingAbilityCost.
    pending_ability_cost: "PendingAbilityCost | None" = None
    #: The priority stack. Set the moment a spell is played and cleared when
    #: both players pass in a row (the chain resolves). While set, the player
    #: holding priority may cast a Reaction spell or `play:pass_priority`.
    #: See PendingChain.
    pending_chain: "PendingChain | None" = None
    #: Set after a play has been committed to a location but the active player
    #: still owes ``remaining`` Energy in exhausted runes (cost > 0). Cleared
    #: when the last rune is exhausted.
    pending_payment: PendingPayment | None = None
    #: Set when the active player moves a unit onto an uncontrolled battlefield.
    #: While set, both players' options are limited to ``play:pass_showdown`` —
    #: first the initiator passes, then the opponent. See PendingShowdown.
    pending_showdown: PendingShowdown | None = None
    #: Set after both players have passed a showdown over a CONTESTED
    #: battlefield (both had units there). While set, each player's
    #: options collapse to ``play:commit_kills:<csv-of-target-indices>`` —
    #: their blind damage-distribution against the opponent's units.
    #: Combat resolves once both have committed; see PendingCombat.
    pending_combat: "PendingCombat | None" = None
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
    #: The chosen champion (a fixed Unit from the deck) starts available
    #: beside the board and can be played at action timing like a hand card
    #: once affordable. These flags flip True the moment that play STARTS
    #: (the champion enters ``pending_play``), so it can't be played twice.
    player_1_champion_played: bool = False
    player_2_champion_played: bool = False
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
        #: Triggered abilities collected by ``_emit`` during the current
        #: action, awaiting ``_drain_triggers`` to push them onto the chain.
        #: Transient (not part of GameState) — always empty between actions.
        self._trigger_queue: list[TriggeredEffect] = []

    @property
    def game_state(self) -> GameState:
        return GameState(
            counter=self._game_state.counter,
            next_unit_uid=self._game_state.next_unit_uid,
            next_chain_cid=self._game_state.next_chain_cid,
            action_log=[
                ActionLogEntry(sequence=e.sequence, actor=e.actor, action=e.action)
                for e in self._game_state.action_log
            ],
            event_feed=[
                EventLogEntry(sequence=e.sequence, kind=e.kind, text=e.text)
                for e in self._game_state.event_feed
            ],
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
                PlayedUnit(card=u.card, location=u.location, exhausted=u.exhausted, bonus_might=u.bonus_might, bonus_shield=u.bonus_shield, uid=u.uid)
                for u in self._game_state.player_1_units
            ],
            player_2_units=[
                PlayedUnit(card=u.card, location=u.location, exhausted=u.exhausted, bonus_might=u.bonus_might, bonus_shield=u.bonus_shield, uid=u.uid)
                for u in self._game_state.player_2_units
            ],
            player_1_spells=[
                PlayedSpell(
                    card=s.card,
                    targets=list(s.targets),
                    order=s.order,
                    repeat_targets=[list(r) for r in s.repeat_targets],
                )
                for s in self._game_state.player_1_spells
            ],
            player_2_spells=[
                PlayedSpell(
                    card=s.card,
                    targets=list(s.targets),
                    order=s.order,
                    repeat_targets=[list(r) for r in s.repeat_targets],
                )
                for s in self._game_state.player_2_spells
            ],
            player_1_gears=[
                PlayedGear(
                    card=g.card,
                    location=g.location,
                    exhausted=g.exhausted,
                    attached_uid=g.attached_uid,
                    attached_to=g.attached_to,
                    attached_on_turn=g.attached_on_turn,
                )
                for g in self._game_state.player_1_gears
            ],
            player_2_gears=[
                PlayedGear(
                    card=g.card,
                    location=g.location,
                    exhausted=g.exhausted,
                    attached_uid=g.attached_uid,
                    attached_to=g.attached_to,
                    attached_on_turn=g.attached_on_turn,
                )
                for g in self._game_state.player_2_gears
            ],
            player_1_trash=list(self._game_state.player_1_trash),
            player_2_trash=list(self._game_state.player_2_trash),
            pending_play=(
                None
                if self._game_state.pending_play is None
                else PendingPlay(
                    actor=self._game_state.pending_play.actor,
                    card=self._game_state.pending_play.card,
                )
            ),
            pending_spell_choice=(
                None
                if self._game_state.pending_spell_choice is None
                else PendingSpellChoice(
                    actor=self._game_state.pending_spell_choice.actor,
                    card=self._game_state.pending_spell_choice.card,
                    requirement=self._game_state.pending_spell_choice.requirement,
                    chosen=[list(picks) for picks in self._game_state.pending_spell_choice.chosen],
                    destinations=list(self._game_state.pending_spell_choice.destinations),
                    battlefields=list(self._game_state.pending_spell_choice.battlefields),
                    gears=list(self._game_state.pending_spell_choice.gears),
                    trash=list(self._game_state.pending_spell_choice.trash),
                    spells=list(self._game_state.pending_spell_choice.spells),
                    locations=list(self._game_state.pending_spell_choice.locations),
                    prior_rounds=[list(r) for r in self._game_state.pending_spell_choice.prior_rounds],
                    activation_source=self._game_state.pending_spell_choice.activation_source,
                    activation_effects=tuple(self._game_state.pending_spell_choice.activation_effects),
                )
            ),
            pending_spell_repeat=(
                None
                if self._game_state.pending_spell_repeat is None
                else PendingSpellRepeat(
                    actor=self._game_state.pending_spell_repeat.actor,
                    card=self._game_state.pending_spell_repeat.card,
                    cost={
                        "energy": int(self._game_state.pending_spell_repeat.cost.get("energy", 0)),
                        "power": dict(self._game_state.pending_spell_repeat.cost.get("power", {})),
                        "any_power": int(self._game_state.pending_spell_repeat.cost.get("any_power", 0)),
                    },
                    rounds=[list(r) for r in self._game_state.pending_spell_repeat.rounds],
                )
            ),
            pending_accelerate=(
                None
                if self._game_state.pending_accelerate is None
                else PendingAccelerate(
                    actor=self._game_state.pending_accelerate.actor,
                    card=self._game_state.pending_accelerate.card,
                    unit_index=self._game_state.pending_accelerate.unit_index,
                    cost={
                        "energy": int(self._game_state.pending_accelerate.cost.get("energy", 0)),
                        "power": dict(self._game_state.pending_accelerate.cost.get("power", {})),
                        "any_power": int(self._game_state.pending_accelerate.cost.get("any_power", 0)),
                    },
                    source_ref=self._game_state.pending_accelerate.source_ref,
                    battlefield=self._game_state.pending_accelerate.battlefield,
                )
            ),
            pending_ability_cost=(
                None
                if self._game_state.pending_ability_cost is None
                else PendingAbilityCost(
                    actor=self._game_state.pending_ability_cost.actor,
                    source=self._game_state.pending_ability_cost.source,
                    source_card=self._game_state.pending_ability_cost.source_card,
                    trigger=self._game_state.pending_ability_cost.trigger,
                    event_kind=self._game_state.pending_ability_cost.event_kind,
                    effects=tuple(self._game_state.pending_ability_cost.effects),
                    conditions=tuple(self._game_state.pending_ability_cost.conditions),
                    context_card=self._game_state.pending_ability_cost.context_card,
                    label=self._game_state.pending_ability_cost.label,
                )
            ),
            pending_effect_choice=(
                None
                if self._game_state.pending_effect_choice is None
                else PendingEffectChoice(
                    actor=self._game_state.pending_effect_choice.actor,
                    code=self._game_state.pending_effect_choice.code,
                    source=self._game_state.pending_effect_choice.source,
                    source_card=self._game_state.pending_effect_choice.source_card,
                    options=list(self._game_state.pending_effect_choice.options),
                    remaining_effects=list(self._game_state.pending_effect_choice.remaining_effects),
                    label=self._game_state.pending_effect_choice.label,
                    trigger=self._game_state.pending_effect_choice.trigger,
                    event_kind=self._game_state.pending_effect_choice.event_kind,
                    optional=self._game_state.pending_effect_choice.optional,
                    continuation_targets=tuple(
                        self._game_state.pending_effect_choice.continuation_targets
                    ),
                )
            ),
            pending_chain=(
                None
                if self._game_state.pending_chain is None
                else PendingChain(
                    items=[
                        ChainItem(
                            actor=it.actor,
                            card=it.card,
                            targets=list(it.targets),
                            repeat_targets=[list(r) for r in it.repeat_targets],
                            effect=it.effect,
                            label=it.label,
                            requirement=it.requirement,
                            target_uids=list(it.target_uids),
                            repeat_target_uids=[list(r) for r in it.repeat_target_uids],
                            cid=it.cid,
                        )
                        for it in self._game_state.pending_chain.items
                    ],
                    priority=self._game_state.pending_chain.priority,
                    consecutive_passes=self._game_state.pending_chain.consecutive_passes,
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
                    focus=self._game_state.pending_showdown.focus,
                    focus_passes=self._game_state.pending_showdown.focus_passes,
                    locked=self._game_state.pending_showdown.locked,
                )
            ),
            pending_combat=(
                None
                if self._game_state.pending_combat is None
                else PendingCombat(
                    battlefield=self._game_state.pending_combat.battlefield,
                    player_1_might=self._game_state.pending_combat.player_1_might,
                    player_2_might=self._game_state.pending_combat.player_2_might,
                    player_1_targets=(
                        None
                        if self._game_state.pending_combat.player_1_targets is None
                        else list(self._game_state.pending_combat.player_1_targets)
                    ),
                    player_2_targets=(
                        None
                        if self._game_state.pending_combat.player_2_targets is None
                        else list(self._game_state.pending_combat.player_2_targets)
                    ),
                    initiator=self._game_state.pending_combat.initiator,
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
            player_1_champion_played=self._game_state.player_1_champion_played,
            player_2_champion_played=self._game_state.player_2_champion_played,
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
        if self._game_state.pending_combat is not None:
            raise ValueError(
                "cannot end the turn while a contested combat is unresolved — "
                "both players must assign their combat damage first"
            )
        if self._game_state.pending_spell_choice is not None:
            raise ValueError("cannot end the turn while a spell is waiting for target selection")
        if self._game_state.pending_effect_choice is not None:
            raise ValueError("cannot end the turn while an effect choice is pending")
        if self._game_state.pending_spell_repeat is not None:
            raise ValueError("cannot end the turn while a [Repeat] decision is pending")
        if self._game_state.pending_chain is not None:
            raise ValueError("cannot end the turn while the chain is open — resolve it first")
        # The active player's turn is ending — fire end-of-turn triggers (e.g.
        # an equipment's unattach + self-damage) BEFORE per-turn cleanup, while
        # the board is still intact. Queued now; drained at the start() choke.
        self._emit(GameEvent(kind="TURN_END", controller=cp.value))
        # Energy and Power are per-turn: clear both players' pools so nothing
        # carries into the next turn.
        self._game_state.player_1_energy = 0
        self._game_state.player_2_energy = 0
        self._game_state.player_1_power = {}
        self._game_state.player_2_power = {}
        # The per-BF scoring cap is also per-turn: clear the set so each
        # battlefield can score again on the new turn (if conditions hold).
        self._game_state.scored_bfs_this_turn = set()
        # Might buffs and granted [Shield] are "this turn" (every implemented
        # buff/grant reads "… this turn"): they expire when the turn ends.
        for unit in (*self._game_state.player_1_units, *self._game_state.player_2_units):
            unit.bonus_might = 0
            unit.bonus_shield = 0
        # Spells "resolve" at end of turn — we don't yet model their effects
        # or a discard pile, so for now they simply vanish off the right-side
        # spell overlay. Both players' stacks are cleared (only the active
        # player can cast today, but interrupts may exist later and the
        # overlay should be empty going into the next turn regardless).
        self._game_state.player_1_spells = []
        self._game_state.player_2_spells = []
        nxt = RequiredTo.PLAYER_2 if cp == RequiredTo.PLAYER_1 else RequiredTo.PLAYER_1
        self._game_state.current_player = nxt
        self._game_state.total_turn_number += 1
        if nxt == RequiredTo.PLAYER_1:
            self._game_state.player_1_turn_number += 1
        else:
            self._game_state.player_2_turn_number += 1
        self._reset_abcd_flags()
        # A new turn begins for ``nxt`` — fire start-of-turn ("beginning
        # phase") triggers. Queued now, drained at the start() choke point.
        self._emit(GameEvent(kind="TURN_START", controller=nxt.value))

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
        self._emit(GameEvent(kind="ON_CHANNEL", controller=actor.value))

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
        self._emit(GameEvent(kind="ON_DRAW", controller=actor.value))

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

    def award_bf_point(
        self, actor: RequiredTo, battlefield: str, *, via: str = "conquer"
    ) -> bool:
        """Award ``actor`` 1 point for ``battlefield`` if it hasn't already
        scored this turn. Returns ``True`` when a point was awarded, ``False``
        when the per-BF-per-turn cap suppressed it.

        Used by both B-phase HOLD scoring and showdown wins so the cap is
        enforced uniformly regardless of source. ``via`` says WHICH it is:

          * ``"conquer"`` — control changed hands → fire ON_CONQUER.
          * ``"hold"``    — kept across turns at the beginning phase → ON_HOLD.

        Either way a point landed, so ON_SCORE fires too — that's the event
        "score here" triggers listen for (any point, regardless of source).
        """
        if battlefield in self._game_state.scored_bfs_this_turn:
            return False
        self._game_state.scored_bfs_this_turn.add(battlefield)
        self.add_score(actor, 1)
        # The conquer/hold-specific event first, then the generic score event.
        kind = "ON_HOLD" if via == "hold" else "ON_CONQUER"
        self._emit(GameEvent(kind=kind, controller=actor.value, battlefield=battlefield))
        self._emit(
            GameEvent(kind="ON_SCORE", controller=actor.value, battlefield=battlefield)
        )
        return True

    def _spell_choice_options(self, choice: "PendingSpellChoice") -> list[str]:
        """Enumerate every valid ``play:choose_spell_targets:<refs>`` for the
        phrase the spell is currently waiting on.

        One option per valid target set (combination of units that satisfies
        the current phrase — count, per-unit filters, and group constraints).
        Refs are ``p1-<i>`` / ``p2-<i>`` tokens identifying a unit by its
        position in that player's units list.

        For a multi-phrase requirement only the phrase at index
        ``len(choice.chosen)`` is offered; units already committed to earlier
        phrases are excluded, and a candidate is dropped if picking it would
        leave a later phrase with no distinct units (so the caster can never
        paint themselves into a soft-lock). Returns ``[]`` once every phrase
        is chosen or if the requirement forces no pick (defensive)."""
        from .requirements import (
            enumerate_unit_target_sets,
            moved_unit_refs,
            plan_feasible,
            ref_to_token,
            spell_target_plan,
            token_to_ref,
        )

        plan = spell_target_plan(choice.requirement)
        idx = len(choice.chosen)
        if idx >= len(plan):
            # All unit picks are in. Then MOVE phrases need a destination per
            # moved unit, BATTLEFIELD phrases a battlefield, and GEAR phrases a
            # gear — offered in that order.
            dest = self._spell_destination_options(choice)
            if dest:
                return dest
            bf = self._spell_battlefield_options(choice)
            if bf:
                return bf
            gear = self._spell_gear_options(choice)
            if gear:
                return gear
            trash = self._spell_trash_options(choice)
            if trash:
                return trash
            spell = self._spell_chain_options(choice)
            if spell:
                return spell
            return self._spell_location_options(choice)
        caster = choice.actor
        used = {token_to_ref(tok) for picks in choice.chosen for tok in picks}
        req = plan[idx]
        rest = plan[idx + 1 :]
        options: list[str] = []
        for combo in enumerate_unit_target_sets(
            req, self._game_state, caster=caster, exclude=used
        ):
            if not plan_feasible(rest, self._game_state, caster, used | set(combo)):
                continue
            options.append(
                "play:choose_spell_targets:" + ",".join(ref_to_token(r) for r in combo)
            )
        return options

    def _spell_destination_options(self, choice: "PendingSpellChoice") -> list[str]:
        """``play:choose_spell_destination:<location>`` options for the next
        moved unit awaiting a destination, or ``[]`` if none are pending.

        Per the agreed rules, a MOVE spell may send the unit anywhere — any
        location EXCEPT the one it's already at (a free relocation). Showdown
        triggering on a contested battlefield is part of the (deferred) effect,
        not the choice, so it isn't applied here."""
        from .requirements import moved_unit_refs

        moved = moved_unit_refs(choice.requirement, choice.chosen)
        if len(choice.destinations) >= len(moved):
            return []
        controller, index = moved[len(choice.destinations)]
        units = (
            self._game_state.player_1_units
            if controller == "player_1"
            else self._game_state.player_2_units
        )
        current = units[index].location if 0 <= index < len(units) else None
        return [
            f"play:choose_spell_destination:{loc}"
            for loc in UNIT_LOCATIONS
            if loc != current
        ]

    def _spell_battlefield_options(self, choice: "PendingSpellChoice") -> list[str]:
        """``play:choose_spell_battlefield:<slot>`` options for the next
        BATTLEFIELD pick a spell is waiting on, or ``[]`` if none are pending.

        Only battlefield slots that exist are offered; a
        ``WHERE_FRIENDLY_UNITS`` phrase further restricts to a battlefield
        where the caster has a unit."""
        from .requirements import battlefield_picks

        picks = battlefield_picks(choice.requirement)
        if len(choice.battlefields) >= len(picks):
            return []
        req = picks[len(choice.battlefields)]
        caster = choice.actor
        options: list[str] = []
        for slot in ("battlefield_1", "battlefield_2"):
            if getattr(self._game_state, slot) is None:
                continue  # not chosen yet (shouldn't happen mid-game)
            if req.where_friendly and not self._caster_has_unit_at(caster, slot):
                continue
            options.append(f"play:choose_spell_battlefield:{slot}")
        return options

    def _caster_has_unit_at(self, caster: RequiredTo, slot: str) -> bool:
        units = (
            self._game_state.player_1_units
            if caster == RequiredTo.PLAYER_1
            else self._game_state.player_2_units
        )
        return any(u.location == slot for u in units)

    def _all_gear_refs(self) -> list[str]:
        """Every gear on the board as a ``g1-<i>`` / ``g2-<i>`` ref token."""
        refs: list[str] = []
        for prefix, gears in (
            ("g1", self._game_state.player_1_gears),
            ("g2", self._game_state.player_2_gears),
        ):
            refs.extend(f"{prefix}-{i}" for i in range(len(gears)))
        return refs

    def _spell_gear_options(self, choice: "PendingSpellChoice") -> list[str]:
        """``play:choose_spell_gear:<gref>`` options for the next gear pick a
        spell is waiting on, or ``[]`` if none are pending. Gears already picked
        for this spell are excluded so the same one can't be chosen twice."""
        from .requirements import gear_picks

        needed = sum(g.min_count for g in gear_picks(choice.requirement))
        if len(choice.gears) >= needed:
            return []
        taken = set(choice.gears)
        return [
            f"play:choose_spell_gear:{ref}"
            for ref in self._all_gear_refs()
            if ref not in taken
        ]

    def _spell_trash_options(self, choice: "PendingSpellChoice") -> list[str]:
        """``play:choose_spell_trash:<tref>`` options for the next trash pick a
        spell is waiting on, or ``[]`` if none are pending. Only trash cards
        matching the current TRASH phrase (side scope, unit-only, energy cap)
        are offered; already-picked refs are excluded."""
        from .requirements import matching_trash_refs, trash_picks

        picks = trash_picks(choice.requirement)
        needed = sum(t.min_count for t in picks)
        if len(choice.trash) >= needed:
            return []
        # The phrase governing the next pick (picks have min_count 1 in the data).
        req = picks[min(len(choice.trash), len(picks) - 1)]
        refs = matching_trash_refs(
            req,
            list(self._game_state.player_1_trash),
            list(self._game_state.player_2_trash),
            choice.actor.value,
        )
        taken = set(choice.trash)
        out: list[str] = []
        for controller, idx in refs:
            tok = f"{'t1' if controller == 'player_1' else 't2'}-{idx}"
            if tok not in taken:
                out.append(f"play:choose_spell_trash:{tok}")
        return out

    def _chain_items_for_match(self) -> list[tuple[str, str]]:
        chain = self._game_state.pending_chain
        if chain is None:
            return []
        return [(it.actor.value, it.card) for it in chain.items]

    def _spell_chain_options(self, choice: "PendingSpellChoice") -> list[str]:
        """``play:choose_spell_chain:c-<i>`` options for the next SPELL phrase a
        spell is waiting on, or ``[]`` if none are pending. Only chain spells
        matching the phrase (side scope, cost caps) are offered; already-picked
        refs are excluded."""
        from .requirements import matching_spell_refs, spell_picks

        picks = spell_picks(choice.requirement)
        needed = sum(s.min_count for s in picks)
        if len(choice.spells) >= needed:
            return []
        req = picks[min(len(choice.spells), len(picks) - 1)]
        refs = matching_spell_refs(req, self._chain_items_for_match(), choice.actor.value)
        taken = set(choice.spells)
        return [f"play:choose_spell_chain:c-{i}" for i in refs if f"c-{i}" not in taken]

    def _spell_location_options(self, choice: "PendingSpellChoice") -> list[str]:
        """``play:choose_spell_location:<loc>`` options for the next LOCATION
        pick a spell is waiting on, or ``[]`` if none are pending. Offers base
        plus any battlefield slot that exists."""
        from .requirements import location_picks

        if len(choice.locations) >= len(location_picks(choice.requirement)):
            return []
        opts = ["play:choose_spell_location:base"]
        for slot in ("battlefield_1", "battlefield_2"):
            if getattr(self._game_state, slot) is not None:
                opts.append(f"play:choose_spell_location:{slot}")
        return opts

    def _spell_choice_complete(self, choice: "PendingSpellChoice") -> bool:
        """True when every pick a spell's requirement forces — unit phrases,
        MOVE destinations, BATTLEFIELD, GEAR, TRASH, chain-SPELL, and LOCATION
        picks — has been made."""
        from .requirements import (
            battlefield_picks,
            gear_picks,
            location_picks,
            moved_unit_refs,
            spell_picks,
            spell_target_plan,
            trash_picks,
        )

        if len(choice.chosen) < len(spell_target_plan(choice.requirement)):
            return False
        if len(choice.destinations) < len(
            moved_unit_refs(choice.requirement, choice.chosen)
        ):
            return False
        if len(choice.battlefields) < len(battlefield_picks(choice.requirement)):
            return False
        if len(choice.gears) < sum(g.min_count for g in gear_picks(choice.requirement)):
            return False
        if len(choice.trash) < sum(t.min_count for t in trash_picks(choice.requirement)):
            return False
        if len(choice.spells) < sum(s.min_count for s in spell_picks(choice.requirement)):
            return False
        if len(choice.locations) < len(location_picks(choice.requirement)):
            return False
        return True

    def offer_repeat_or_push(
        self, actor: RequiredTo, card: str, rounds: list[list[str]]
    ) -> None:
        """After a selection round finalizes, branch on the [Repeat] keyword.

        ``rounds`` holds the finalized target list for every selection round
        so far (the base cast plus each repeat). If the card has a (supported)
        Repeat cost AND it hasn't been repeated yet, pause on a repeat decision
        (``pending_spell_repeat``); otherwise push the spell straight onto the
        chain with all rounds. A non-repeat spell always arrives here with a
        single round, so this is a transparent pass-through for it.

        [Repeat] may be used AT MOST ONCE: the base cast plus a single repeat
        round (two rounds total). Once two rounds have been finalized we push
        straight to the chain instead of re-offering, so a caster can't pay to
        repeat the same spell indefinitely."""
        from .csv_data import card_repeat_cost

        cost = card_repeat_cost(card)
        already_repeated = len(rounds) >= 2
        if cost is not None and not already_repeated:
            self._game_state.pending_spell_repeat = PendingSpellRepeat(
                actor=actor, card=card, cost=cost, rounds=[list(r) for r in rounds]
            )
            return
        self._push_spell_to_chain(actor, card, rounds)

    @staticmethod
    def _unit_ref_from_token(token: str) -> tuple[str, int] | None:
        """A target token "player_1:<i>" / "player_2:<i>" → (controller, index).
        Non-unit tokens (move_dest:, bf:, gear:, trash:, spell:, location:)
        return None — they aren't relocatable units."""
        parts = token.split(":")
        if len(parts) == 2 and parts[0] in ("player_1", "player_2") and parts[1].isdigit():
            return (parts[0], int(parts[1]))
        return None

    def _target_token_uid(self, token: str) -> int | None:
        """The stable uid of the unit a target token points at right now, or
        None for a non-unit token / out-of-range ref."""
        ref = self._unit_ref_from_token(token)
        if ref is None:
            return None
        controller, idx = ref
        units = (
            self._game_state.player_1_units
            if controller == "player_1"
            else self._game_state.player_2_units
        )
        if 0 <= idx < len(units):
            return getattr(units[idx], "uid", 0) or None
        return None

    def _unit_by_uid(self, uid: int) -> tuple[str, int] | None:
        """Locate a unit by its stable uid → (controller, current index), or
        None if it has left play."""
        for controller, units in (
            ("player_1", self._game_state.player_1_units),
            ("player_2", self._game_state.player_2_units),
        ):
            for i, u in enumerate(units):
                if getattr(u, "uid", 0) == uid:
                    return (controller, i)
        return None

    def _resolve_round_targets(
        self,
        requirement: str,
        targets: list[str],
        uids: list[int | None],
        caster: str,
    ) -> tuple[list[str] | None, str]:
        """Re-resolve one resolution round's targets against CURRENT state.

        1. RE-LOCATE every unit token by its captured uid (positional indices
           may have shifted as units left play); a unit whose uid is gone is
           dropped. Non-unit tokens pass through untouched.
        2. RE-VALIDATE the surviving unit targets against the spell's original
           requirement. If they no longer satisfy it (e.g. a reaction buffed
           the unit past Gust's "3 Might or less"), the round should FIZZLE.

        Returns ``(relocated_targets, "")`` on success, or ``(None, reason)``
        on fizzle — ``reason`` being a short human explanation for the feed.
        """
        from .requirements import board_units, explain_fizzle, targets_still_satisfy

        padded = list(uids) + [None] * max(0, len(targets) - len(uids))
        relocated: list[str] = []
        for token, uid in zip(targets, padded):
            ref = self._unit_ref_from_token(token)
            if ref is None:
                relocated.append(token)  # non-unit token — keep as-is
                continue
            if uid is None:
                relocated.append(token)  # no captured identity → can't relocate
                continue
            loc = self._unit_by_uid(uid)
            if loc is None:
                continue  # unit left play → target gone, drop it
            relocated.append(f"{loc[0]}:{loc[1]}")

        if requirement:
            views = {(v.controller, v.index): v for v in board_units(self._game_state)}
            surviving = []
            for token in relocated:
                ref = self._unit_ref_from_token(token)
                if ref is None:
                    continue
                v = views.get((ref[0], ref[1]))
                if v is not None:
                    surviving.append(v)
            if not targets_still_satisfy(requirement, surviving, caster):
                return None, explain_fizzle(requirement, surviving, caster)
        return relocated, ""

    def _push_spell_to_chain(
        self, actor: RequiredTo, card: str, rounds: list[list[str]]
    ) -> None:
        """Put a just-played spell onto the chain and (re)open priority.

        ``rounds`` is one target list per resolution the spell will get: index
        0 is the base cast, indices 1+ are paid-for [Repeat] rounds. The base
        targets become ``ChainItem.targets``; the extras ride along as
        ``repeat_targets`` (recorded for when spell effects exist — today the
        spell still resolves as a single chain item).

        The newest spell goes to the FRONT (``items[0]`` = top of chain).
        Priority lands on the caster and the consecutive-pass counter resets
        — exactly the "the player who cast the spell gains priority" rule,
        whether this is the opening cast or a Reaction response.

        The spell is ALSO dropped into the caster's spell pile right now so
        it shows in the spell overlay the moment it's cast (not only after
        the chain resolves). The chain just tracks the open priority window;
        ``_resolve_chain`` closes it without touching the pile."""
        # Make sure every unit in play has a uid, then capture the uid behind
        # each chosen unit target so the spell can re-locate them at resolution
        # even if positional indices shifted (units left play in between).
        self._backfill_unit_uids()
        from .csv_data import card_spell_requirement_of

        requirement = card_spell_requirement_of(card) or ""
        base = list(rounds[0]) if rounds else []
        repeats = [list(r) for r in rounds[1:]] if rounds else []
        base_uids = [self._target_token_uid(t) for t in base]
        repeat_uids = [[self._target_token_uid(t) for t in r] for r in repeats]
        item = ChainItem(
            actor=actor,
            card=card,
            targets=base,
            repeat_targets=repeats,
            requirement=requirement,
            target_uids=base_uids,
            repeat_target_uids=repeat_uids,
            cid=self._take_chain_cid(),
        )
        chain = self._game_state.pending_chain
        if chain is None:
            self._game_state.pending_chain = PendingChain(
                items=[item], priority=actor, consecutive_passes=0
            )
        else:
            chain.items.insert(0, item)
            chain.priority = actor
            chain.consecutive_passes = 0

        # Per-turn cast index across BOTH players, so the shared overlay can
        # stack spells in true chronological order. Computed BEFORE the append.
        order = len(self._game_state.player_1_spells) + len(
            self._game_state.player_2_spells
        )
        spells = (
            self._game_state.player_1_spells
            if actor == RequiredTo.PLAYER_1
            else self._game_state.player_2_spells
        )
        spells.append(
            PlayedSpell(card=card, targets=base, order=order, repeat_targets=repeats)
        )
        # NOTE: spell-play triggers ("when you play a spell…") fire when the
        # spell RESOLVES, not here at cast time — see _execute_chain_item.
        # House rule: the spell gets its reaction window alone; only after it
        # resolves does the trigger hit the chain with its own window.

    def _resolve_chain(self) -> None:
        """Resolve the TOP (most recent) item of the chain — LIFO, one at a
        time. Both players passing in a row resolves only that single top
        spell; the chain shrinks by one. If items remain, the OWNER of the
        new top item gains priority (they may respond to it), and the
        consecutive-pass counter resets. When the last item resolves, the
        chain closes and play returns to the action turn (or showdown) where
        it left off.

        Cast spells already sit in their casters' piles (added at cast time,
        see ``_push_spell_to_chain``) and still have no per-spell effect, so
        resolving a spell item just removes it. A TRIGGERED-ABILITY item
        (``ChainItem.effect`` set), however, RUNS its effects here via the
        effect registry — this is where "add the effect to the chain" pays
        off."""
        chain = self._game_state.pending_chain
        if chain is None:
            return
        if chain.items:
            resolved = chain.items.pop(0)  # resolve the most-recently-added item
            self._execute_chain_item(resolved)
        if chain.items:
            # A new top item is revealed — its owner gets priority to respond.
            chain.priority = chain.items[0].actor
            chain.consecutive_passes = 0
        else:
            self._game_state.pending_chain = None
        # Resolving an item may itself fire events (e.g. an effect kills a
        # unit). Push any it produced before play continues.
        self._drain_triggers()

    def _execute_chain_item(self, item: "ChainItem") -> None:
        """Run a resolving chain item. A SPELL item has no engine effect yet,
        but its RESOLUTION is when "when you play a spell" triggers fire —
        the spell gets its reaction window alone, and only once it resolves
        does the trigger land on the chain (with its own window, via the
        drain at the end of ``_resolve_chain``). A triggered-ability item
        runs each of its effect codes through the effect registry, logging
        what resolved (or that a code is not yet implemented)."""
        eff = item.effect
        if eff is None:
            if item.card is not None:
                # Run the spell's OWN tagged effects against its chosen
                # target(s) — one pass per [Repeat] round — then discard it.
                self._run_spell_effects(item)
                # A resolved spell goes to its caster's TRASH (the discard
                # pile shown next to the main deck), like any spent card.
                gs = self._game_state
                if item.actor is RequiredTo.PLAYER_1:
                    gs.player_1_trash.append(item.card)
                else:
                    gs.player_2_trash.append(item.card)
                self._log_event("event", f"{item.card} resolved → trash")
                self._emit(
                    GameEvent(
                        kind="ON_PLAY_SPELL",
                        controller=item.actor.value,
                        source=None,
                        # Card name rides along so a "when a spell is played"
                        # trigger can reference WHICH spell set it off.
                        data={"card": item.card},
                    )
                )
            return
        try:
            controller = RequiredTo(eff.controller)
        except ValueError:
            return
        # Effect items can carry chosen targets (activated abilities pick a
        # target up front, e.g. Heart of Dark Ice → a unit). Relocate them by
        # uid at resolution; if they no longer satisfy the requirement (left
        # play / changed), the effect fizzles. Triggered abilities have no
        # pre-chosen targets, so this is a no-op for them.
        targets: list[str] = []
        if item.targets:
            resolved, reason = self._resolve_round_targets(
                item.requirement, list(item.targets), list(item.target_uids), controller.value
            )
            if resolved is None:
                self._log_event("fizzle", f"{item.label or eff.source} did nothing — {reason}")
                return
            targets = resolved
        self._run_effect_codes(
            controller=controller,
            source=eff.source,
            trigger=eff.trigger,
            event_kind=eff.event_kind,
            label=item.label or eff.source or "",
            codes=list(eff.effects),
            targets=targets,
        )

    def _run_spell_effects(self, item: "ChainItem") -> None:
        """Resolve a cast spell's tagged effects against the units it targeted.

        A spell's taxonomy ability has no trigger — it's the card's own
        effect — so we gather every ``activeEffect`` across the spell's
        abilities and run them with the chosen ``targets``. With [Repeat],
        each extra paid round (``repeat_targets``) re-runs the same effects
        against that round's targets, so e.g. Frigid Touch stacks -2 Might
        per round. Unknown effect codes fall through to the registry's
        logged no-op, exactly like triggered abilities."""
        codes: list[str] = []
        for ability in _abilities.triggered_abilities_for(item.card or ""):
            codes.extend(ability.active_effects)
        if not codes:
            return
        # One (targets, captured-uids) pair per resolution round (base + each
        # [Repeat]). Each round is re-located by uid and re-validated against
        # the requirement at the moment IT resolves.
        rounds: list[tuple[list[str], list[int | None]]] = [
            (list(item.targets), list(item.target_uids))
        ]
        for rt, ru in zip(
            item.repeat_targets,
            list(item.repeat_target_uids) + [[]] * len(item.repeat_targets),
        ):
            rounds.append((list(rt), list(ru)))
        caster = item.actor.value
        for picks, uids in rounds:
            resolved, reason = self._resolve_round_targets(item.requirement, picks, uids, caster)
            if resolved is None:
                # The chosen target no longer satisfies the requirement (buffed
                # past Gust's 3-Might cap, or removed in reaction). Only the
                # TARGETED effects fizzle — any UNtargeted effect in the same
                # ability still resolves (e.g. Stupefy's "Draw 1" happens even
                # though the "-1 Might" had no unit to land on). If EVERY effect
                # was targeted, the whole thing did nothing.
                untargeted = [c for c in codes if not _effects.effect_uses_targets(c)]
                self._log_event("fizzle", f"{item.card} did nothing — {reason}")
                if untargeted:
                    self._run_effect_codes(
                        controller=item.actor,
                        source=None,
                        trigger="",
                        event_kind="",
                        label=item.card or "",
                        codes=untargeted,
                        targets=[],
                    )
                continue
            self._run_effect_codes(
                controller=item.actor,
                source=None,
                trigger="",
                event_kind="",
                label=item.card or "",
                codes=codes,
                targets=resolved,
            )

    def _run_effect_codes(
        self,
        controller: RequiredTo,
        source: str | None,
        trigger: str,
        event_kind: str,
        label: str,
        codes: list[str],
        targets: list[str] | None = None,
    ) -> None:
        """Run an ability's effect codes in order.

        Instant codes dispatch through the effect registry as before. A
        CHOICE code (see ``effects`` choice registry) with at least one valid
        option PAUSES execution: ``pending_effect_choice`` is set (carrying
        the not-yet-run remainder of ``codes``) and the method returns — the
        ``play:choose_effect_target`` handler applies the pick and calls back
        in here with the remainder. A choice code with NO valid options
        fizzles as a logged no-op and execution continues.
        """
        for i, code in enumerate(codes):
            ctx = _effects.EffectContext(
                engine=self,
                controller=controller,
                source=source,
                code=code,
                trigger=trigger,
                event_kind=event_kind,
                targets=tuple(targets or ()),
            )
            line_label = label or source or code
            if _effects.is_choice_effect(code):
                options = _effects.choice_effect_options(ctx)
                if options:
                    self._game_state.pending_effect_choice = PendingEffectChoice(
                        actor=controller,
                        code=code,
                        source=source,
                        source_card=self._card_name_for_ref(source),
                        options=options,
                        remaining_effects=list(codes[i + 1 :]),
                        label=line_label,
                        trigger=trigger,
                        event_kind=event_kind,
                        optional=_effects.choice_effect_optional(code),
                        continuation_targets=tuple(targets or ()),
                    )
                    self._log_event(
                        "effect", f"{line_label}: {controller.value} to choose"
                    )
                    return
                # No options to pick. Some choice effects still have a
                # non-choice half to run (e.g. DISCARD_1_DRAW_1 with an empty
                # hand still draws) — that's the on_empty fallback.
                empty_line = _effects.run_choice_effect_on_empty(ctx)
                if empty_line is not None:
                    self._log_event("effect", f"{line_label}: {empty_line}")
                else:
                    self._log_event("effect", f"{line_label}: {code} (no valid target)")
                continue
            ran = _effects.execute_effect(ctx)
            if ran:
                self._log_event("effect", f"{line_label}: {code}")
            else:
                self._log_event("effect", f"{line_label}: {code} (not implemented)")

    def _card_name_for_ref(self, ref: str | None) -> str | None:
        """The card NAME behind an ability-source ref — a battlefield slot
        ("battlefield_1"), a unit ref ("player_1:0"), or a standalone-gear ref
        ("gear:player_1:0")."""
        gs = self._game_state
        if ref == "battlefield_1":
            return gs.battlefield_1
        if ref == "battlefield_2":
            return gs.battlefield_2
        if ref and ref.startswith("gear:"):
            parts = ref.split(":")
            if len(parts) == 3 and parts[2].isdigit():
                gears = gs.player_1_gears if parts[1] == "player_1" else gs.player_2_gears
                i = int(parts[2])
                return gears[i].card if 0 <= i < len(gears) else None
            return None
        if ref and ":" in ref:
            side, _, idx_s = ref.partition(":")
            units = (
                gs.player_1_units
                if side == RequiredTo.PLAYER_1.value
                else gs.player_2_units if side == RequiredTo.PLAYER_2.value else None
            )
            try:
                idx = int(idx_s)
            except ValueError:
                return None
            if units is not None and 0 <= idx < len(units):
                return units[idx].card
        return None

    # ----------------------------------------------------------------- #
    # Trigger / event plumbing
    # ----------------------------------------------------------------- #
    def _log_event(self, kind: str, text: str) -> None:
        """Append a line to the on-screen trigger/effect feed."""
        self._game_state.event_feed.append(
            EventLogEntry(sequence=len(self._game_state.event_feed), kind=kind, text=text)
        )

    def _abilities_in_play(self):
        """Yield ``(ref, controller, location, card, ability)`` for every
        triggered ability on a unit currently in play (both players), AND on
        the two battlefield cards themselves (e.g. Abandoned Hall's "when a
        player plays a spell…")."""
        gs = self._game_state
        for side, units in (
            (RequiredTo.PLAYER_1, gs.player_1_units),
            (RequiredTo.PLAYER_2, gs.player_2_units),
        ):
            for idx, unit in enumerate(units):
                for ability in _abilities.triggered_abilities_for(unit.card):
                    if not ability.triggers or not ability.active_effects:
                        continue
                    yield (f"{side.value}:{idx}", side.value, unit.location, unit.card, ability)
        # Battlefield cards. They aren't owned by either player, so their
        # "controller" is the battlefield's current holder when there is one
        # (None otherwise) — ANY-scoped triggers don't care, FRIENDLY-scoped
        # ones ("while you control this battlefield…") match the holder. The
        # ref AND location are the slot itself ("battlefield_1") so HERE-scoped
        # triggers and effect handlers can address "here".
        for slot, name, holder in (
            ("battlefield_1", gs.battlefield_1, gs.battlefield_1_controller),
            ("battlefield_2", gs.battlefield_2, gs.battlefield_2_controller),
        ):
            if not name:
                continue
            for ability in _abilities.triggered_abilities_for(name):
                if not ability.triggers or not ability.active_effects:
                    continue
                controller = holder.value if holder is not None else None
                yield (slot, controller, slot, name, ability)
        # Attached EQUIPMENT. An effect-text equipment ability appends to its
        # host unit's rules, so its triggers fire as if on that unit. The ref is
        # a composite ``equip:<gear-controller>:<gear-index>:<host-uid>`` so an
        # effect can address both the gear ("this" — UNATTACH_THIS) and the
        # equipped unit by stable uid ("self" — DEAL_N_SELF), order-independent.
        for side, gears in (
            (RequiredTo.PLAYER_1, gs.player_1_gears),
            (RequiredTo.PLAYER_2, gs.player_2_gears),
        ):
            for gidx, gear in enumerate(gears):
                if not gear.attached_uid:
                    continue
                host = self._unit_by_uid(gear.attached_uid)  # relocate by stable uid
                if host is None:
                    continue  # host left play → gear is effectively unattached
                host_units = gs.player_1_units if host[0] == "player_1" else gs.player_2_units
                host_unit = host_units[host[1]]
                for ability in _abilities.triggered_abilities_for(gear.card):
                    if not ability.effect_text:
                        continue
                    if not ability.triggers or not ability.active_effects:
                        continue
                    yield (
                        f"equip:{side.value}:{gidx}:{gear.attached_uid}",
                        side.value,
                        host_unit.location,
                        gear.card,
                        ability,
                    )
        # Standalone GEAR rule-text triggered abilities (e.g. Chemtech Cask's
        # "when you play a spell on an opponent's turn, you may exhaust me to
        # play a Gold token"). Unlike the effect-text equipment above, these are
        # the gear's OWN ability (not appended to a host), so they fire for the
        # gear's controller. Source ref ``gear:<controller>:<index>`` lets an
        # EXHAUST_THIS cost exhaust the gear itself.
        for side, gears in (
            (RequiredTo.PLAYER_1, gs.player_1_gears),
            (RequiredTo.PLAYER_2, gs.player_2_gears),
        ):
            for gidx, gear in enumerate(gears):
                for ability in _abilities.triggered_abilities_for(gear.card):
                    if ability.effect_text:
                        continue  # attached-equipment text handled above
                    if not ability.triggers or not ability.active_effects:
                        continue
                    yield (
                        f"gear:{side.value}:{gidx}",
                        side.value,
                        gear.location,
                        gear.card,
                        ability,
                    )

    def _death_replacement_for(self, unit: "PlayedUnit") -> tuple[str, ...]:
        """The would-die REPLACEMENT effects that apply to ``unit``, or ``()``.

        A replacement effect (trigger ``IF_ID_DIE``) intercedes when the unit
        WOULD die and substitutes its own outcome instead — the unit never
        actually dies. We look in two places:

          * the unit's OWN rule-text abilities (a unit that saves itself), and
          * any EFFECT-TEXT equipment attached to it (e.g. the Guardian Angel
            gear, whose "if I'd die" text applies to its equipped host).

        ``IF_ID_DIE`` is deliberately NOT in ``TRIGGER_EVENT_MAP`` — it is a
        replacement marker, not a chain trigger, so it never fires an item onto
        the chain; it is consulted only here, at the moment of death."""
        from .abilities import triggered_abilities_for

        for ability in triggered_abilities_for(unit.card):
            if not ability.effect_text and "IF_ID_DIE" in ability.triggers and ability.active_effects:
                return ability.active_effects
        gs = self._game_state
        for gears in (gs.player_1_gears, gs.player_2_gears):
            for gear in gears:
                if gear.attached_uid is None or gear.attached_uid != unit.uid:
                    continue
                for ability in triggered_abilities_for(gear.card):
                    if ability.effect_text and "IF_ID_DIE" in ability.triggers and ability.active_effects:
                        return ability.active_effects
        return ()

    def _recall_instead_of_death(self, controller: str, unit: "PlayedUnit") -> None:
        """Apply a HEAL_EXHAUST_RECALL would-die replacement: the unit does NOT
        die (no ON_DEATH, nothing to trash). HEAL is a no-op (this game keeps no
        persistent damage); the unit's temporary buffs and exhaust state drop as
        it leaves play; RECALL returns the card to its owner's hand.

        The caller is responsible for removing ``unit`` from its units list;
        this only handles the destination (hand) and the feed line."""
        unit.bonus_might = 0
        unit.bonus_shield = 0
        unit.exhausted = False
        hand = (
            self._game_state.player_1_hand
            if controller == "player_1"
            else self._game_state.player_2_hand
        )
        if hand is not None:
            hand.append(unit.card)
        self._log_event(
            "effect",
            f"{unit.card} would die — healed, exhausted and recalled to its owner's hand",
        )

    def _kill_unit(self, controller: str, index: int, battlefield: str | None = None) -> None:
        """Remove the unit at (controller, index) from play and send it to its
        owner's trash, firing ON_DEATH first (so deathknell / "when a unit dies"
        watchers still see it). Used by non-combat lethal effects (e.g. an
        equipment's end-of-turn self-damage).

        If the unit has a would-die REPLACEMENT (e.g. an attached Guardian
        Angel), it is recalled to hand instead — no death, no ON_DEATH."""
        units = (
            self._game_state.player_1_units
            if controller == "player_1"
            else self._game_state.player_2_units
            if controller == "player_2"
            else None
        )
        if units is None or not (0 <= index < len(units)):
            return
        if self._death_replacement_for(units[index]):
            self._recall_instead_of_death(controller, units.pop(index))
            return
        self._emit(
            GameEvent(
                kind="ON_DEATH",
                controller=controller,
                source=f"{controller}:{index}",
                battlefield=battlefield,
            )
        )
        dead = units.pop(index)
        trash = (
            self._game_state.player_1_trash
            if controller == "player_1"
            else self._game_state.player_2_trash
        )
        trash.append(dead.card)

    def _emit(self, event: GameEvent) -> None:
        """Announce a state transition. Scans cards in play RIGHT NOW for
        triggered abilities that respond to ``event`` and queues them (they're
        pushed onto the chain by ``_drain_triggers``). Scanning at emit time —
        not at drain — means an ability on a unit that's about to leave play
        (e.g. a DEATHKNELL emitted just before the unit is removed) is still
        found."""
        self._log_event("event", self._describe_event(event))
        for ref, controller, location, card, ability in self._abilities_in_play():
            matched = [
                t
                for t in ability.triggers
                if trigger_matches(
                    t,
                    event,
                    owner_controller=controller,
                    owner_location=location,
                    owner_ref=ref,
                )
            ]
            if not matched:
                continue
            # Battlefield-sourced abilities phrase their effect around the
            # player the EVENT was about ("when a player plays a spell, THEY
            # may give a unit THEY control here…"), so the resolved effect
            # belongs to the event's controller — fall back to the holder.
            # A controller-less match (unheld battlefield + controller-less
            # event) can't resolve; skip it.
            effect_controller = (
                (event.controller or controller)
                if ref in ("battlefield_1", "battlefield_2")
                else controller
            )
            if effect_controller is None:
                continue
            self._trigger_queue.append(
                TriggeredEffect(
                    controller=effect_controller,
                    source=ref,
                    trigger=matched[0],
                    event_kind=event.kind,
                    effects=tuple(ability.active_effects),
                    conditions=tuple(ability.conditions),
                    costs=tuple(ability.costs),
                    label=f"{card} — {matched[0]}",
                    # The card the event was about (e.g. the resolved spell
                    # for ON_PLAY_SPELL) — surfaced in the chain UI.
                    context_card=event.data.get("card"),
                )
            )

    @staticmethod
    def _describe_event(event: GameEvent) -> str:
        bits = [event.kind]
        if event.controller:
            bits.append(event.controller)
        if event.battlefield:
            bits.append(f"@{event.battlefield}")
        return " ".join(bits)

    def _resolve_exhaustable_source(self, ref: str | None):
        """The in-play unit or gear behind an ability's ``source`` ref, if it has
        an exhaust state — used to pay an ``EXHAUST_THIS`` cost. Handles unit
        refs (``"player_1:0"``) and standalone-gear refs (``"gear:player_1:0"``).
        Battlefields (no exhaust state) and missing sources return None."""
        if not ref or ref in ("battlefield_1", "battlefield_2"):
            return None
        gs = self._game_state
        if ref.startswith("gear:"):
            parts = ref.split(":")
            if len(parts) == 3 and parts[2].isdigit():
                gears = gs.player_1_gears if parts[1] == "player_1" else gs.player_2_gears
                i = int(parts[2])
                return gears[i] if 0 <= i < len(gears) else None
            return None
        parts = ref.split(":")
        if len(parts) == 2 and parts[0] in ("player_1", "player_2") and parts[1].isdigit():
            units = gs.player_1_units if parts[0] == "player_1" else gs.player_2_units
            i = int(parts[1])
            return units[i] if 0 <= i < len(units) else None
        return None

    def _activated_ability_at(self, source_ref: str | None):
        """The EXHAUST_THIS-only activated ability on the permanent at
        ``source_ref`` (unit or gear), or None. Energy/rune-cost activated
        abilities (e.g. The Syren) are deferred, so they're not returned."""
        from .abilities import activated_abilities_for

        card = self._card_name_for_ref(source_ref)
        if not card:
            return None
        for ab in activated_abilities_for(card):
            if tuple(ab.costs) == ("EXHAUST_THIS",):
                return ab
        return None

    @staticmethod
    def _derive_activation_requirement(effects: tuple[str, ...]) -> str:
        """The target requirement an activated ability's effects imply, in the
        same grammar spells use. Today only ``GIVE_UNIT_±NM`` needs a target
        ("a unit"); untargeted effects return "" (no pick)."""
        import re as _re

        for e in effects:
            if _re.fullmatch(r"GIVE_UNIT_[+-]\d+M", e):
                return "ANY UNIT (1)"
        return ""

    def _push_activated_effect(
        self,
        actor: RequiredTo,
        source: str | None,
        effects: tuple[str, ...],
        targets: list[str],
        target_uids: list[int | None],
        requirement: str,
    ) -> None:
        """Put an activated ability's effect on the chain as an EFFECT item
        carrying its chosen targets (so it resolves like a triggered ability,
        but with pre-picked targets). The cost was already paid by the caller."""
        from .triggers import TriggeredEffect

        label = f"{self._card_name_for_ref(source) or 'ability'} — activated"
        te = TriggeredEffect(
            controller=actor.value,
            source=source,
            trigger="ACTIVATED",
            event_kind="ACTIVATED",
            effects=tuple(effects),
            label=label,
        )
        item = ChainItem(
            actor=actor,
            card=None,
            targets=list(targets),
            effect=te,
            label=label,
            requirement=requirement,
            target_uids=list(target_uids),
            cid=self._take_chain_cid(),
        )
        chain = self._game_state.pending_chain
        if chain is None:
            self._game_state.pending_chain = PendingChain(
                items=[item], priority=actor, consecutive_passes=0
            )
        else:
            chain.items.insert(0, item)
            chain.priority = actor
            chain.consecutive_passes = 0
        self._log_event("trigger", f"{label} → chain")

    def _drain_triggers(self) -> None:
        """Push every queued triggered ability onto the chain, then clear the
        queue. APNAP order: the active player's triggers go on FIRST so they
        end up BELOW the opponent's and resolve LAST (LIFO). Each push opens
        or extends the chain via the same priority machinery spells use.

        A triggered ability with a payable cost (``EXHAUST_THIS``) does NOT go
        straight on the chain: its controller is first asked whether to pay
        (PendingAbilityCost). The effect only reaches the chain if they pay —
        so we pause here, stash the rest of the queue, and resume after the
        decision. A source that's already exhausted / gone can't pay, so the
        ability is simply dropped."""
        if self._game_state.pending_ability_cost is not None:
            return  # a cost decision owns the clock; resume after it resolves
        if not self._trigger_queue:
            return
        active = self._game_state.current_player

        def _order(te: TriggeredEffect) -> int:
            return 0 if te.controller == getattr(active, "value", active) else 1

        queue = sorted(self._trigger_queue, key=_order)
        self._trigger_queue = []
        for i, te in enumerate(queue):
            if "EXHAUST_THIS" in te.costs:
                src = self._resolve_exhaustable_source(te.source)
                if src is None or src.exhausted:
                    # Can't pay (source gone or already exhausted) → no effect.
                    self._log_event("event", f"{te.label} — can't pay [exhaust], skipped")
                    continue
                try:
                    actor = RequiredTo(te.controller)
                except ValueError:
                    continue
                self._game_state.pending_ability_cost = PendingAbilityCost(
                    actor=actor,
                    source=te.source,
                    source_card=self._card_name_for_ref(te.source),
                    trigger=te.trigger,
                    event_kind=te.event_kind,
                    effects=tuple(te.effects),
                    conditions=tuple(te.conditions),
                    context_card=te.context_card,
                    label=te.label,
                )
                self._trigger_queue = queue[i + 1 :]  # resume these after the decision
                return
            self._push_effect_to_chain(te)

    def _push_effect_to_chain(self, te: TriggeredEffect) -> None:
        """Place a triggered ability on top of the chain, handing priority to
        its controller (the same window spells use to respond)."""
        try:
            actor = RequiredTo(te.controller)
        except ValueError:
            return
        item = ChainItem(
            actor=actor, card=None, targets=[], effect=te, label=te.label,
            cid=self._take_chain_cid(),
        )
        chain = self._game_state.pending_chain
        if chain is None:
            self._game_state.pending_chain = PendingChain(
                items=[item], priority=actor, consecutive_passes=0
            )
        else:
            chain.items.insert(0, item)
            chain.priority = actor
            chain.consecutive_passes = 0
        self._log_event("trigger", f"{te.label} → chain")

    def _take_chain_cid(self) -> int:
        """Hand out the next stable chain-item cid (monotonic per game)."""
        cid = self._game_state.next_chain_cid
        self._game_state.next_chain_cid += 1
        return cid

    def _chain_item_by_cid(self, cid: int):
        """The (index, ChainItem) on the chain with stable id ``cid``, or
        ``(-1, None)`` if it's no longer there (resolved or already removed)."""
        chain = self._game_state.pending_chain
        if chain is not None:
            for i, it in enumerate(chain.items):
                if it.cid == cid:
                    return i, it
        return -1, None

    def _reaction_play_options(self, actor: RequiredTo) -> list[str]:
        """``play:play_spell:<i>`` for each Reaction spell ``actor`` could play
        in response on the chain: a Spell with the [Reaction] keyword that's
        affordable from their CURRENT pools and whose Spell Choice Requirement
        is satisfiable. Duplicate names collapse to the leftmost copy."""
        from .csv_data import card_is_reaction
        from .requirements import spell_playable

        hand = (
            self._game_state.player_1_hand
            if actor == RequiredTo.PLAYER_1
            else self._game_state.player_2_hand
        )
        energy = self.player_energy(actor)
        seen: set[str] = set()
        out: list[str] = []
        for i, card in enumerate(hand or []):
            if card in seen:
                continue
            if card_type_of(card) != "Spell":
                continue
            if not card_is_reaction(card):
                continue
            if self.card_energy_cost(card) > energy:
                continue
            if not self.can_afford_power_cost(actor, card):
                continue
            if not spell_playable(self._game_state, card, caster=actor):
                continue
            seen.add(card)
            out.append(f"play:play_spell:{i}")
        return out

    def _assign_damage_options(self, actor: RequiredTo) -> list[str]:
        """Enumerate every valid ``play:assign_damage:<csv>`` a player can
        submit for the current PendingCombat.

        The payload is the set of OPPONENT unit indices the player assigns
        LETHAL damage to (the units they kill). A set ``S`` is valid iff:

          * ``sum(Might of S) <= budget`` (the player's total Might at the
            BF — you can't assign more damage than you have), and
          * ``budget - sum(Might of S) < min(Might of survivors)`` — i.e.
            the leftover can't finish off any remaining enemy unit, so the
            kill is *maximal* (you must assign lethal before moving on and
            can't waste damage that could kill another unit).

        With no survivors the second clause is vacuously true (you killed
        everything you could afford). The empty set is therefore only
        offered when the budget can't kill the cheapest enemy unit.

        Returns ``[]`` if there's no active combat or the actor isn't
        player_1 / player_2. 2^N enumeration is fine — N (enemy units at a
        single BF) stays small in practice.
        """
        import itertools
        from .csv_data import card_might_of
        combat = self._game_state.pending_combat
        if combat is None:
            return []
        if actor == RequiredTo.PLAYER_1:
            opponent_units = self._game_state.player_2_units
            opp_controller = "player_2"
            budget = combat.player_1_might
        elif actor == RequiredTo.PLAYER_2:
            opponent_units = self._game_state.player_1_units
            opp_controller = "player_1"
            budget = combat.player_2_might
        else:
            return []

        # (opponent-unit-index, lethal-cost). Lethal cost is the unit's
        # EFFECTIVE Might (printed Might + bonus_might from buffs), floored at
        # 1: a 0-Might unit still needs 1 damage to die, so it can't be killed
        # for free. Crucially this must include bonus_might — the damage BUDGET
        # (might_at_battlefield / combat.player_X_might) already counts buffs,
        # so the lethal threshold must too, else a buffed unit (e.g. 3 printed
        # + 3 buff = 6 Might) would wrongly die to only its printed Might worth
        # of damage.
        targets: list[tuple[int, int]] = []
        for i, u in enumerate(opponent_units):
            if u.location != combat.battlefield:
                continue
            if card_might_of(u.card) is None:
                continue
            # Lethal cost = CURRENT Might (printed + buffs + attached equipment),
            # floored at 1. Uses the same source of truth as the damage budget.
            targets.append((i, max(self.effective_unit_might(opp_controller, i), 1)))

        options: list[str] = []
        for size in range(0, len(targets) + 1):
            for combo in itertools.combinations(targets, size):
                spent = sum(m for _, m in combo)
                if spent > budget:
                    continue
                leftover = budget - spent
                chosen = {i for i, _ in combo}
                survivors = [m for i, m in targets if i not in chosen]
                # Maximal-kill rule: leftover must be unable to kill any
                # surviving enemy unit, else the player was REQUIRED to
                # assign lethal to one of them too.
                if survivors and leftover >= min(survivors):
                    continue
                indices = sorted(chosen)
                options.append(
                    "play:assign_damage:" + ",".join(str(i) for i in indices)
                )
        return options

    def effective_unit_might(self, controller: str, index: int) -> int:
        """A unit's CURRENT Might — the single source of truth used everywhere
        Might matters (combat budget + lethal cost, spell targeting, debuff
        floors). It's the printed Might plus persistent ``bonus_might`` buffs
        plus any continuous Might granted by EFFECT-TEXT equipment attached to
        it (e.g. an Equipment with ``UNIT_ATTACHED_+2M``). Equipment Might is
        computed live, so it disappears the moment the gear is unattached."""
        from .abilities import attached_might_bonus, attached_shield_bonus
        from .csv_data import card_might_of, card_printed_shield

        units = (
            self._game_state.player_1_units
            if controller == "player_1"
            else self._game_state.player_2_units
            if controller == "player_2"
            else []
        )
        if index < 0 or index >= len(units):
            return 0
        unit = units[index]
        gears = (
            self._game_state.player_1_gears
            if controller == "player_1"
            else self._game_state.player_2_gears
        )
        turn = self._game_state.total_turn_number
        might = (
            (card_might_of(unit.card) or 0)
            + unit.bonus_might
            + attached_might_bonus(gears, unit.uid, turn)
        )
        # [Shield] adds Might ONLY while this unit is DEFENDING (printed keyword
        # + equipment SHIELD_N + this-turn granted bonus_shield, all stacking).
        if self._unit_is_defending(controller, unit.location):
            might += (
                card_printed_shield(unit.card)
                + unit.bonus_shield
                + attached_shield_bonus(gears, unit.uid, turn)
            )
        return might

    def _unit_is_defending(self, controller: str, location: str) -> bool:
        """True if ``controller``'s unit at ``location`` is currently DEFENDING:
        there's an active showdown (or its combat step) AT that battlefield and
        ``controller`` is NOT the initiator (the attacker who moved in). The
        defender is the side that held the battlefield."""
        gs = self._game_state
        sd = gs.pending_showdown
        if sd is not None and sd.battlefield == location:
            return controller != sd.initiator.value
        pc = gs.pending_combat
        if pc is not None and pc.battlefield == location and pc.initiator is not None:
            return controller != pc.initiator.value
        return False

    def might_at_battlefield(self, actor: RequiredTo, battlefield: str) -> int:
        """Sum of CURRENT Might of all of ``actor``'s units sitting at
        ``battlefield`` (printed + buffs + attached-equipment grants). Used to
        compute each player's damage budget when a contested showdown enters
        its combat phase."""
        if actor not in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
            return 0
        units = (
            self._game_state.player_1_units
            if actor == RequiredTo.PLAYER_1
            else self._game_state.player_2_units
        )
        total = 0
        for i, unit in enumerate(units):
            if unit.location != battlefield:
                continue
            total += self.effective_unit_might(actor.value, i)
        return total

    def resolve_combat(self, battlefield: str) -> None:
        """Apply the kills committed during a ``PendingCombat`` for
        ``battlefield`` and clear the pending state.

        Both players' ``player_X_targets`` lists must already be set
        (the commit_kills handler ensures this before calling here).
        Targets are interpreted as indices into the OPPONENT's
        ``player_X_units`` at the moment combat opened, so we apply
        all kills SIMULTANEOUSLY — we collect both target sets first
        and only then prune both unit lists, which means each side's
        targets refer to the same indices they validated against.

        Indices that fell out of range (because a unit array shrank
        between commit and resolve via some out-of-band path) are
        silently skipped — defensive only; the action handler
        rejects invalid indices on commit.
        """
        gs = self._game_state
        pc = gs.pending_combat
        if pc is None or pc.battlefield != battlefield:
            return
        p1_targets = set(pc.player_1_targets or [])  # P2 units P1 killed
        p2_targets = set(pc.player_2_targets or [])  # P1 units P2 killed

        # Split each casualty set into units that TRULY die vs units saved by a
        # would-die REPLACEMENT (e.g. an attached Guardian Angel → recall). A
        # replaced unit never dies: no ON_DEATH, no trash — it returns to hand.
        def _split(units: list, targets: set[int]) -> tuple[set[int], set[int]]:
            dead, replaced = set(), set()
            for i in targets:
                if 0 <= i < len(units):
                    (replaced if self._death_replacement_for(units[i]) else dead).add(i)
            return dead, replaced

        p1_dead, p1_replaced = _split(gs.player_1_units, p2_targets)  # P1's units P2 hit
        p2_dead, p2_replaced = _split(gs.player_2_units, p1_targets)  # P2's units P1 hit

        # Fire death triggers BEFORE pruning, while the dying units are still in
        # play — so a unit's own DEATHKNELL (and "when a unit dies" watchers)
        # can still see it. ONLY the truly-dead emit ON_DEATH (a replaced unit
        # didn't die). Each death is its own event, tagged with the dead unit's
        # owner + ref and the battlefield it fell on.
        for i in p1_dead:
            self._emit(
                GameEvent(
                    kind="ON_DEATH",
                    controller=RequiredTo.PLAYER_1.value,
                    source=f"{RequiredTo.PLAYER_1.value}:{i}",
                    battlefield=battlefield,
                )
            )
        for i in p2_dead:
            self._emit(
                GameEvent(
                    kind="ON_DEATH",
                    controller=RequiredTo.PLAYER_2.value,
                    source=f"{RequiredTo.PLAYER_2.value}:{i}",
                    battlefield=battlefield,
                )
            )
        # Apply simultaneously: snapshot both lists first. Truly-dead units →
        # owner's trash (index order); replaced units → recalled to hand; then
        # both leave play entirely.
        gs.player_2_trash.extend(
            u.card for i, u in enumerate(gs.player_2_units) if i in p2_dead
        )
        gs.player_1_trash.extend(
            u.card for i, u in enumerate(gs.player_1_units) if i in p1_dead
        )
        for i, u in enumerate(gs.player_1_units):
            if i in p1_replaced:
                self._recall_instead_of_death(RequiredTo.PLAYER_1.value, u)
        for i, u in enumerate(gs.player_2_units):
            if i in p2_replaced:
                self._recall_instead_of_death(RequiredTo.PLAYER_2.value, u)
        gs.player_2_units = [
            u for i, u in enumerate(gs.player_2_units) if i not in p1_targets
        ]
        gs.player_1_units = [
            u for i, u in enumerate(gs.player_1_units) if i not in p2_targets
        ]
        gs.pending_combat = None

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

    def can_afford_equip_cost(self, actor: RequiredTo, cost: dict[str, object]) -> bool:
        """True iff ``actor`` can pay an ``[Equip]`` cost (``card_equip_cost``
        shape: energy + per-domain power + any-type power). Specific-domain
        requirements are reserved first; the any-type requirement draws from
        whatever Power remains across all domains."""
        if self.player_energy(actor) < int(cost.get("energy", 0)):
            return False
        pool = dict(self.player_power(actor))
        for domain, amount in dict(cost.get("power", {})).items():
            if pool.get(domain, 0) < amount:
                return False
            pool[domain] = pool.get(domain, 0) - amount
        return sum(pool.values()) >= int(cost.get("any_power", 0))

    def _deduct_equip_cost(self, actor: RequiredTo, cost: dict[str, object]) -> None:
        """Spend an [Equip] cost: energy, then specific-domain Power, then the
        any-type Power from whatever domains have Power left (greedy)."""
        energy = int(cost.get("energy", 0))
        if energy:
            self.add_energy(actor, -energy)
        for domain, amount in dict(cost.get("power", {})).items():
            if amount:
                self.add_power(actor, domain, -int(amount))
        remaining = int(cost.get("any_power", 0))
        if remaining <= 0:
            return
        pool = self.player_power(actor)
        for domain in list(pool.keys()):
            if remaining <= 0:
                break
            take = min(pool.get(domain, 0), remaining)
            if take:
                self.add_power(actor, domain, -take)
                remaining -= take
        if remaining > 0:
            raise ValueError("internal error: still short on any-type Power for equip")

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

    def _ready_all_gears(self, actor: RequiredTo) -> None:
        """Step A (Awake): flip every exhausted gear owned by ``actor`` back to ready.

        Gears enter ready, so this is a no-op for freshly played ones; it
        exists so a later tap-style ability that exhausts a gear gets cleared
        on the owner's next Awake, mirroring units.
        """
        if actor == RequiredTo.PLAYER_1:
            gears = self._game_state.player_1_gears
        elif actor == RequiredTo.PLAYER_2:
            gears = self._game_state.player_2_gears
        else:
            return
        for gear in gears:
            gear.exhausted = False

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
            self._ready_all_gears(actor)
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
                self.award_bf_point(actor, "battlefield_1", via="hold")
            if gs.battlefield_2_controller == actor:
                self.award_bf_point(actor, "battlefield_2", via="hold")
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
            # Single drain choke point: every action routes back through
            # start() (play handlers `return self.start()`; ABCD recurses
            # above), so flushing queued triggers onto the chain here — after
            # ABCD, before any options are built — covers triggers fired by
            # both ABCD steps and discretionary plays. The chain-priority
            # block below then surfaces them for resolution.
            self._drain_triggers()
            active = self._game_state.current_player
            showdown = self._game_state.pending_showdown
            # Only drive the showdown menu when nothing finer-grained is
            # mid-resolution. A spell played IN the showdown opens a chain
            # (or a target pick); those blocks below own the menu until the
            # spell resolves, after which we return here.
            if (
                showdown is not None
                and self._game_state.pending_chain is None
                and self._game_state.pending_spell_choice is None
                # A [Repeat] decision or a resolving effect-choice is a
                # finer-grained sub-step that must be answered before the
                # showdown menu reopens — otherwise a repeatable Action/
                # Reaction cast IN the showdown would never get its repeat
                # prompt (the showdown menu would grab the clock first).
                and self._game_state.pending_spell_repeat is None
                and self._game_state.pending_effect_choice is None
            ):
                from .csv_data import card_playable_in_showdown
                from .requirements import spell_playable

                # The FOCUS holder is on the clock. They may play an
                # [Action]/[Reaction] spell, the initiator may MUSTER more
                # units (only while the fight hasn't started), and either
                # way they can PASS FOCUS.
                focus = showdown.focus_holder
                opts: list[str] = []

                # MUSTER: only the initiator, only while they hold focus and
                # the fight hasn't started, brings ready base units onto the
                # contested battlefield.
                if focus == showdown.initiator and not showdown.locked:
                    init_units = (
                        self._game_state.player_1_units
                        if showdown.initiator == RequiredTo.PLAYER_1
                        else self._game_state.player_2_units
                    )
                    for ui, u in enumerate(init_units):
                        if not u.exhausted and u.location == "base":
                            opts.append(
                                f"play:move_unit:{ui}:{showdown.battlefield}"
                            )

                # Play an [Action]/[Reaction] spell from the focus holder's
                # hand. Doing so starts the fight (locks mustering) and opens
                # a chain. We surface a bare play_spell ONLY for spells already
                # affordable from the current pools (mirroring the normal action
                # turn). Spells that still need Energy/Power are offered instead
                # as pre-costed picker chips (player_X_intents, computed in
                # shortcuts.py for the focus holder), which expand into the
                # rune-payment + cast chain — so the focus holder never has to
                # tap runes by hand. Standalone rune actions are intentionally
                # NOT surfaced here, exactly like the action turn.
                focus_hand = (
                    self._game_state.player_1_hand
                    if focus == RequiredTo.PLAYER_1
                    else self._game_state.player_2_hand
                ) or []
                energy = self.player_energy(focus)
                seen_spell: set[str] = set()
                for hi, c in enumerate(focus_hand):
                    if c in seen_spell:
                        continue
                    if card_type_of(c) != "Spell" or not card_playable_in_showdown(c):
                        continue
                    if self.card_energy_cost(c) > energy:
                        continue
                    if not self.can_afford_power_cost(focus, c):
                        continue
                    if not spell_playable(self._game_state, c, caster=focus):
                        continue
                    seen_spell.add(c)
                    opts.append(f"play:play_spell:{hi}")

                opts.append("play:pass_showdown")
                return EngineOutput(
                    game_state=self.game_state,
                    player_1_options=opts if focus == RequiredTo.PLAYER_1 else [],
                    player_2_options=opts if focus == RequiredTo.PLAYER_2 else [],
                    required_action=_required_action(focus, RequiredStep.ACTION_TURN),
                )

            combat = self._game_state.pending_combat
            if combat is not None:
                # Mid-combat: each player assigns their total Might at the
                # contested BF as damage to the enemy units. The two
                # assignments are independent and applied simultaneously,
                # but we surface them ONE PLAYER AT A TIME (player_1 first,
                # then player_2) so the menu — and the branch tree — only
                # ever shows a single side's choices. Whoever's turn it is
                # gets every valid `play:assign_damage:<csv>` (the maximal
                # kill-sets under the lethal-first rule); the other side's
                # menu is empty until it's their turn. A player who has
                # already committed (or auto-committed via the 0-might rule)
                # is skipped.
                if combat.player_1_targets is None:
                    next_actor = RequiredTo.PLAYER_1
                    p1_options = self._assign_damage_options(RequiredTo.PLAYER_1)
                    p2_options: list[str] = []
                elif combat.player_2_targets is None:
                    next_actor = RequiredTo.PLAYER_2
                    p1_options = []
                    p2_options = self._assign_damage_options(RequiredTo.PLAYER_2)
                else:
                    # Both committed — resolution happens inside the
                    # assign_damage handler, so reaching this branch without
                    # pending_combat being cleared shouldn't happen.
                    # Defensive fallback.
                    next_actor = RequiredTo.BOTH
                    p1_options = []
                    p2_options = []
                return EngineOutput(
                    game_state=self.game_state,
                    player_1_options=p1_options,
                    player_2_options=p2_options,
                    required_action=_required_action(next_actor, RequiredStep.ACTION_TURN),
                )

            repeat = self._game_state.pending_spell_repeat
            if repeat is not None:
                # A [Repeat] spell just finished a selection round. The caster
                # decides whether to pay again and repeat. The WAYS TO PAY are
                # surfaced as pre-costed picker chips (player_X_intents, via
                # shortcuts.compute_repeat_intents) — the exact same shortcut
                # payment flow the initial cast uses. The only flat option here
                # is to DECLINE; everyone else is suppressed.
                chooser = repeat.actor
                opts = ["play:choose_repeat:no"]
                return EngineOutput(
                    game_state=self.game_state,
                    player_1_options=opts if chooser == RequiredTo.PLAYER_1 else [],
                    player_2_options=opts if chooser == RequiredTo.PLAYER_2 else [],
                    required_action=_required_action(chooser, RequiredStep.ACTION_TURN),
                )

            cost_dec = self._game_state.pending_ability_cost
            if cost_dec is not None:
                # A triggered ability fired but carries a payable cost; its
                # controller decides whether to pay (exhaust the source) to put
                # the effect on the chain, or decline. Flat yes/no options.
                chooser = cost_dec.actor
                opts = ["play:choose_ability_cost:yes", "play:choose_ability_cost:no"]
                return EngineOutput(
                    game_state=self.game_state,
                    player_1_options=opts if chooser == RequiredTo.PLAYER_1 else [],
                    player_2_options=opts if chooser == RequiredTo.PLAYER_2 else [],
                    required_action=_required_action(chooser, RequiredStep.ACTION_TURN),
                )

            accel = self._game_state.pending_accelerate
            if accel is not None:
                # A just-played [Accelerate] unit is waiting on its controller's
                # pay-to-ready decision. Like [Repeat], the WAYS TO PAY are
                # pre-costed picker chips (player_X_intents, via
                # shortcuts.compute_accelerate_intents); the only flat option
                # here is to DECLINE.
                chooser = accel.actor
                opts = ["play:choose_accelerate:no"]
                return EngineOutput(
                    game_state=self.game_state,
                    player_1_options=opts if chooser == RequiredTo.PLAYER_1 else [],
                    player_2_options=opts if chooser == RequiredTo.PLAYER_2 else [],
                    required_action=_required_action(chooser, RequiredStep.ACTION_TURN),
                )

            effect_choice = self._game_state.pending_effect_choice
            if effect_choice is not None:
                # A resolving triggered ability is waiting for its controller
                # to pick a target (or decline). The chooser's menu collapses
                # to one option per valid unit + pass; the other player is
                # suppressed — exactly the spell-targeting pattern. Checked
                # BEFORE the chain branch: the choice must resolve before
                # priority play continues.
                opts = [
                    f"play:choose_effect_target:{t}" for t in effect_choice.options
                ]
                # A "may" effect can be declined; a FORCED choice (e.g.
                # "discard 1") cannot — only the picks are offered.
                if effect_choice.optional:
                    opts.append("play:choose_effect_target:pass")
                chooser = effect_choice.actor
                return EngineOutput(
                    game_state=self.game_state,
                    player_1_options=opts if chooser == RequiredTo.PLAYER_1 else [],
                    player_2_options=opts if chooser == RequiredTo.PLAYER_2 else [],
                    required_action=_required_action(chooser, RequiredStep.ACTION_TURN),
                )

            spell_choice = self._game_state.pending_spell_choice
            if spell_choice is not None:
                # Mid-cast: a played spell is waiting for its required target
                # pick. The caster's menu collapses to the enumerated valid
                # target sets (play:choose_spell_targets:<refs>); everything
                # else is suppressed until a set is chosen. See
                # PendingSpellChoice and action_turn/builtins.py.
                opts = self._spell_choice_options(spell_choice)
                chooser = spell_choice.actor
                return EngineOutput(
                    game_state=self.game_state,
                    player_1_options=opts if chooser == RequiredTo.PLAYER_1 else [],
                    player_2_options=opts if chooser == RequiredTo.PLAYER_2 else [],
                    required_action=_required_action(chooser, RequiredStep.ACTION_TURN),
                )

            chain = self._game_state.pending_chain
            if chain is not None:
                # Chain open: only the priority holder may act, and only by
                # casting a [Reaction] spell or passing priority. Everything
                # else (units, moves, end turn, the opponent's menu) is
                # suppressed until both players pass and the chain resolves.
                holder = chain.priority
                opts = self._reaction_play_options(holder) + ["play:pass_priority"]
                return EngineOutput(
                    game_state=self.game_state,
                    player_1_options=opts if holder == RequiredTo.PLAYER_1 else [],
                    player_2_options=opts if holder == RequiredTo.PLAYER_2 else [],
                    required_action=_required_action(holder, RequiredStep.ACTION_TURN),
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

                # Activated abilities ("exhaust: do X") on the active player's
                # READY permanents (units + standalone gears). EXHAUST_THIS-only
                # for now; offered as play:activate:<source ref>. Gated on a
                # valid target existing when the ability targets a unit.
                _gs = self._game_state
                _have_unit = bool(_gs.player_1_units or _gs.player_2_units)
                _av_units = _gs.player_1_units if active == RequiredTo.PLAYER_1 else _gs.player_2_units
                _av_gears = _gs.player_1_gears if active == RequiredTo.PLAYER_1 else _gs.player_2_gears
                for _ui, _u in enumerate(_av_units):
                    if _u.exhausted:
                        continue
                    _ref = f"{active.value}:{_ui}"
                    _ab = self._activated_ability_at(_ref)
                    if _ab is None:
                        continue
                    if self._derive_activation_requirement(tuple(_ab.active_effects)) == "ANY UNIT (1)" and not _have_unit:
                        continue
                    options.append(f"play:activate:{_ref}")
                for _gi, _g in enumerate(_av_gears):
                    if _g.exhausted:
                        continue
                    _ref = f"gear:{active.value}:{_gi}"
                    _ab = self._activated_ability_at(_ref)
                    if _ab is None:
                        continue
                    if self._derive_activation_requirement(tuple(_ab.active_effects)) == "ANY UNIT (1)" and not _have_unit:
                        continue
                    options.append(f"play:activate:{_ref}")

                # Spells are gated by the same Energy + domain Power cost
                # gates as units. Unlike units they don't go to a location;
                # the handler places them directly into ``player_X_spells``.
                # See action_turn/builtins.py::_play_spell.
                #
                # On top of the cost gate, a spell is only offered if its
                # "Spell Choice Requirement" (CSV) can be met on the current
                # board — i.e. there is at least one VALID set of targets for
                # the choices the card requires. See
                # riftbound_engine/requirements.py. Cards with no requirement
                # (blank cell) pass this gate unconditionally.
                from .requirements import spell_playable

                for i, card in enumerate(active_hand or []):
                    if card in seen_play_card:
                        continue
                    if card_type_of(card) != "Spell":
                        continue
                    if self.card_energy_cost(card) > energy:
                        continue
                    if not self.can_afford_power_cost(active, card):
                        continue
                    if not spell_playable(self._game_state, card, caster=active):
                        continue
                    seen_play_card.add(card)
                    options.append(f"play:play_spell:{i}")

                # Gears are gated by the same Energy + domain Power cost gates
                # as units, but they have no Spell Choice Requirement and no
                # location pick: the handler commits them straight to base in
                # the ready state. See action_turn/builtins.py::_play_gear.
                for i, card in enumerate(active_hand or []):
                    if card in seen_play_card:
                        continue
                    if card_type_of(card) != "Gear":
                        continue
                    if self.card_energy_cost(card) > energy:
                        continue
                    if not self.can_afford_power_cost(active, card):
                        continue
                    seen_play_card.add(card)
                    options.append(f"play:play_gear:{i}")

                # The chosen champion plays like a hand unit (action timing),
                # surfaced as a flat option when it hasn't been played yet and
                # is affordable from the CURRENT pools. (When it needs rune
                # payment instead, compute_play_intents offers a combo chip —
                # same split as hand cards.) See action_turn::_play_champion.
                champ_deck = (
                    self._game_state.player_1_deck
                    if active == RequiredTo.PLAYER_1
                    else self._game_state.player_2_deck
                )
                champ_played = (
                    self._game_state.player_1_champion_played
                    if active == RequiredTo.PLAYER_1
                    else self._game_state.player_2_champion_played
                )
                champ = champ_deck.chosen_champion if champ_deck is not None else None
                if (
                    champ
                    and not champ_played
                    and self.card_energy_cost(champ) <= energy
                    and self.can_afford_power_cost(active, champ)
                ):
                    options.append("play:play_champion")

                # Movement: each READY unit owned by the active player can
                # move base ↔ a battlefield. Destination control drives the
                # follow-up:
                #   - own-controlled BF → just relocates;
                #   - uncontrolled BF  → opens a showdown;
                #   - opponent BF      → opens a showdown (the active
                #                        player is "invading" — unlike
                #                        play_unit, which still refuses to
                #                        deploy a *fresh* unit there).
                # BF ↔ BF is not allowed; route through base. Moving
                # always exhausts the unit — see
                # action_turn/builtins.py::_move_unit.
                active_units = (
                    self._game_state.player_1_units
                    if active == RequiredTo.PLAYER_1
                    else self._game_state.player_2_units
                )
                for unit_idx, unit in enumerate(active_units):
                    if unit.exhausted:
                        continue
                    if unit.location == "base":
                        # Base → both battlefields are always offered;
                        # the move handler opens a showdown when the
                        # destination isn't already self-controlled.
                        for dest in ("battlefield_1", "battlefield_2"):
                            options.append(f"play:move_unit:{unit_idx}:{dest}")
                    else:
                        # Battlefield → base only. (BF ↔ BF rejected.)
                        options.append(f"play:move_unit:{unit_idx}:base")

                # NOTE: equipping is surfaced via pre-costed picker chips
                # (compute_equip_intents) rather than flat options — the chip's
                # chain produces the [Equip] cost from runes and then applies
                # play:equip. The raw play:equip action handler still validates
                # + pays when that chain runs. See shortcuts.py.

                # NOTE: Gold gear tokens are NOT surfaced as standalone options.
                # Like raw rune actions, killing a Gold token only matters as a
                # PAYMENT step toward playing a card, so it's folded into the
                # shortcut payment plans (shortcuts.py :: _plan_payments) rather
                # than offered as a "use the gold for whatever" action. The
                # play:use_gold handler stays registered so a payment chain can
                # apply it.

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
        """Commit one action and record it on ``state.action_log``.

        The action-log entry is appended ONLY if the underlying
        implementation returns without raising — failed actions
        (validation errors, illegal moves) do not pollute the log."""
        stripped = action.strip()
        output = self._apply_action_impl(stripped, actor)
        self._game_state.action_log.append(
            ActionLogEntry(
                sequence=len(self._game_state.action_log),
                actor=actor.value if isinstance(actor, RequiredTo) else str(actor),
                action=stripped,
            )
        )
        return output

    def _backfill_unit_uids(self) -> None:
        """Give every in-play unit a stable nonzero ``uid`` (idempotent).

        Units enter play through many paths (play_unit, champion, fake-fill,
        restored states). Rather than thread uid assignment through each, we
        assign lazily here — called at the start of every action and before
        spell targets are captured — so a unit always has a stable id by the
        time anything can reference it, and an existing id is never changed."""
        gs = self._game_state
        for units in (gs.player_1_units, gs.player_2_units):
            for u in units:
                if not getattr(u, "uid", 0):
                    u.uid = gs.next_unit_uid
                    gs.next_unit_uid += 1

    def _apply_action_impl(self, action: str, actor: RequiredTo) -> EngineOutput:
        if not action:
            raise ValueError("action must not be empty")
        # Ensure every unit already in play carries a stable uid before this
        # action runs (so e.g. capturing a spell's targets records real ids).
        self._backfill_unit_uids()

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
