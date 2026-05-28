"""Built-in and example `play:` handlers. Import new action modules here so they register."""

from __future__ import annotations

from .context import ActionTurnContext
from .registry import register_turn_action


@register_turn_action("end_turn")
def _end_turn(ctx: ActionTurnContext) -> None:
    """End the current player's turn and move to the next player's ABCD."""
    if ctx.payload:
        raise ValueError("play:end_turn does not take a payload")
    ctx.engine._advance_turn()


@register_turn_action("play_unit")
def _play_unit(ctx: ActionTurnContext) -> None:
    """Start playing a card as a unit. The location is picked in a follow-up step.

    Payload is the 0-based hand index. The card is removed from hand and
    parked in `pending_play`; it commits to `units` after
    `play:choose_location:*`.

    Cost gate: this action is rejected up front if either
      * the card's Energy is greater than the active player's current
        Energy pool, OR
      * the card has a Power requirement the player can't meet from the
        Power pool of the card's listed domain(s).
    Energy is produced by exhausting runes (``play:exhaust_rune:*``);
    Power by recycling runes (``play:recycle_rune:*`` /
    ``play:exhaust_and_recycle_rune:*``). Both costs are **deducted
    immediately** when the play starts.

    Only cards whose CSV ``Card Type`` is ``Unit`` may be played this way.
    """
    from ..csv_data import card_type_of
    from ..engine import PendingPlay
    from ..engine import RequiredTo as RT

    payload = ctx.payload.strip()
    if not payload:
        raise ValueError("play:play_unit requires a hand index (e.g. play:play_unit:0)")
    try:
        index = int(payload)
    except ValueError as e:
        raise ValueError(f"play:play_unit index must be an integer, got {payload!r}") from e

    gs = ctx.engine._game_state
    if gs.pending_play is not None:
        raise ValueError("a play is already waiting for a location; choose one first")
    if gs.pending_showdown is not None:
        raise ValueError(
            "cannot play units while a showdown is in progress — "
            "resolve the showdown first"
        )

    if ctx.actor == RT.PLAYER_1:
        hand = gs.player_1_hand
    elif ctx.actor == RT.PLAYER_2:
        hand = gs.player_2_hand
    else:
        raise ValueError("play_unit requires player_1 or player_2")

    if hand is None:
        raise ValueError("hand is not initialized")
    if index < 0 or index >= len(hand):
        raise ValueError(f"play_unit index out of range: {index} (hand size {len(hand)})")

    # Peek the card before mutating state — if it's not a Unit, reject without
    # disturbing the hand.
    card = hand[index]
    card_type = card_type_of(card)
    if card_type != "Unit":
        raise ValueError(
            f"'{card}' cannot be played as a unit "
            f"(CSV Card Type: {card_type or 'unknown'}; only 'Unit' is allowed)"
        )

    energy_cost = ctx.engine.card_energy_cost(card)
    energy = ctx.engine.player_energy(ctx.actor)
    if energy_cost > energy:
        raise ValueError(
            f"cannot play '{card}': costs {energy_cost} Energy but only {energy} available"
            f" — exhaust runes first to produce Energy"
        )

    power_cost = ctx.engine.card_power_cost(card)
    if power_cost > 0:
        if not ctx.engine.can_afford_power_cost(ctx.actor, card):
            domains = ctx.engine.card_domains(card)
            domain_label = " / ".join(domains) if domains else "<no domain>"
            pool = ctx.engine.player_power(ctx.actor)
            available = sum(pool.get(d, 0) for d in domains)
            raise ValueError(
                f"cannot play '{card}': costs {power_cost} {domain_label} Power "
                f"but only {available} available — recycle runes first to produce Power"
            )

    # Deduct both costs up front so the pools stay accurate even before the
    # unit is committed to a location (the player can't bail out of pending_play).
    if energy_cost > 0:
        ctx.engine.add_energy(ctx.actor, -energy_cost)
    if power_cost > 0:
        ctx.engine._deduct_power_cost(ctx.actor, card)
    hand.pop(index)
    gs.pending_play = PendingPlay(actor=ctx.actor, card=card)


@register_turn_action("play_spell")
def _play_spell(ctx: ActionTurnContext) -> None:
    """Play a card whose CSV ``Card Type`` is ``Spell``.

    Payload is the 0-based hand index. The card is removed from hand and
    appended to ``player_X_spells``. Unlike ``play_unit``, spells don't
    go to a location — there's no ``pending_play`` / ``choose_location``
    follow-up. The Energy and domain Power costs are deducted up front
    using the same gates as units (see ``_play_unit``).

    The engine currently has no resolution / on-cast effect for spells:
    the card just sits in ``player_X_spells`` for the rest of the match
    so the UI can render it (the surface displays it on the right side
    of the screen, vertically centered and slightly enlarged).
    """
    from ..csv_data import card_type_of
    from ..engine import PlayedSpell
    from ..engine import RequiredTo as RT

    payload = ctx.payload.strip()
    if not payload:
        raise ValueError("play:play_spell requires a hand index (e.g. play:play_spell:0)")
    try:
        index = int(payload)
    except ValueError as e:
        raise ValueError(f"play:play_spell index must be an integer, got {payload!r}") from e

    gs = ctx.engine._game_state
    if gs.pending_play is not None:
        raise ValueError(
            "cannot play a spell while a unit is waiting for a location — "
            "choose the location first"
        )
    if gs.pending_showdown is not None:
        raise ValueError(
            "cannot play spells while a showdown is in progress — "
            "resolve the showdown first"
        )

    if ctx.actor == RT.PLAYER_1:
        hand = gs.player_1_hand
        spells = gs.player_1_spells
    elif ctx.actor == RT.PLAYER_2:
        hand = gs.player_2_hand
        spells = gs.player_2_spells
    else:
        raise ValueError("play_spell requires player_1 or player_2")

    if hand is None:
        raise ValueError("hand is not initialized")
    if index < 0 or index >= len(hand):
        raise ValueError(f"play_spell index out of range: {index} (hand size {len(hand)})")

    # Peek the card before mutating state — if it's not a Spell, reject without
    # disturbing the hand.
    card = hand[index]
    card_type = card_type_of(card)
    if card_type != "Spell":
        raise ValueError(
            f"'{card}' cannot be played as a spell "
            f"(CSV Card Type: {card_type or 'unknown'}; only 'Spell' is allowed)"
        )

    energy_cost = ctx.engine.card_energy_cost(card)
    energy = ctx.engine.player_energy(ctx.actor)
    if energy_cost > energy:
        raise ValueError(
            f"cannot play '{card}': costs {energy_cost} Energy but only {energy} available"
            f" — exhaust runes first to produce Energy"
        )

    power_cost = ctx.engine.card_power_cost(card)
    if power_cost > 0:
        if not ctx.engine.can_afford_power_cost(ctx.actor, card):
            domains = ctx.engine.card_domains(card)
            domain_label = " / ".join(domains) if domains else "<no domain>"
            pool = ctx.engine.player_power(ctx.actor)
            available = sum(pool.get(d, 0) for d in domains)
            raise ValueError(
                f"cannot play '{card}': costs {power_cost} {domain_label} Power "
                f"but only {available} available — recycle runes first to produce Power"
            )

    # Deduct both costs up front, then commit the card to the spell stack.
    if energy_cost > 0:
        ctx.engine.add_energy(ctx.actor, -energy_cost)
    if power_cost > 0:
        ctx.engine._deduct_power_cost(ctx.actor, card)
    hand.pop(index)
    spells.append(PlayedSpell(card=card))


@register_turn_action("choose_location")
def _choose_location(ctx: ActionTurnContext) -> None:
    """Commit a pending play to a location (base, battlefield_1, or battlefield_2).

    The unit is appended to the player's ``units`` and ``pending_play`` is
    cleared. The Energy cost was already deducted by ``play:play_unit:*`` —
    this step only places the unit on the board.
    """
    from ..engine import PlayedUnit, UNIT_LOCATIONS
    from ..engine import RequiredTo as RT

    location = ctx.payload.strip()
    if not location:
        raise ValueError("play:choose_location requires a location (base|battlefield_1|battlefield_2)")
    if location not in UNIT_LOCATIONS:
        raise ValueError(
            f"unknown location {location!r}; expected one of {', '.join(UNIT_LOCATIONS)}"
        )

    gs = ctx.engine._game_state
    pending = gs.pending_play
    if pending is None:
        raise ValueError("no play is waiting for a location")
    if pending.actor != ctx.actor:
        raise ValueError("only the player who started the play may choose its location")

    if not ctx.engine.player_controls_location(ctx.actor, location):
        raise ValueError(
            f"{ctx.actor.value} does not control {location!r}; "
            "units may only be played in a territory the player controls"
        )

    if ctx.actor == RT.PLAYER_1:
        units = gs.player_1_units
    elif ctx.actor == RT.PLAYER_2:
        units = gs.player_2_units
    else:
        raise ValueError("choose_location requires player_1 or player_2")

    # Units enter the battlefield exhausted (summoning sickness); they ready
    # on the owner's next Awake (ABCD step A).
    units.append(PlayedUnit(card=pending.card, location=location, exhausted=True))
    gs.pending_play = None


@register_turn_action("move_unit")
def _move_unit(ctx: ActionTurnContext) -> None:
    """Move a ready unit between base and a battlefield, exhausting it.

    Wire format: ``play:move_unit:<unit_index>:<destination>``.
      * ``unit_index`` — 0-based index into the active player's ``units`` list.
      * ``destination`` — one of ``base``, ``battlefield_1``, ``battlefield_2``.

    Rules:
      * The unit must be **ready** (``exhausted=False``). Newly played units
        suffer summoning sickness and cannot move on their first turn.
      * Allowed transitions: ``base`` ↔ any battlefield. Battlefield ↔
        battlefield jumps are NOT allowed (route through base).
      * The destination must be different from the unit's current location.
      * The destination's CONTROL status drives what happens after the move:
          - own-controlled battlefield → just relocates;
          - uncontrolled battlefield → opens a showdown;
          - opponent-controlled battlefield → opens a showdown (the
            active player is "invading" — unlike ``play_unit``, which
            still refuses to deploy a fresh unit onto an opponent BF,
            moving a unit there is allowed and triggers the contest).

    Effect: updates the unit's ``location`` and flips ``exhausted=True`` so
    the unit can't move again this turn (and stays exhausted through the
    opponent's turn, readying on the owner's next Awake).
    """
    from ..engine import UNIT_LOCATIONS
    from ..engine import RequiredTo as RT

    payload = ctx.payload
    if not payload:
        raise ValueError(
            "play:move_unit requires <unit_index>:<destination> "
            "(e.g. play:move_unit:0:battlefield_1)"
        )
    parts = payload.split(":", 1)
    if len(parts) != 2:
        raise ValueError(
            "play:move_unit requires <unit_index>:<destination> "
            "(e.g. play:move_unit:0:battlefield_1)"
        )
    raw_index, destination = parts[0].strip(), parts[1].strip()
    try:
        index = int(raw_index)
    except ValueError as e:
        raise ValueError(
            f"play:move_unit unit index must be an integer, got {raw_index!r}"
        ) from e
    if destination not in UNIT_LOCATIONS:
        raise ValueError(
            f"unknown destination {destination!r}; expected one of {', '.join(UNIT_LOCATIONS)}"
        )

    gs = ctx.engine._game_state
    if gs.pending_play is not None:
        raise ValueError(
            "cannot move units while a play is waiting for a location — "
            "choose the location first"
        )
    if gs.pending_showdown is not None:
        raise ValueError(
            "cannot move units while a showdown is in progress — "
            "resolve the showdown first"
        )

    if ctx.actor == RT.PLAYER_1:
        units = gs.player_1_units
    elif ctx.actor == RT.PLAYER_2:
        units = gs.player_2_units
    else:
        raise ValueError("move_unit requires player_1 or player_2")

    if index < 0 or index >= len(units):
        raise ValueError(
            f"move_unit unit index out of range: {index} (unit count {len(units)})"
        )
    unit = units[index]

    if unit.exhausted:
        raise ValueError(
            f"unit at index {index} ({unit.card!r}) is exhausted and cannot move "
            "this turn — it will ready on the owner's next Awake"
        )
    if unit.location == destination:
        raise ValueError(
            f"unit at index {index} ({unit.card!r}) is already at {destination!r}"
        )

    # Base ↔ battlefield only; BF ↔ BF is disallowed.
    if unit.location != "base" and destination != "base":
        raise ValueError(
            f"cannot move {unit.card!r} from {unit.location!r} to {destination!r}: "
            "battlefield-to-battlefield moves are not allowed; route through base"
        )

    # Determine the current controller of the destination battlefield (if any).
    # Moves to a battlefield the active player already controls just
    # relocate the unit. Moves to an UNCONTROLLED battlefield, or to one
    # controlled by the OPPONENT, open a showdown — see PendingShowdown.
    # Only battlefield → battlefield is barred (handled above).
    dest_controller: "RequiredTo | None" = None
    if destination == "battlefield_1":
        dest_controller = gs.battlefield_1_controller
    elif destination == "battlefield_2":
        dest_controller = gs.battlefield_2_controller

    unit.location = destination
    unit.exhausted = True

    # If the unit just walked onto a battlefield that the active player
    # does NOT already control, open a showdown. Both players' options
    # collapse to ``play:pass_showdown`` until both have passed; on
    # resolution the initiator takes control (matching the minimal
    # showdown model we've had for uncontrolled BFs).
    if destination != "base" and dest_controller != ctx.actor:
        from ..engine import PendingShowdown
        gs.pending_showdown = PendingShowdown(
            battlefield=destination,
            initiator=ctx.actor,
        )


@register_turn_action("pass_showdown")
def _pass_showdown(ctx: ActionTurnContext) -> None:
    """Pass on a pending showdown. The initiator passes first, then the opponent.

    When both players have passed, the showdown resolves: the contested
    battlefield's controller is set to the showdown's initiator (in the
    current minimal model the initiator is always the only player with a
    unit on the battlefield, so they always "win"), and ``pending_showdown``
    is cleared. The active player's turn then continues normally.

    Payload is ignored — passes carry no arguments.
    """
    from ..engine import RequiredTo as RT  # noqa: F401 — kept for symmetry

    gs = ctx.engine._game_state
    showdown = gs.pending_showdown
    if showdown is None:
        raise ValueError("no showdown is in progress")

    initiator = showdown.initiator
    opponent = ctx.engine.opponent_of(initiator)
    # Sequencing: initiator passes first, opponent second. Out-of-order
    # passes are rejected to keep the wire protocol explicit.
    if not showdown.initiator_passed:
        if ctx.actor != initiator:
            raise ValueError(
                f"showdown is waiting on the initiator ({initiator.value}) to pass first"
            )
        showdown.initiator_passed = True
    else:
        if ctx.actor != opponent:
            raise ValueError(
                f"showdown is waiting on the opponent ({opponent.value}) to pass"
            )
        showdown.opponent_passed = True

    # Both passed → resolve.
    if showdown.initiator_passed and showdown.opponent_passed:
        if showdown.battlefield == "battlefield_1":
            gs.battlefield_1_controller = initiator
        elif showdown.battlefield == "battlefield_2":
            gs.battlefield_2_controller = initiator
        else:  # pragma: no cover — guarded by move_unit handler
            raise ValueError(f"unknown showdown battlefield {showdown.battlefield!r}")
        # Gaining control of a battlefield you didn't already control
        # scores 1 point — capped at 1 per battlefield per turn via
        # award_bf_point (so this is suppressed if B-phase HOLD scoring
        # already credited this BF this turn, e.g. for a future rule that
        # could give a player control during their own HOLD step).
        ctx.engine.award_bf_point(initiator, showdown.battlefield)
        gs.pending_showdown = None


@register_turn_action("exhaust_rune")
def _exhaust_rune(ctx: ActionTurnContext) -> None:
    """Exhaust one ready rune from the active player's pool, producing 1 Energy.

    Payload is the 0-based index into the active player's rune pool. The
    chosen rune must currently be ready (``exhausted=False``). The
    produced Energy is added to the active player's pool and persists for
    the rest of the turn (it does NOT carry into the next turn).

    Only valid during the active player's action turn — i.e. when no
    ``pending_play`` is waiting for a location.
    """
    from ..engine import RequiredTo as RT  # noqa: F401 — kept for symmetry

    payload = ctx.payload.strip()
    if not payload:
        raise ValueError("play:exhaust_rune requires a rune index (e.g. play:exhaust_rune:0)")
    try:
        index = int(payload)
    except ValueError as e:
        raise ValueError(f"play:exhaust_rune index must be an integer, got {payload!r}") from e

    gs = ctx.engine._game_state
    if gs.pending_play is not None:
        raise ValueError(
            "cannot exhaust runes while a play is waiting for a location — "
            "choose the location first"
        )
    if gs.pending_showdown is not None:
        raise ValueError(
            "cannot exhaust runes while a showdown is in progress — "
            "resolve the showdown first"
        )
    if ctx.actor != gs.current_player:
        raise ValueError("only the active player may exhaust runes")

    pool = ctx.engine.runes_for(ctx.actor)
    if index < 0 or index >= len(pool):
        raise ValueError(f"exhaust_rune index out of range: {index} (rune pool size {len(pool)})")
    rune = pool[index]
    if rune.exhausted:
        raise ValueError(f"rune at index {index} is already exhausted; pick a ready rune")

    rune.exhausted = True
    ctx.engine.add_energy(ctx.actor, 1)


def _take_rune_at_index(ctx: ActionTurnContext, index: int):
    """Validate index/phase/actor and pop the rune at ``index`` from the pool.

    Shared helper for the two recycle handlers below. Caller decides what
    additional effects to apply (Energy gain, etc.) before/after the rune is
    popped. The popped Rune is returned with its ``exhausted`` flag intact;
    the caller is responsible for resetting it (the recycle path resets it
    to False so the rune comes back ready when re-channeled).
    """
    gs = ctx.engine._game_state
    if gs.pending_play is not None:
        raise ValueError(
            "cannot recycle runes while a play is waiting for a location — "
            "choose the location first"
        )
    if gs.pending_showdown is not None:
        raise ValueError(
            "cannot recycle runes while a showdown is in progress — "
            "resolve the showdown first"
        )
    if ctx.actor != gs.current_player:
        raise ValueError("only the active player may recycle runes")
    pool = ctx.engine.runes_for(ctx.actor)
    if index < 0 or index >= len(pool):
        raise ValueError(
            f"recycle_rune index out of range: {index} (rune pool size {len(pool)})"
        )
    return pool[index]


@register_turn_action("recycle_rune")
def _recycle_rune(ctx: ActionTurnContext) -> None:
    """Recycle an already-exhausted rune: pool → bottom of rune library, +1 domain Power.

    Payload is the 0-based index into the active player's rune pool. The
    chosen rune must currently be **exhausted** — its Energy was already
    produced when it was first exhausted this turn, so no new Energy is
    granted. For recycling a ready rune use ``exhaust_and_recycle_rune``.

    The recycled rune is reset to ``exhausted=False`` before being placed
    at the bottom of the rune library so it comes back ready when the
    Channel step (ABCD C) eventually pulls it back into the pool.
    """
    payload = ctx.payload.strip()
    if not payload:
        raise ValueError("play:recycle_rune requires a rune index (e.g. play:recycle_rune:0)")
    try:
        index = int(payload)
    except ValueError as e:
        raise ValueError(f"play:recycle_rune index must be an integer, got {payload!r}") from e

    rune = _take_rune_at_index(ctx, index)
    if not rune.exhausted:
        raise ValueError(
            f"rune at index {index} is ready, not exhausted — "
            "use play:exhaust_and_recycle_rune to spend a ready rune"
        )

    pool = ctx.engine.runes_for(ctx.actor)
    library = ctx.engine.rune_library_for(ctx.actor)
    domain = rune.domain
    # Pop from the pool, reset, and place at the bottom of the rune library.
    pool.pop(index)
    rune.exhausted = False
    library.append(rune)
    ctx.engine.add_power(ctx.actor, domain, 1)


@register_turn_action("exhaust_and_recycle_rune")
def _exhaust_and_recycle_rune(ctx: ActionTurnContext) -> None:
    """Exhaust a ready rune (+1 Energy) AND recycle it (+1 domain Power).

    Payload is the 0-based index into the active player's rune pool. The
    chosen rune must currently be **ready**. The rune is exhausted (which
    produces 1 Energy as if via ``exhaust_rune``), then removed from the
    pool and placed at the bottom of the rune library (which produces 1
    Power of the rune's domain). For recycling a rune that's already
    exhausted, use ``recycle_rune`` instead.
    """
    payload = ctx.payload.strip()
    if not payload:
        raise ValueError(
            "play:exhaust_and_recycle_rune requires a rune index "
            "(e.g. play:exhaust_and_recycle_rune:0)"
        )
    try:
        index = int(payload)
    except ValueError as e:
        raise ValueError(
            f"play:exhaust_and_recycle_rune index must be an integer, got {payload!r}"
        ) from e

    rune = _take_rune_at_index(ctx, index)
    if rune.exhausted:
        raise ValueError(
            f"rune at index {index} is already exhausted — "
            "use play:recycle_rune to recycle an exhausted rune without producing Energy"
        )

    pool = ctx.engine.runes_for(ctx.actor)
    library = ctx.engine.rune_library_for(ctx.actor)
    domain = rune.domain
    # Exhausting a rune always produces 1 Energy (same rule as exhaust_rune).
    ctx.engine.add_energy(ctx.actor, 1)
    # Then recycle: pop from pool, reset exhausted, and place at library bottom.
    pool.pop(index)
    rune.exhausted = False
    library.append(rune)
    ctx.engine.add_power(ctx.actor, domain, 1)
