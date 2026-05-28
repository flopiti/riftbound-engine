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
    if gs.pending_combat is not None:
        raise ValueError(
            "cannot play units while a contested showdown is in combat — "
            "commit your kills first"
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
    if gs.pending_combat is not None:
        raise ValueError(
            "cannot play spells while a contested showdown is in combat — "
            "commit your kills first"
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
    if gs.pending_combat is not None:
        raise ValueError(
            "cannot move units while a contested showdown is in combat — "
            "commit your kills first"
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

    When both players have passed, the showdown resolves with one of two
    outcomes:

      * **Conquest** — only the initiator has units at the battlefield.
        The initiator takes control and earns 1 point (capped per-BF-
        per-turn via ``award_bf_point``).
      * **Contested** — both players have units at the battlefield. No
        one controls it; the battlefield's controller is cleared. If
        someone previously controlled the BF, the point they earned for
        it is revoked (they no longer hold a conquered battlefield).

    ``pending_showdown`` is cleared either way and the active player's
    turn continues normally.

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

    # Both passed → either resolve directly (uncontested) or move
    # into the PendingCombat damage-distribution phase (contested).
    if showdown.initiator_passed and showdown.opponent_passed:
        bf = showdown.battlefield
        if bf not in ("battlefield_1", "battlefield_2"):
            # Guarded by move_unit handler, but defensive.
            raise ValueError(f"unknown showdown battlefield {bf!r}")

        p1_has = any(u.location == bf for u in gs.player_1_units)
        p2_has = any(u.location == bf for u in gs.player_2_units)
        contested = p1_has and p2_has

        if contested:
            # Both players have units here — open a simultaneous
            # damage-distribution combat. Each side's damage budget is
            # their TOTAL might at this BF; they'll commit a target
            # list via play:commit_kills:<csv-of-target-indices>.
            # 0-might attackers auto-commit to [] (they get to skip
            # their damage step but still have to wait for the
            # opponent's commit before combat resolves).
            from ..engine import PendingCombat
            p1_might = ctx.engine.might_at_battlefield(RequiredTo.PLAYER_1, bf)
            p2_might = ctx.engine.might_at_battlefield(RequiredTo.PLAYER_2, bf)
            gs.pending_combat = PendingCombat(
                battlefield=bf,
                player_1_might=p1_might,
                player_2_might=p2_might,
                player_1_targets=[] if p1_might == 0 else None,
                player_2_targets=[] if p2_might == 0 else None,
            )
            gs.pending_showdown = None
            # Fall through with pending_combat set; control/scoring
            # decisions wait until both players commit and resolve_combat
            # has actually applied the kills.
            return

        # Not contested — only the initiator has units at the BF (move_unit
        # never opens a showdown onto a BF where only the OPPONENT has
        # units), so this is a clean conquest of an empty or
        # opponent-controlled-but-empty battlefield.
        from ..engine import RequiredTo as RT
        previous_controller = (
            gs.battlefield_1_controller
            if bf == "battlefield_1"
            else gs.battlefield_2_controller
        )
        if p1_has or p2_has:
            winner = RT.PLAYER_1 if p1_has else RT.PLAYER_2
            if bf == "battlefield_1":
                gs.battlefield_1_controller = winner
            else:
                gs.battlefield_2_controller = winner
            if previous_controller != winner:
                ctx.engine.award_bf_point(winner, bf)
        else:
            if bf == "battlefield_1":
                gs.battlefield_1_controller = None
            else:
                gs.battlefield_2_controller = None
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
    if gs.pending_combat is not None:
        raise ValueError(
            "cannot exhaust runes while a contested showdown is in combat — "
            "commit your kills first"
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
    if gs.pending_combat is not None:
        raise ValueError(
            "cannot recycle runes while a contested showdown is in combat — "
            "commit your kills first"
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


@register_turn_action("commit_kills")
def _commit_kills(ctx: ActionTurnContext) -> None:
    """Commit one player's damage assignment in a PendingCombat.

    Payload is a comma-separated list of OPPONENT unit indices to kill
    (empty payload = "I kill nothing"). The sum of the targets' Might
    must not exceed the actor's total Might at the contested BF — that's
    the "damage must kill" rule (no partial damage, no overflow). Each
    target index must be unique and refer to an existing opponent unit
    sitting AT the contested battlefield.

    Both players commit independently and BLIND; the engine doesn't
    reveal the opponent's choice until both have committed. As soon as
    both ``player_1_targets`` and ``player_2_targets`` are set, the
    handler calls ``engine.resolve_combat()`` which applies all kills
    simultaneously and then the move continues by reading the
    post-combat state for control + scoring.
    """
    from ..csv_data import card_might_of
    from ..engine import RequiredTo as RT

    gs = ctx.engine._game_state
    pc = gs.pending_combat
    if pc is None:
        raise ValueError("no contested combat is awaiting kill assignments")
    if ctx.actor not in (RT.PLAYER_1, RT.PLAYER_2):
        raise ValueError("commit_kills requires player_1 or player_2")

    # Parse the index list. Empty payload is valid — "I kill nothing".
    payload = ctx.payload.strip()
    if payload:
        try:
            target_indices = [int(p.strip()) for p in payload.split(",") if p.strip()]
        except ValueError as e:
            raise ValueError(
                f"play:commit_kills payload must be a comma-separated list of integers, got {payload!r}"
            ) from e
    else:
        target_indices = []
    if len(set(target_indices)) != len(target_indices):
        raise ValueError("play:commit_kills target indices must be unique")

    # Resolve which list we're killing into and our damage budget.
    if ctx.actor == RT.PLAYER_1:
        if pc.player_1_targets is not None:
            raise ValueError("player_1 has already committed their kills for this combat")
        opponent_units = gs.player_2_units
        budget = pc.player_1_might
    else:
        if pc.player_2_targets is not None:
            raise ValueError("player_2 has already committed their kills for this combat")
        opponent_units = gs.player_1_units
        budget = pc.player_2_might

    # Validate every target: in range, at the contested BF, and the
    # total Might cost doesn't exceed our budget.
    total_cost = 0
    for idx in target_indices:
        if idx < 0 or idx >= len(opponent_units):
            raise ValueError(
                f"play:commit_kills target index out of range: {idx} "
                f"(opponent has {len(opponent_units)} units)"
            )
        unit = opponent_units[idx]
        if unit.location != pc.battlefield:
            raise ValueError(
                f"play:commit_kills target index {idx} is not at the contested "
                f"battlefield ({pc.battlefield})"
            )
        might = card_might_of(unit.card)
        if might is None:
            raise ValueError(
                f"play:commit_kills target {unit.card!r} has no Might value in the CSV"
            )
        total_cost += might
    if total_cost > budget:
        raise ValueError(
            f"play:commit_kills total Might of targets ({total_cost}) exceeds "
            f"available damage ({budget})"
        )

    # Record this player's commit.
    if ctx.actor == RT.PLAYER_1:
        pc.player_1_targets = list(target_indices)
    else:
        pc.player_2_targets = list(target_indices)

    # If both committed, resolve combat NOW and then close out
    # control/scoring per the same rules as an uncontested showdown.
    if pc.player_1_targets is not None and pc.player_2_targets is not None:
        bf = pc.battlefield
        ctx.engine.resolve_combat(bf)
        # `resolve_combat` cleared pending_combat. Now read who's left
        # at the BF and assign control + a point (points NEVER removed —
        # the previous controller keeping the BF earns no new point).
        p1_has = any(u.location == bf for u in gs.player_1_units)
        p2_has = any(u.location == bf for u in gs.player_2_units)
        previous_controller = (
            gs.battlefield_1_controller
            if bf == "battlefield_1"
            else gs.battlefield_2_controller
        )
        if p1_has and p2_has:
            # Still contested even after combat. Nobody's been wiped —
            # control unchanged, no point awarded.
            return
        if p1_has or p2_has:
            winner = RT.PLAYER_1 if p1_has else RT.PLAYER_2
            if bf == "battlefield_1":
                gs.battlefield_1_controller = winner
            else:
                gs.battlefield_2_controller = winner
            if previous_controller != winner:
                ctx.engine.award_bf_point(winner, bf)
        else:
            # Both sides wiped — BF goes neutral.
            if bf == "battlefield_1":
                gs.battlefield_1_controller = None
            else:
                gs.battlefield_2_controller = None
