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

    [Accelerate] is NOT paid here — it's offered as a separate decision AFTER
    the location is chosen (see ``choose_location`` / ``choose_accelerate``),
    mirroring the [Repeat] flow.
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
    if gs.pending_spell_choice is not None:
        raise ValueError(
            "cannot play a unit while a spell is waiting for target selection — "
            "choose its targets first"
        )
    if gs.pending_chain is not None:
        raise ValueError(
            "cannot play a unit while the chain is open — pass priority (or "
            "respond with a Reaction) until the chain resolves"
        )
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


@register_turn_action("play_champion")
def _play_champion(ctx: ActionTurnContext) -> None:
    """Play the chosen champion — the fixed Unit that sits beside the board.

    The champion isn't in the hand; it starts available and can be played at
    action timing exactly like a hand unit, once affordable. It can only be
    played ONCE per match (it then lives on the board as a normal unit). Any
    trailing payload (a synthetic index from the picker) is ignored.

    Same cost gates and pending guards as ``play_unit``: Energy + domain
    Power are checked and deducted up front, then the champion parks in
    ``pending_play`` and commits on ``play:choose_location:*``.
    """
    from ..engine import PendingPlay
    from ..engine import RequiredTo as RT

    gs = ctx.engine._game_state
    if gs.pending_play is not None:
        raise ValueError("a play is already waiting for a location; choose one first")
    if gs.pending_spell_choice is not None:
        raise ValueError(
            "cannot play the champion while a spell is waiting for target selection"
        )
    if gs.pending_chain is not None:
        raise ValueError(
            "cannot play the champion while the chain is open — resolve it first"
        )
    if gs.pending_showdown is not None:
        raise ValueError(
            "cannot play the champion while a showdown is in progress"
        )
    if gs.pending_combat is not None:
        raise ValueError(
            "cannot play the champion while a contested showdown is in combat"
        )

    if ctx.actor == RT.PLAYER_1:
        deck = gs.player_1_deck
        already = gs.player_1_champion_played
    elif ctx.actor == RT.PLAYER_2:
        deck = gs.player_2_deck
        already = gs.player_2_champion_played
    else:
        raise ValueError("play_champion requires player_1 or player_2")

    if deck is None or not deck.chosen_champion:
        raise ValueError("no chosen champion is available for this player")
    if already:
        raise ValueError("the champion has already been played this match")

    card = deck.chosen_champion

    energy_cost = ctx.engine.card_energy_cost(card)
    energy = ctx.engine.player_energy(ctx.actor)
    if energy_cost > energy:
        raise ValueError(
            f"cannot play champion '{card}': costs {energy_cost} Energy but only "
            f"{energy} available — exhaust runes first to produce Energy"
        )
    power_cost = ctx.engine.card_power_cost(card)
    if power_cost > 0 and not ctx.engine.can_afford_power_cost(ctx.actor, card):
        domains = ctx.engine.card_domains(card)
        domain_label = " / ".join(domains) if domains else "<no domain>"
        pool = ctx.engine.player_power(ctx.actor)
        available = sum(pool.get(d, 0) for d in domains)
        raise ValueError(
            f"cannot play champion '{card}': costs {power_cost} {domain_label} "
            f"Power but only {available} available — recycle runes first"
        )

    # Deduct costs, mark the champion as played, and park it for placement.
    if energy_cost > 0:
        ctx.engine.add_energy(ctx.actor, -energy_cost)
    if power_cost > 0:
        ctx.engine._deduct_power_cost(ctx.actor, card)
    if ctx.actor == RT.PLAYER_1:
        gs.player_1_champion_played = True
    else:
        gs.player_2_champion_played = True
    gs.pending_play = PendingPlay(actor=ctx.actor, card=card)


@register_turn_action("play_gear")
def _play_gear(ctx: ActionTurnContext) -> None:
    """Play a card whose CSV ``Card Type`` is ``Gear``.

    Payload is the 0-based hand index. Gears are paid exactly like units —
    the same Energy + domain Power cost gates apply and both costs are
    deducted up front — but they differ in how they enter play:

      * there is NO ``pending_play`` / ``play:choose_location`` follow-up; the
        gear is committed to the owner's ``base`` immediately, the way a spell
        skips location selection; and
      * it enters **ready** (``exhausted=False``), unlike a unit which enters
        exhausted with summoning sickness.

    A gear then stays at base for the rest of the match — it is never offered
    a ``play:move_unit`` option (see engine option generation), so it cannot
    move. Only cards whose CSV ``Card Type`` is ``Gear`` may be played here.
    """
    from ..csv_data import card_type_of
    from ..engine import PlayedGear
    from ..engine import RequiredTo as RT

    payload = ctx.payload.strip()
    if not payload:
        raise ValueError("play:play_gear requires a hand index (e.g. play:play_gear:0)")
    try:
        index = int(payload)
    except ValueError as e:
        raise ValueError(f"play:play_gear index must be an integer, got {payload!r}") from e

    gs = ctx.engine._game_state
    # Same play gates as a unit: a gear is an action-timing play, not a
    # reaction, so it can't be slipped in mid-resolution.
    if gs.pending_play is not None:
        raise ValueError("a play is already waiting for a location; choose one first")
    if gs.pending_spell_choice is not None:
        raise ValueError(
            "cannot play a gear while a spell is waiting for target selection — "
            "choose its targets first"
        )
    if gs.pending_chain is not None:
        raise ValueError(
            "cannot play a gear while the chain is open — pass priority (or "
            "respond with a Reaction) until the chain resolves"
        )
    if gs.pending_showdown is not None:
        raise ValueError(
            "cannot play gears while a showdown is in progress — "
            "resolve the showdown first"
        )
    if gs.pending_combat is not None:
        raise ValueError(
            "cannot play gears while a contested showdown is in combat — "
            "commit your kills first"
        )

    if ctx.actor == RT.PLAYER_1:
        hand = gs.player_1_hand
        gears = gs.player_1_gears
    elif ctx.actor == RT.PLAYER_2:
        hand = gs.player_2_hand
        gears = gs.player_2_gears
    else:
        raise ValueError("play_gear requires player_1 or player_2")

    if hand is None:
        raise ValueError("hand is not initialized")
    if index < 0 or index >= len(hand):
        raise ValueError(f"play_gear index out of range: {index} (hand size {len(hand)})")

    # Peek the card before mutating state — if it's not a Gear, reject without
    # disturbing the hand.
    card = hand[index]
    card_type = card_type_of(card)
    if card_type != "Gear":
        raise ValueError(
            f"'{card}' cannot be played as a gear "
            f"(CSV Card Type: {card_type or 'unknown'}; only 'Gear' is allowed)"
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

    # Deduct both costs up front, then commit the gear straight to base in the
    # READY state (no pending_play, no location pick).
    if energy_cost > 0:
        ctx.engine.add_energy(ctx.actor, -energy_cost)
    if power_cost > 0:
        ctx.engine._deduct_power_cost(ctx.actor, card)
    hand.pop(index)
    gears.append(PlayedGear(card=card, location="base", exhausted=False))


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
    if gs.pending_spell_choice is not None:
        raise ValueError(
            "cannot play a spell while another spell is waiting for target "
            "selection — choose its targets first"
        )
    # NOTE: a pending_showdown does NOT block spells — Action/Reaction
    # spells are explicitly playable in a showdown. That's validated below
    # once we know which card it is (see the showdown gate after the
    # card-type check).
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

    # Showdown gate: during a showdown only the player on the clock may act,
    # and only with an [Action] or [Reaction] spell. Playing one STARTS the
    # fight — `locked` is flipped below (with the other mutations) so the
    # initiator can no longer muster additional units. (The actual lock is
    # set late, after every validation passes, to avoid mutating on a
    # rejected play.)
    showdown = gs.pending_showdown
    if showdown is not None and gs.pending_chain is None:
        from ..csv_data import card_playable_in_showdown

        # Only the FOCUS holder may open a play; only [Action]/[Reaction]
        # spells are legal in a showdown.
        if ctx.actor != showdown.focus_holder:
            raise ValueError(
                f"focus is with {showdown.focus_holder.value}; "
                f"{ctx.actor.value} cannot play a spell in the showdown right now"
            )
        if not card_playable_in_showdown(card):
            raise ValueError(
                f"'{card}' can't be played in a showdown — only [Action] and "
                "[Reaction] spells may be played during a showdown"
            )

    # If a chain is already open, this is a RESPONSE: only the player holding
    # priority may act, and only with a [Reaction] spell. The opening cast
    # (no chain yet) can be any spell, played by the active player at action
    # timing the way it always was.
    chain = gs.pending_chain
    if chain is not None:
        from ..csv_data import card_is_reaction

        if ctx.actor != chain.priority:
            raise ValueError(
                f"priority is with {chain.priority.value}; "
                f"{ctx.actor.value} cannot play a spell right now"
            )
        if not card_is_reaction(card):
            raise ValueError(
                f"'{card}' is not a [Reaction] spell — only Reaction spells can "
                "be played in response while the chain is open"
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

    # Requirement gate (defense-in-depth). The options/intents paths already
    # hide spells with no valid target, but a direct apply_action must not be
    # able to play one: it would park in pending_spell_choice with zero
    # choosable sets and soft-lock the turn. Checked BEFORE any mutation so a
    # rejection leaves hand/pools untouched.
    from ..requirements import spell_playable

    if not spell_playable(gs, card, caster=ctx.actor):
        raise ValueError(
            f"cannot play '{card}': its Spell Choice Requirement has no valid "
            "target on the board"
        )

    # All validation passed — if this spell is being played during a
    # showdown, the fight has now started: lock mustering (no more units)
    # and reset the focus-pass counter (a play means the showdown isn't
    # about to resolve from prior passes).
    if showdown is not None:
        showdown.locked = True
        showdown.focus_passes = 0

    # Deduct both costs up front and remove the card from hand.
    if energy_cost > 0:
        ctx.engine.add_energy(ctx.actor, -energy_cost)
    if power_cost > 0:
        ctx.engine._deduct_power_cost(ctx.actor, card)
    hand.pop(index)

    # If the card's Spell Choice Requirement forces one or more explicit
    # target picks (a pure-AND tree with at least one ANY-UNIT phrase whose
    # minimum >= 1), park it in pending_spell_choice and let the caster choose
    # via play:choose_spell_targets:* — one pick per phrase, in order. The
    # spell only reaches the chain once every phrase is chosen. Otherwise (no
    # requirement, min-0, an unknown selector, or an OR tree) it goes straight
    # onto the chain, which opens (or refreshes) the priority window. The card
    # no longer lands directly in the spell pile — it resolves off the chain
    # once both players pass (see _resolve_chain / pass_priority).
    from ..csv_data import card_spell_requirement_of
    from ..requirements import (
        battlefield_picks,
        gear_picks,
        location_picks,
        spell_picks,
        spell_target_plan,
        trash_picks,
    )

    raw_req = card_spell_requirement_of(card)
    if (
        spell_target_plan(raw_req)
        or battlefield_picks(raw_req)
        or gear_picks(raw_req)
        or trash_picks(raw_req)
        or spell_picks(raw_req)
        or location_picks(raw_req)
    ):
        from ..engine import PendingSpellChoice

        gs.pending_spell_choice = PendingSpellChoice(
            actor=ctx.actor, card=card, requirement=raw_req or ""
        )
    else:
        # No target picks — a single empty round. offer_repeat_or_push opens
        # the [Repeat] decision if the spell has one, else pushes to the chain.
        ctx.engine.offer_repeat_or_push(ctx.actor, card, [[]])


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

    # Units enter exhausted (summoning sickness) and ready on the owner's next
    # Awake (ABCD step A). A unit with [Accelerate] may pay an extra cost to
    # enter ready, but that's a SEPARATE decision offered right after this
    # location choice (see below / choose_accelerate).
    card = pending.card
    units.append(PlayedUnit(card=card, location=location, exhausted=True))
    gs.pending_play = None

    unit_index = len(units) - 1
    source_ref = f"{ctx.actor.value}:{unit_index}"
    battlefield = location if location != "base" else None

    # [Accelerate]: open the pay-to-ready decision instead of firing the
    # "when you play me" event now. The event is DEFERRED to choose_accelerate
    # so the decision resolves before any on-play trigger/chain opens (mirrors
    # how [Repeat] sequences its decision). A unit without Accelerate fires the
    # event immediately, exactly as before.
    from ..csv_data import card_accelerate_cost, card_has_accelerate

    if card_has_accelerate(card):
        acc = card_accelerate_cost(card)
        if acc is not None:
            from ..engine import PendingAccelerate

            acc_e, acc_p, acc_dom = acc
            gs.pending_accelerate = PendingAccelerate(
                actor=ctx.actor,
                card=card,
                unit_index=unit_index,
                cost={"energy": acc_e, "power": {acc_dom: acc_p} if acc_p else {}, "any_power": 0},
                source_ref=source_ref,
                battlefield=battlefield,
            )
            return

    # The unit has entered play — fire "when played" triggers. The new unit is
    # the source (its index is the last in the controller's list). Battlefield
    # is set only for non-base locations so "…here" triggers scope correctly.
    from ..triggers import GameEvent

    ctx.engine._emit(
        GameEvent(
            kind="ON_PLAY_UNIT",
            controller=ctx.actor.value,
            source=source_ref,
            battlefield=battlefield,
        )
    )


@register_turn_action("choose_spell_targets")
def _choose_spell_targets(ctx: ActionTurnContext) -> None:
    """Commit the target pick for the phrase a spell is currently choosing.

    Wire format: ``play:choose_spell_targets:<refs>`` where ``<refs>`` is a
    comma-separated list of unit refs (``p1-0`` / ``p2-1`` — controller +
    index into that player's units list). The chosen set must satisfy the
    phrase currently being chosen (count within range, every unit passing the
    per-unit filters, and any group constraint like SAME_LOC / SUM), and may
    not reuse a unit already committed to an earlier phrase.

    For a single-phrase requirement the spell lands on the chain immediately.
    For a multi-phrase pure-AND requirement each call commits one phrase and
    advances; the spell only lands once every phrase has a pick. On the final
    pick the spell goes to the caster's spell stack with ALL chosen targets
    recorded and ``pending_spell_choice`` is cleared.
    """
    from ..requirements import (
        plan_feasible,
        ref_to_token,
        spell_target_plan,
        target_set_satisfies,
        token_to_ref,
    )

    gs = ctx.engine._game_state
    choice = gs.pending_spell_choice
    if choice is None:
        raise ValueError("no spell is waiting for target selection")
    if choice.actor != ctx.actor:
        raise ValueError(
            f"only the caster ({choice.actor.value}) may choose this spell's targets"
        )

    plan = spell_target_plan(choice.requirement)
    idx = len(choice.chosen)
    if not plan or idx >= len(plan):
        # Defensive: pending_spell_choice should only ever hold a requirement
        # with picks left. If it somehow doesn't, push with whatever's chosen.
        targets = [
            f"{c}:{i}"
            for picks in choice.chosen
            for c, i in (token_to_ref(tok) for tok in picks)
        ]
        ctx.engine._push_spell_to_chain(ctx.actor, choice.card, targets)
        gs.pending_spell_choice = None
        return

    req = plan[idx]
    caster = choice.actor
    used = {token_to_ref(tok) for picks in choice.chosen for tok in picks}

    payload = ctx.payload.strip()
    if not payload:
        raise ValueError(
            "play:choose_spell_targets requires target refs "
            "(e.g. play:choose_spell_targets:p1-0)"
        )
    refs = [token_to_ref(tok) for tok in payload.split(",") if tok.strip()]
    if not target_set_satisfies(req, gs, refs, caster=caster, exclude=used):
        raise ValueError(
            f"invalid target set for '{choice.card}': {payload} "
            "(wrong count, a unit doesn't match the requirement, reuses an "
            "already-chosen unit, or a group constraint is violated)"
        )
    # Reject a pick that would leave a later phrase with no distinct units.
    if not plan_feasible(plan[idx + 1 :], gs, caster, used | set(refs)):
        raise ValueError(
            f"invalid target set for '{choice.card}': {payload} "
            "(leaves a later required target with no valid unit)"
        )

    choice.chosen.append([ref_to_token(r) for r in refs])

    if len(choice.chosen) < len(plan):
        # More phrases to pick — stay parked; the next phrase's options are
        # enumerated on the following snapshot.
        return

    # All units picked. MOVE destinations and BATTLEFIELD picks may still be
    # pending — stay parked so their options surface. Otherwise the spell is
    # fully chosen and lands now.
    if not ctx.engine._spell_choice_complete(choice):
        return
    _finalize_spell_choice(ctx, choice)


def _finalize_spell_choice(ctx: ActionTurnContext, choice) -> None:
    """Push a fully-chosen spell onto the chain and clear the pending choice.

    Targets carry the picked unit refs (``player_1:0``) plus, for MOVE spells,
    one ``move_dest:<location>`` token per moved unit (in pick order). The move
    itself is NOT applied yet — the effect is deferred; this only records the
    choices."""
    from ..requirements import token_to_ref

    targets = [
        f"{c}:{i}"
        for picks in choice.chosen
        for c, i in (token_to_ref(tok) for tok in picks)
    ]
    targets += [f"move_dest:{d}" for d in choice.destinations]
    targets += [f"bf:{b}" for b in choice.battlefields]
    targets += [f"gear:{g}" for g in choice.gears]
    targets += [f"trash:{t}" for t in choice.trash]
    targets += [f"spell:{s}" for s in choice.spells]
    targets += [f"location:{loc}" for loc in choice.locations]
    # Accumulate this round's targets onto any prior [Repeat] rounds, then
    # branch: offer another repeat (if the keyword + affordability allow) or
    # push the spell with every round onto the chain.
    rounds = [list(r) for r in choice.prior_rounds] + [targets]
    ctx.engine._game_state.pending_spell_choice = None
    ctx.engine.offer_repeat_or_push(ctx.actor, choice.card, rounds)


@register_turn_action("choose_spell_destination")
def _choose_spell_destination(ctx: ActionTurnContext) -> None:
    """Pick the destination for the next MOVE'd unit awaiting one.

    Wire format: ``play:choose_spell_destination:<location>`` where
    ``<location>`` is ``base`` / ``battlefield_1`` / ``battlefield_2``. Per the
    agreed rules a MOVE spell may relocate the unit anywhere EXCEPT its current
    location (a free move); the relocation/showdown effect is deferred, so this
    only records the chosen destination. Destinations are collected one per
    moved unit, in the order the units were picked; once every moved unit has a
    destination the spell lands on the chain."""
    from ..engine import UNIT_LOCATIONS
    from ..requirements import moved_unit_refs

    gs = ctx.engine._game_state
    choice = gs.pending_spell_choice
    if choice is None:
        raise ValueError("no spell is waiting for a destination")
    if choice.actor != ctx.actor:
        raise ValueError(
            f"only the caster ({choice.actor.value}) may choose this spell's destination"
        )

    moved = moved_unit_refs(choice.requirement, choice.chosen)
    pos = len(choice.destinations)
    if pos >= len(moved):
        raise ValueError(f"'{choice.card}' is not waiting for a destination")

    location = ctx.payload.strip()
    if location not in UNIT_LOCATIONS:
        raise ValueError(
            f"unknown destination {location!r}; expected one of {', '.join(UNIT_LOCATIONS)}"
        )

    # Free move: any location except the unit's current one.
    controller, index = moved[pos]
    units = gs.player_1_units if controller == "player_1" else gs.player_2_units
    current = units[index].location if 0 <= index < len(units) else None
    if location == current:
        raise ValueError(
            f"unit is already at {location!r}; pick a different destination"
        )

    choice.destinations.append(location)

    # Fully chosen (no destinations or battlefield picks left)? Land it.
    if ctx.engine._spell_choice_complete(choice):
        _finalize_spell_choice(ctx, choice)


@register_turn_action("choose_spell_battlefield")
def _choose_spell_battlefield(ctx: ActionTurnContext) -> None:
    """Pick the battlefield for the next BATTLEFIELD phrase awaiting one.

    Wire format: ``play:choose_spell_battlefield:<slot>`` where ``<slot>`` is
    ``battlefield_1`` / ``battlefield_2``. A ``WHERE_FRIENDLY_UNITS`` phrase
    only accepts a battlefield where the caster has a unit. Like the other
    spell choices the pick is recorded (as a ``bf:<slot>`` target token); no
    effect is applied yet. Picks are collected one per BATTLEFIELD phrase, in
    order; once every one has a battlefield the spell lands on the chain."""
    from ..requirements import battlefield_picks

    gs = ctx.engine._game_state
    choice = gs.pending_spell_choice
    if choice is None:
        raise ValueError("no spell is waiting for a battlefield")
    if choice.actor != ctx.actor:
        raise ValueError(
            f"only the caster ({choice.actor.value}) may choose this spell's battlefield"
        )

    picks = battlefield_picks(choice.requirement)
    pos = len(choice.battlefields)
    if pos >= len(picks):
        raise ValueError(f"'{choice.card}' is not waiting for a battlefield")

    slot = ctx.payload.strip()
    if slot not in ("battlefield_1", "battlefield_2"):
        raise ValueError(
            f"unknown battlefield {slot!r}; expected battlefield_1 or battlefield_2"
        )
    if getattr(gs, slot) is None:
        raise ValueError(f"{slot} has not been chosen yet")
    if picks[pos].where_friendly and not ctx.engine._caster_has_unit_at(ctx.actor, slot):
        raise ValueError(
            f"{ctx.actor.value} has no unit at {slot}; this spell needs a "
            "battlefield where you have a friendly unit"
        )

    choice.battlefields.append(slot)

    if ctx.engine._spell_choice_complete(choice):
        _finalize_spell_choice(ctx, choice)


@register_turn_action("choose_spell_gear")
def _choose_spell_gear(ctx: ActionTurnContext) -> None:
    """Pick a gear for the next GEAR phrase awaiting one.

    Wire format: ``play:choose_spell_gear:<gref>`` where ``<gref>`` is a gear
    ref ``g1-<i>`` / ``g2-<i>`` (controller + index into that player's gears).
    Gears carry no side, so any gear on the board may be chosen; a gear already
    picked for this spell can't be reused. Like the other spell choices the
    pick is recorded (as a ``gear:<gref>`` target token); no effect is applied
    yet. Once every GEAR phrase has its gear(s) the spell lands on the chain."""
    from ..requirements import gear_picks

    gs = ctx.engine._game_state
    choice = gs.pending_spell_choice
    if choice is None:
        raise ValueError("no spell is waiting for a gear")
    if choice.actor != ctx.actor:
        raise ValueError(
            f"only the caster ({choice.actor.value}) may choose this spell's gear"
        )

    needed = sum(g.min_count for g in gear_picks(choice.requirement))
    if len(choice.gears) >= needed:
        raise ValueError(f"'{choice.card}' is not waiting for a gear")

    ref = ctx.payload.strip()
    if ref not in ctx.engine._all_gear_refs():
        raise ValueError(f"no gear {ref!r} on the board")
    if ref in choice.gears:
        raise ValueError(f"gear {ref!r} is already chosen for this spell")

    choice.gears.append(ref)

    if ctx.engine._spell_choice_complete(choice):
        _finalize_spell_choice(ctx, choice)


@register_turn_action("choose_spell_trash")
def _choose_spell_trash(ctx: ActionTurnContext) -> None:
    """Pick a trash card for the next TRASH phrase awaiting one.

    Wire format: ``play:choose_spell_trash:<tref>`` where ``<tref>`` is a trash
    ref ``t1-<i>`` / ``t2-<i>`` (controller + index into that player's trash
    pile). The card must match the phrase (side scope, unit-only, energy cap)
    and can't be reused. Recorded as a ``trash:<tref>`` target token; no effect
    is applied yet. Once every TRASH phrase has its card(s) the spell lands."""
    from ..requirements import matching_trash_refs, trash_picks

    gs = ctx.engine._game_state
    choice = gs.pending_spell_choice
    if choice is None:
        raise ValueError("no spell is waiting for a trash card")
    if choice.actor != ctx.actor:
        raise ValueError(
            f"only the caster ({choice.actor.value}) may choose this spell's trash card"
        )

    picks = trash_picks(choice.requirement)
    needed = sum(t.min_count for t in picks)
    if len(choice.trash) >= needed:
        raise ValueError(f"'{choice.card}' is not waiting for a trash card")

    ref = ctx.payload.strip()
    if ref in choice.trash:
        raise ValueError(f"trash card {ref!r} is already chosen for this spell")
    req = picks[min(len(choice.trash), len(picks) - 1)]
    valid = {
        f"{'t1' if c == 'player_1' else 't2'}-{i}"
        for c, i in matching_trash_refs(
            req, list(gs.player_1_trash), list(gs.player_2_trash), ctx.actor.value
        )
    }
    if ref not in valid:
        raise ValueError(f"trash card {ref!r} does not match this spell's requirement")

    choice.trash.append(ref)

    if ctx.engine._spell_choice_complete(choice):
        _finalize_spell_choice(ctx, choice)


@register_turn_action("choose_spell_chain")
def _choose_spell_chain(ctx: ActionTurnContext) -> None:
    """Pick a spell on the chain for the next SPELL phrase (counterspell-style).

    Wire format: ``play:choose_spell_chain:c-<i>`` where ``<i>`` indexes
    ``pending_chain.items`` (``c-0`` = top of chain). The chosen spell must
    match the phrase (side scope, cost caps) and can't be reused. Recorded as a
    ``spell:c-<i>`` target token; no effect is applied yet. Once every SPELL
    phrase has its target the reaction spell lands on the chain."""
    from ..requirements import matching_spell_refs, spell_picks

    gs = ctx.engine._game_state
    choice = gs.pending_spell_choice
    if choice is None:
        raise ValueError("no spell is waiting for a chain-spell target")
    if choice.actor != ctx.actor:
        raise ValueError(
            f"only the caster ({choice.actor.value}) may choose this spell's target"
        )

    picks = spell_picks(choice.requirement)
    needed = sum(s.min_count for s in picks)
    if len(choice.spells) >= needed:
        raise ValueError(f"'{choice.card}' is not waiting for a chain-spell target")

    ref = ctx.payload.strip()
    if ref in choice.spells:
        raise ValueError(f"chain spell {ref!r} is already chosen for this spell")
    req = picks[min(len(choice.spells), len(picks) - 1)]
    chain_items = ctx.engine._chain_items_for_match()
    valid = {f"c-{i}" for i in matching_spell_refs(req, chain_items, ctx.actor.value)}
    if ref not in valid:
        raise ValueError(f"chain spell {ref!r} does not match this spell's requirement")

    choice.spells.append(ref)

    if ctx.engine._spell_choice_complete(choice):
        _finalize_spell_choice(ctx, choice)


@register_turn_action("choose_spell_location")
def _choose_spell_location(ctx: ActionTurnContext) -> None:
    """Pick a location for the next LOCATION phrase awaiting one.

    Wire format: ``play:choose_spell_location:<loc>`` where ``<loc>`` is
    ``base`` / ``battlefield_1`` / ``battlefield_2``. Recorded as a
    ``location:<loc>`` target token; no effect is applied yet. Once every
    LOCATION phrase has its location the spell lands on the chain."""
    from ..engine import UNIT_LOCATIONS
    from ..requirements import location_picks

    gs = ctx.engine._game_state
    choice = gs.pending_spell_choice
    if choice is None:
        raise ValueError("no spell is waiting for a location")
    if choice.actor != ctx.actor:
        raise ValueError(
            f"only the caster ({choice.actor.value}) may choose this spell's location"
        )

    if len(choice.locations) >= len(location_picks(choice.requirement)):
        raise ValueError(f"'{choice.card}' is not waiting for a location")

    loc = ctx.payload.strip()
    if loc not in UNIT_LOCATIONS:
        raise ValueError(
            f"unknown location {loc!r}; expected one of {', '.join(UNIT_LOCATIONS)}"
        )
    if loc != "base" and getattr(gs, loc) is None:
        raise ValueError(f"{loc} has not been chosen yet")

    choice.locations.append(loc)

    if ctx.engine._spell_choice_complete(choice):
        _finalize_spell_choice(ctx, choice)


@register_turn_action("choose_effect_target")
def _choose_effect_target(ctx: ActionTurnContext) -> None:
    """Answer a pending effect choice (a resolving triggered ability that
    needs a player decision — e.g. Abandoned Hall's "may give a unit here
    +1 might").

    Wire format: ``play:choose_effect_target:<token>`` where ``<token>`` is
    one of the choice's enumerated unit tokens (``p1-0`` / ``p2-1``), or
    ``play:choose_effect_target:pass`` to decline (the "may").

    Applies the pick, clears the pending state, then continues the ability's
    remaining effect codes (which may open a new choice).
    """
    from ..effects import EffectContext, apply_choice_effect

    gs = ctx.engine._game_state
    choice = gs.pending_effect_choice
    if choice is None:
        raise ValueError("no effect choice is pending")
    if ctx.actor != choice.actor:
        raise ValueError(
            f"the effect choice belongs to {choice.actor.value}, not {ctx.actor.value}"
        )
    token = ctx.payload.strip().lower()
    if not token:
        raise ValueError(
            "play:choose_effect_target requires a unit token or 'pass' "
            "(e.g. play:choose_effect_target:p1-0)"
        )
    if token != "pass" and token not in choice.options:
        raise ValueError(
            f"{token!r} is not a valid choice "
            f"(options: {', '.join(choice.options)} or pass)"
        )

    gs.pending_effect_choice = None
    if token == "pass":
        ctx.engine._log_event("effect", f"{choice.label}: declined")
    else:
        ectx = EffectContext(
            engine=ctx.engine,
            controller=choice.actor,
            source=choice.source,
            code=choice.code,
            trigger=choice.trigger,
            event_kind=choice.event_kind,
        )
        text = apply_choice_effect(ectx, token)
        ctx.engine._log_event("effect", f"{choice.label}: {text}")

    # Continue the ability where it paused (may pause again on a new choice),
    # then push any triggers the resolution produced.
    ctx.engine._run_effect_codes(
        controller=choice.actor,
        source=choice.source,
        trigger=choice.trigger,
        event_kind=choice.event_kind,
        label=choice.label,
        codes=list(choice.remaining_effects),
    )
    ctx.engine._drain_triggers()


@register_turn_action("choose_repeat")
def _choose_repeat(ctx: ActionTurnContext) -> None:
    """Decide whether to repeat a just-played [Repeat] spell.

    Wire format: ``play:choose_repeat:yes`` (pay the additional cost and
    re-open target selection for another round) or ``play:choose_repeat:no``
    (push the spell onto the chain with every round's targets).

    Only the spell's caster may answer. ``yes`` requires the repeat cost be
    affordable from the caster's CURRENT pools — they bank Energy/Power by
    exhausting/recycling runes first (the same way the base cost is paid),
    which is offered alongside this decision while it's pending.
    """
    from ..csv_data import card_spell_requirement_of
    from ..engine import PendingSpellChoice
    from ..requirements import (
        battlefield_picks,
        gear_picks,
        location_picks,
        spell_picks,
        spell_target_plan,
        trash_picks,
    )

    gs = ctx.engine._game_state
    rep = gs.pending_spell_repeat
    if rep is None:
        raise ValueError("no [Repeat] decision is pending")
    if ctx.actor != rep.actor:
        raise ValueError(
            f"the [Repeat] decision belongs to {rep.actor.value}, not {ctx.actor.value}"
        )
    choice = ctx.payload.strip().lower()
    if choice not in ("yes", "no"):
        raise ValueError("play:choose_repeat requires 'yes' or 'no'")

    if choice == "no":
        gs.pending_spell_repeat = None
        ctx.engine._push_spell_to_chain(rep.actor, rep.card, rep.rounds)
        return

    # yes — must be able to pay the additional cost from current pools.
    if not ctx.engine.can_afford_equip_cost(rep.actor, rep.cost):
        raise ValueError(
            "cannot repeat: the additional cost isn't affordable yet — "
            "exhaust/recycle runes to bank the Energy/Power first"
        )
    ctx.engine._deduct_equip_cost(rep.actor, rep.cost)
    gs.pending_spell_repeat = None

    # Re-open selection for another round. If the spell forces target picks,
    # park a fresh PendingSpellChoice carrying the rounds so far; otherwise
    # there's nothing to pick, so record an empty round and re-offer at once.
    raw_req = card_spell_requirement_of(rep.card)
    has_picks = bool(
        spell_target_plan(raw_req)
        or battlefield_picks(raw_req)
        or gear_picks(raw_req)
        or trash_picks(raw_req)
        or spell_picks(raw_req)
        or location_picks(raw_req)
    )
    if has_picks:
        gs.pending_spell_choice = PendingSpellChoice(
            actor=rep.actor,
            card=rep.card,
            requirement=raw_req or "",
            prior_rounds=[list(r) for r in rep.rounds],
        )
    else:
        ctx.engine.offer_repeat_or_push(rep.actor, rep.card, rep.rounds + [[]])


@register_turn_action("choose_accelerate")
def _choose_accelerate(ctx: ActionTurnContext) -> None:
    """Decide whether to [Accelerate] a just-played unit.

    Wire format: ``play:choose_accelerate:yes`` (pay the additional cost; the
    unit enters READY) or ``play:choose_accelerate:no`` (decline; it stays
    exhausted). Either way the unit's deferred "when you play me" event fires
    once the decision is made.

    Only the unit's controller may answer. ``yes`` requires the Accelerate cost
    be affordable from the controller's CURRENT pools — they bank Energy/Power
    by exhausting/recycling runes first, offered alongside this decision while
    it's pending (compute_accelerate_intents)."""
    from ..engine import RequiredTo as RT
    from ..triggers import GameEvent

    gs = ctx.engine._game_state
    acc = gs.pending_accelerate
    if acc is None:
        raise ValueError("no [Accelerate] decision is pending")
    if ctx.actor != acc.actor:
        raise ValueError(
            f"the [Accelerate] decision belongs to {acc.actor.value}, not {ctx.actor.value}"
        )
    choice = ctx.payload.strip().lower()
    if choice not in ("yes", "no"):
        raise ValueError("play:choose_accelerate requires 'yes' or 'no'")

    if choice == "yes":
        if not ctx.engine.can_afford_equip_cost(acc.actor, acc.cost):
            raise ValueError(
                "cannot Accelerate: the additional cost isn't affordable yet — "
                "exhaust/recycle runes to bank the Energy/Power first"
            )
        ctx.engine._deduct_equip_cost(acc.actor, acc.cost)
        units = gs.player_1_units if acc.actor == RT.PLAYER_1 else gs.player_2_units
        if 0 <= acc.unit_index < len(units):
            units[acc.unit_index].exhausted = False  # enters READY

    gs.pending_accelerate = None

    # Fire the deferred "when you play me" event now that the decision is made.
    ctx.engine._emit(
        GameEvent(
            kind="ON_PLAY_UNIT",
            controller=acc.actor.value,
            source=acc.source_ref,
            battlefield=acc.battlefield,
        )
    )


@register_turn_action("pass_priority")
def _pass_priority(ctx: ActionTurnContext) -> None:
    """Pass priority on the open chain.

    Wire format: ``play:pass_priority`` (no payload). Only the player holding
    priority may pass. Passing hands priority to the opponent; when BOTH
    players pass in a row (no spell added in between) only the TOP item of
    the chain resolves — then priority opens for the owner of the new top
    item, or play returns to the action turn if that was the last item (see
    ``GameEngine._resolve_chain``). Casting a Reaction spell instead of
    passing resets the pass count (handled in ``_push_spell_to_chain``).
    """
    gs = ctx.engine._game_state
    chain = gs.pending_chain
    if chain is None:
        raise ValueError("no chain is open; there is no priority to pass")
    if gs.pending_spell_choice is not None:
        raise ValueError(
            "finish choosing the pending spell's targets before passing priority"
        )
    if gs.pending_effect_choice is not None:
        raise ValueError(
            "resolve the pending effect choice before passing priority"
        )
    if gs.pending_spell_repeat is not None:
        raise ValueError(
            "resolve the pending [Repeat] decision before passing priority"
        )
    if ctx.actor != chain.priority:
        raise ValueError(
            f"priority is with {chain.priority.value}, not {ctx.actor.value}"
        )

    chain.consecutive_passes += 1
    if chain.consecutive_passes >= 2:
        # Both players passed in a row — resolve the chain and reopen the
        # action turn exactly where it was before the first spell was cast.
        ctx.engine._resolve_chain()
    else:
        chain.priority = ctx.engine.opponent_of(chain.priority)


@register_turn_action("equip")
def _equip(ctx: ActionTurnContext) -> None:
    """Attach an Equipment gear the active player has on the board to a chosen
    unit, paying the gear's ``[Equip]`` cost.

    Wire format: ``play:equip:<gear_index>:<controller>:<unit_index>``.
      * ``gear_index`` indexes the ACTIVE player's gears.
      * ``<controller>:<unit_index>`` identifies the target unit — any unit on
        the board (either player's) may be chosen.

    The gear must be an Equipment (Gear tagged Equipment). The [Equip] cost
    (Energy + domain/any Power, parsed from the ability text) is checked and
    deducted. Re-equipping an already-attached Equipment is allowed (it just
    moves to the new unit)."""
    from ..csv_data import card_equip_cost, card_is_equipment
    from ..engine import RequiredTo as RT

    parts = ctx.payload.split(":")
    if len(parts) != 3:
        raise ValueError(
            "play:equip requires <gear_index>:<controller>:<unit_index> "
            "(e.g. play:equip:0:player_1:2)"
        )
    try:
        gear_index = int(parts[0])
        unit_index = int(parts[2])
    except ValueError as e:
        raise ValueError(f"play:equip indices must be integers, got {ctx.payload!r}") from e
    controller = parts[1]
    if controller not in ("player_1", "player_2"):
        raise ValueError(f"unknown controller {controller!r}; expected player_1 or player_2")
    # You may only equip onto a unit YOU control.
    if controller != ctx.actor.value:
        raise ValueError(
            f"{ctx.actor.value} can only equip onto a unit they control, not {controller}'s"
        )

    gs = ctx.engine._game_state
    # Equip is a normal-turn play (action timing), exactly like playing a gear
    # or a unit — NOT a reaction or a showdown play. Enforce it at the engine
    # level so a raw play:equip can't slip in mid-resolution even though the
    # UI only ever offers it on the action turn (compute_equip_intents gates
    # the same conditions). Mirrors the guards in _play_gear.
    if gs.pending_play is not None:
        raise ValueError("a play is already waiting for a location; choose one first")
    if gs.pending_spell_choice is not None:
        raise ValueError(
            "cannot equip while a spell is waiting for target selection — "
            "choose its targets first"
        )
    if gs.pending_chain is not None:
        raise ValueError(
            "cannot equip while the chain is open — pass priority (or respond "
            "with a Reaction) until the chain resolves"
        )
    if gs.pending_showdown is not None:
        raise ValueError(
            "cannot equip while a showdown is in progress — resolve the showdown first"
        )
    if gs.pending_combat is not None:
        raise ValueError(
            "cannot equip while a contested showdown is in combat — commit your kills first"
        )
    if getattr(gs, "pending_spell_repeat", None) is not None:
        raise ValueError("cannot equip while a [Repeat] decision is pending")
    if getattr(gs, "pending_effect_choice", None) is not None:
        raise ValueError("cannot equip while an effect choice is pending")

    gears = gs.player_1_gears if ctx.actor == RT.PLAYER_1 else gs.player_2_gears
    if gear_index < 0 or gear_index >= len(gears):
        raise ValueError(f"gear index out of range: {gear_index}")
    gear = gears[gear_index]
    if not card_is_equipment(gear.card):
        raise ValueError(f"'{gear.card}' is not an Equipment and can't be equipped")

    target_units = gs.player_1_units if controller == "player_1" else gs.player_2_units
    if unit_index < 0 or unit_index >= len(target_units):
        raise ValueError(f"no unit {controller}:{unit_index} on the board")

    cost = card_equip_cost(gear.card)
    if cost is None:
        raise ValueError(f"'{gear.card}' has no parseable [Equip] cost")
    if not ctx.engine.can_afford_equip_cost(ctx.actor, cost):
        energy = cost["energy"]
        power = cost["power"] or {}
        any_power = cost["any_power"]
        raise ValueError(
            f"cannot equip '{gear.card}': can't afford its [Equip] cost "
            f"({energy} Energy + {power} power + {any_power} any) — "
            "produce Energy / recycle runes first"
        )

    ctx.engine._deduct_equip_cost(ctx.actor, cost)
    # Identity is the host's stable uid (positional refs drift when units
    # leave play). attached_to is kept as a display hint of the current pos.
    ctx.engine._backfill_unit_uids()
    gear.attached_uid = target_units[unit_index].uid
    gear.attached_to = f"{controller}:{unit_index}"
    # Stamp the turn so "while attached THIS turn" passives (e.g. Brutalizer's
    # extra +2 Might) can tell a fresh attach from one made a prior turn.
    gear.attached_on_turn = gs.total_turn_number


@register_turn_action("quick_draw")
def _quick_draw(ctx: ActionTurnContext) -> None:
    """Play a ``[Quick-Draw]`` Equipment from hand and attach it for FREE.

    Wire format: ``play:quick_draw:<hand_index>:<unit_controller>:<unit_index>``.

    Quick-Draw gives the equipment [Reaction] timing, so this is allowed in a
    reaction window (an open chain or a showdown) as well as on your own action
    turn — but not while a finer sub-step is mid-resolution. You pay only the
    CARD cost (the gear's printed Energy/Power), NOT the [Equip] cost, and it
    attaches immediately to a unit you control. (Re-attaching later, after the
    host dies, uses the normal play:equip and DOES cost the [Equip].)
    """
    from ..csv_data import card_has_quick_draw
    from ..engine import PlayedGear
    from ..engine import RequiredTo as RT

    parts = ctx.payload.split(":")
    if len(parts) != 3:
        raise ValueError(
            "play:quick_draw requires <hand_index>:<controller>:<unit_index> "
            "(e.g. play:quick_draw:0:player_1:2)"
        )
    try:
        index = int(parts[0])
        unit_index = int(parts[2])
    except ValueError as e:
        raise ValueError(f"play:quick_draw indices must be integers, got {ctx.payload!r}") from e
    controller = parts[1]
    if controller != ctx.actor.value:
        raise ValueError(
            f"{ctx.actor.value} can only Quick-Draw onto a unit they control, not {controller}'s"
        )

    gs = ctx.engine._game_state
    # [Reaction] timing: an open chain / showdown is FINE (that's the window),
    # but you can't slip it in while you're mid-choosing something.
    if gs.pending_play is not None:
        raise ValueError("a play is already waiting for a location; choose one first")
    if gs.pending_spell_choice is not None:
        raise ValueError("cannot Quick-Draw while a spell is waiting for target selection")
    if gs.pending_payment is not None:
        raise ValueError("cannot Quick-Draw while a payment is pending")
    if gs.pending_combat is not None:
        raise ValueError("cannot Quick-Draw while combat damage is being assigned")
    if getattr(gs, "pending_spell_repeat", None) is not None:
        raise ValueError("cannot Quick-Draw while a [Repeat] decision is pending")
    if getattr(gs, "pending_effect_choice", None) is not None:
        raise ValueError("cannot Quick-Draw while an effect choice is pending")

    hand = gs.player_1_hand if ctx.actor == RT.PLAYER_1 else gs.player_2_hand
    gears = gs.player_1_gears if ctx.actor == RT.PLAYER_1 else gs.player_2_gears
    units = gs.player_1_units if ctx.actor == RT.PLAYER_1 else gs.player_2_units
    if hand is None:
        raise ValueError("hand is not initialized")
    if index < 0 or index >= len(hand):
        raise ValueError(f"quick_draw index out of range: {index} (hand size {len(hand)})")
    card = hand[index]
    if not card_has_quick_draw(card):
        raise ValueError(f"'{card}' does not have [Quick-Draw]")
    if unit_index < 0 or unit_index >= len(units):
        raise ValueError(f"no unit to attach to at {controller}:{unit_index}")

    # Pay only the CARD cost (Energy + domain Power), exactly like a normal play.
    energy_cost = ctx.engine.card_energy_cost(card)
    if energy_cost > ctx.engine.player_energy(ctx.actor):
        raise ValueError(
            f"cannot Quick-Draw '{card}': costs {energy_cost} Energy but not enough available"
        )
    if ctx.engine.card_power_cost(card) > 0 and not ctx.engine.can_afford_power_cost(ctx.actor, card):
        raise ValueError(f"cannot Quick-Draw '{card}': can't afford its Power cost")
    if energy_cost > 0:
        ctx.engine.add_energy(ctx.actor, -energy_cost)
    if ctx.engine.card_power_cost(card) > 0:
        ctx.engine._deduct_power_cost(ctx.actor, card)

    # Commit the gear and attach it for free — no [Equip] cost. Resolves
    # immediately; it doesn't go on the chain.
    hand.pop(index)
    ctx.engine._backfill_unit_uids()
    gear = PlayedGear(card=card, location="base", exhausted=False)
    gear.attached_uid = units[unit_index].uid
    gear.attached_to = f"{controller}:{unit_index}"
    gear.attached_on_turn = gs.total_turn_number
    gears.append(gear)


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
    if gs.pending_spell_choice is not None:
        raise ValueError(
            "cannot move units while a spell is waiting for target selection — "
            "choose its targets first"
        )
    if gs.pending_chain is not None:
        raise ValueError(
            "cannot move units while the chain is open — resolve it first "
            "(pass priority or respond with a Reaction)"
        )
    if gs.pending_showdown is not None:
        sd = gs.pending_showdown
        # MUSTER: while the showdown is still unlocked, the initiator may
        # keep moving additional units onto the SAME contested battlefield
        # (reinforce before the fight). Any other move during a showdown —
        # a different player, a different destination, or after the fight
        # has started (locked) / the initiator has passed — is illegal.
        muster_ok = (
            ctx.actor == sd.initiator
            and sd.focus_holder == sd.initiator
            and not sd.locked
            and destination == sd.battlefield
        )
        if not muster_ok:
            raise ValueError(
                "cannot move units while a showdown is in progress — only the "
                "initiator may muster more units onto the contested battlefield, "
                "while they still hold focus and before the fight starts (a card "
                "was played)"
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
    if destination != "base" and dest_controller != ctx.actor and gs.pending_showdown is None:
        from ..engine import PendingShowdown
        gs.pending_showdown = PendingShowdown(
            battlefield=destination,
            initiator=ctx.actor,
        )
    # If a showdown is ALREADY open (this was a muster move), it stays open
    # and unlocked — the initiator can keep reinforcing or end mustering by
    # playing a card / passing.


def _resolve_showdown(ctx: ActionTurnContext) -> None:
    """Resolve a showdown whose FOCUS loop has ended (both players passed
    focus in a row).

      * **Conquest** — only one side has units at the battlefield. That
        player takes control and earns 1 point (capped per-BF-per-turn via
        ``award_bf_point``).
      * **Contested** — both players have units there. Open a
        ``PendingCombat`` damage step; control/scoring wait until the
        damage is assigned and applied.
      * **Empty** — neither side has units: the battlefield goes neutral.

    Clears ``pending_showdown``.
    """
    from ..engine import RequiredTo as RT

    gs = ctx.engine._game_state
    showdown = gs.pending_showdown
    bf = showdown.battlefield
    if bf not in ("battlefield_1", "battlefield_2"):
        raise ValueError(f"unknown showdown battlefield {bf!r}")

    p1_has = any(u.location == bf for u in gs.player_1_units)
    p2_has = any(u.location == bf for u in gs.player_2_units)

    if p1_has and p2_has:
        from ..engine import PendingCombat
        p1_might = ctx.engine.might_at_battlefield(RT.PLAYER_1, bf)
        p2_might = ctx.engine.might_at_battlefield(RT.PLAYER_2, bf)
        gs.pending_combat = PendingCombat(
            battlefield=bf,
            player_1_might=p1_might,
            player_2_might=p2_might,
            player_1_targets=[] if p1_might == 0 else None,
            player_2_targets=[] if p2_might == 0 else None,
        )
        gs.pending_showdown = None
        return

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


@register_turn_action("pass_showdown")
def _pass_showdown(ctx: ActionTurnContext) -> None:
    """Pass FOCUS in a showdown.

    Focus alternates: the holder passes it to the opponent. Two consecutive
    focus passes (no play in between) end the showdown and resolve it (see
    ``_resolve_showdown``). Playing a spell resets the pass counter, so the
    fight only ends once both players decline to act in a row.

    Payload is ignored — passes carry no arguments.
    """
    gs = ctx.engine._game_state
    showdown = gs.pending_showdown
    if showdown is None:
        raise ValueError("no showdown is in progress")

    holder = showdown.focus_holder
    if ctx.actor != holder:
        raise ValueError(
            f"focus is with {holder.value}; {ctx.actor.value} cannot pass it"
        )

    showdown.focus_passes += 1
    if showdown.focus_passes >= 2:
        # Both players passed focus in a row → the fight is over.
        _resolve_showdown(ctx)
    else:
        # Hand focus to the opponent, who may now play / pass.
        showdown.focus = ctx.engine.opponent_of(holder)


def _may_tap_runes(gs, actor) -> bool:
    """Whether ``actor`` may tap (exhaust / recycle) their own runes right now.

    The player on the clock is the one allowed to bank Energy/Power to pay
    for a play:
      * a [Repeat] decision is pending → its caster (banking to afford the
        repeat — "the same shortcut payment way" as the base cost);
      * a chain is open → the priority holder;
      * else a showdown is open → the FOCUS holder (you tap to afford the
        Action/Reaction you're about to play in the showdown);
      * otherwise → the active player.
    """
    repeat = getattr(gs, "pending_spell_repeat", None)
    if repeat is not None:
        return actor == repeat.actor
    chain = gs.pending_chain
    if chain is not None:
        return actor == chain.priority
    showdown = gs.pending_showdown
    if showdown is not None:
        return actor == showdown.focus_holder
    return actor == gs.current_player


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
    if gs.pending_combat is not None:
        raise ValueError(
            "cannot exhaust runes while a contested showdown is in combat — "
            "commit your kills first"
        )
    if not _may_tap_runes(gs, ctx.actor):
        raise ValueError(
            "only the player on the clock may exhaust runes — the active player "
            "normally, the priority holder while a chain is open, or the focus "
            "holder during a showdown"
        )

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
    if gs.pending_combat is not None:
        raise ValueError(
            "cannot recycle runes while a contested showdown is in combat — "
            "commit your kills first"
        )
    if not _may_tap_runes(gs, ctx.actor):
        raise ValueError(
            "only the player on the clock may recycle runes — the active player "
            "normally, the priority holder while a chain is open, or the focus "
            "holder during a showdown"
        )
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


@register_turn_action("assign_damage")
def _assign_damage(ctx: ActionTurnContext) -> None:
    """Commit one player's combat damage assignment in a PendingCombat.

    Payload is a comma-separated list of OPPONENT unit indices the player
    assigns LETHAL damage to (i.e. the units they kill). Empty payload =
    "I kill nothing". The assignment must obey Riftbound's damage rules:

      * the summed Might of the killed units must not exceed the actor's
        damage budget (their total Might at the contested BF); and
      * the assignment must be MAXIMAL — the leftover damage
        (``budget − sum(killed Might)``) must be too small to also kill
        any surviving enemy unit. You must assign lethal to a unit before
        moving on and can't waste damage that could kill another unit.

    Each target index must be unique and refer to an existing opponent
    unit sitting AT the contested battlefield.

    Both players commit independently and BLIND; the engine doesn't reveal
    the opponent's choice until both have committed. As soon as both
    ``player_1_targets`` and ``player_2_targets`` are set, the handler
    calls ``engine.resolve_combat()`` which applies all kills
    simultaneously (killed units → owner's trash) and then the move
    continues by reading the post-combat state for control + scoring.
    """
    from ..csv_data import card_might_of
    from ..engine import RequiredTo as RT

    gs = ctx.engine._game_state
    pc = gs.pending_combat
    if pc is None:
        raise ValueError("no contested combat is awaiting damage assignments")
    if ctx.actor not in (RT.PLAYER_1, RT.PLAYER_2):
        raise ValueError("assign_damage requires player_1 or player_2")

    # Parse the index list. Empty payload is valid — "I kill nothing".
    payload = ctx.payload.strip()
    if payload:
        try:
            target_indices = [int(p.strip()) for p in payload.split(",") if p.strip()]
        except ValueError as e:
            raise ValueError(
                f"play:assign_damage payload must be a comma-separated list of integers, got {payload!r}"
            ) from e
    else:
        target_indices = []
    if len(set(target_indices)) != len(target_indices):
        raise ValueError("play:assign_damage target indices must be unique")

    # Resolve which list we're killing into and our damage budget.
    if ctx.actor == RT.PLAYER_1:
        if pc.player_1_targets is not None:
            raise ValueError("player_1 has already committed their damage for this combat")
        opponent_units = gs.player_2_units
        opp_controller = "player_2"
        budget = pc.player_1_might
    else:
        if pc.player_2_targets is not None:
            raise ValueError("player_2 has already committed their damage for this combat")
        opponent_units = gs.player_1_units
        opp_controller = "player_1"
        budget = pc.player_2_might

    # Gather every assignable enemy unit (at the BF, with a Might value).
    # The stored value is the LETHAL COST = max(Might, 1): a 0-Might unit
    # still needs 1 damage to die, so it isn't free to kill.
    bf_targets: dict[int, int] = {}  # index -> lethal cost
    for i, u in enumerate(opponent_units):
        if u.location != pc.battlefield:
            continue
        if card_might_of(u.card) is None:
            continue
        # Lethal cost = CURRENT Might (printed + buffs + attached equipment),
        # floored at 1 — the SAME value the option enumeration used, so the
        # commit can't disagree with what was offered.
        bf_targets[i] = max(ctx.engine.effective_unit_might(opp_controller, i), 1)

    # Validate every target: assignable, and total Might within budget.
    total_cost = 0
    for idx in target_indices:
        if idx not in bf_targets:
            raise ValueError(
                f"play:assign_damage target index {idx} is not an assignable enemy "
                f"unit at the contested battlefield ({pc.battlefield})"
            )
        total_cost += bf_targets[idx]
    if total_cost > budget:
        raise ValueError(
            f"play:assign_damage total Might of targets ({total_cost}) exceeds "
            f"available damage ({budget})"
        )

    # Enforce the maximal-kill rule: leftover damage must be unable to
    # finish off any surviving enemy unit (otherwise you were required to
    # assign lethal to it too — no wasting damage that could kill).
    leftover = budget - total_cost
    chosen = set(target_indices)
    survivors = [m for i, m in bf_targets.items() if i not in chosen]
    if survivors and leftover >= min(survivors):
        raise ValueError(
            f"play:assign_damage leaves {leftover} damage that must still be "
            f"assigned as lethal to another enemy unit (cheapest survivor needs "
            f"{min(survivors)}) — assign damage until no further unit can be killed"
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
