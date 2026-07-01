"""Data-driven, hot-reloadable effect handlers.

The Implementation-tab agent writes effect SPECS — plain JSON data, never code —
into ``generated_effects.json`` via the server's ``implement_effect`` tool. This
module reads that file at import and registers a REAL engine handler for each
spec, so the effect actually runs. Because the engine runs under
``uvicorn --reload``, writing the JSON makes the new effect live with no manual
restart.

Specs are validated defensively: a malformed spec is skipped (and logged), so a
bad entry can never break engine import or the rest of the registry.

Supported spec kinds (extend here as the agent needs more):
  - ``might_delta``: add/subtract Might from matching units, with an optional
    floor. Fields: code, amount (int), min_might (int|null), target
    ("enemy"|"friendly"|"any"), location ("here"|"any"), select ("all"|"one").
    ``select:"one"`` is a chosen-target choice effect; ``"all"`` hits every
    matching unit. ``here`` is relative to the ability's SOURCE unit location.
  - ``return_from_trash``: move a card out of the controller's trash to a
    destination. Fields: code, filter ("champion"|"unit"|"spell"|"gear"|"any"),
    destination ("hand"|"champion_zone"), optional (bool), condition
    ("champion_zone_empty"|null). Base destinations only; "play" comes later.
"""

from __future__ import annotations

import json
import os

from . import effects as _effects
from .effects import (
    EffectContext,
    _resolve_unit,
    register_choice_effect,
    register_effect,
)

_SPEC_PATH = os.path.join(os.path.dirname(__file__), "generated_effects.json")

#: Codes this module has registered, so reload() can cleanly UNregister them
#: before re-reading the specs (register_* refuses a duplicate code otherwise).
_REGISTERED: set[str] = set()

#: code → options-enumerator(engine, controller, source) -> [wire tokens]. An
#: effect in here picks its target WHEN THE TRIGGER IS PLACED on the chain
#: (rule 402.1): the engine pauses for the pick, locks it on the chain item,
#: and the handler applies to it at resolution. Lets the agent author a
#: "target chosen before it goes on the chain" effect by data alone.
PLACEMENT_TARGETERS: dict = {}


def is_placement_targeted(code: str) -> bool:
    """True if ``code`` chooses its target at trigger-placement time."""
    return code in PLACEMENT_TARGETERS


def placement_options(code: str, engine, controller, source) -> list[str]:
    """Valid pick tokens for a placement-targeted effect, given the source."""
    fn = PLACEMENT_TARGETERS.get(code)
    return fn(engine, controller, source) if fn else []


def _source_location(ctx: EffectContext):
    _units, _idx, unit = _resolve_unit(ctx.engine, ctx.source)
    return unit.location if unit is not None else None


def _matching_units(ctx: EffectContext, target: str, location: str):
    """Yield (controller_str, index, unit) for units matching target+location.
    ``here`` is the SOURCE unit's location (the unit whose ability this is)."""
    gs = ctx.engine._game_state
    me = getattr(ctx.controller, "value", ctx.controller)
    here = _source_location(ctx) if location == "here" else None
    for side, units in (("player_1", gs.player_1_units), ("player_2", gs.player_2_units)):
        if target == "enemy" and side == me:
            continue
        if target == "friendly" and side != me:
            continue
        for i, u in enumerate(units):
            if location == "here" and (here is None or u.location != here):
                continue
            yield side, i, u


def _apply_might_delta(ctx, controller_str, index, unit, amount, min_might):
    """+amount Might; for a reduction with a floor, never take effective Might
    below ``min_might`` (and never buff a unit already at/under the floor)."""
    if amount < 0 and min_might is not None:
        effective = ctx.engine.effective_unit_might(controller_str, index)
        reduction = max(0, min(-amount, effective - min_might))
        unit.bonus_might -= reduction
    else:
        unit.bonus_might += amount


class _Ctx:
    __slots__ = ("engine", "controller", "source")


def _register_unit_effect(spec: dict, apply_to_ref) -> None:
    """Shared wiring for any effect that acts on UNIT(s) matching a target/
    location, in two flavours: ``select:"all"`` hits every match instantly;
    ``select:"one"`` is PLACEMENT-TARGETED — the player picks one matching unit
    when the trigger goes on the chain (rule 402.1), it's locked on the chain
    item, and ``apply_to_ref`` runs against it at resolution.

    ``apply_to_ref(ctx, ref)`` does the actual mutation for one unit ref."""
    code = spec["code"]
    target = spec.get("target", "any")
    location = spec.get("location", "any")
    select = spec.get("select", "all")
    _REGISTERED.add(code)
    if select == "one":
        def _handler(ctx, _apply=apply_to_ref):
            for ref in ctx.targets:
                _apply(ctx, ref)

        register_effect(code, targeted=True)(_handler)

        def _placement_options(engine, controller, source, _t=target, _l=location):
            c = _Ctx()
            c.engine, c.controller, c.source = engine, controller, source
            return [
                ("p1" if cs == "player_1" else "p2") + f"-{i}"
                for cs, i, _u in _matching_units(c, _t, _l)
            ]

        PLACEMENT_TARGETERS[code] = _placement_options
    else:
        def _handler(ctx, _apply=apply_to_ref, _t=target, _l=location):
            for cs, i, _u in _matching_units(ctx, _t, _l):
                _apply(ctx, f"{cs}:{i}")

        register_effect(code)(_handler)


def _register_might_delta(spec: dict) -> None:
    amount = int(spec["amount"])
    min_might = spec.get("min_might")

    def _apply(ctx, ref, _amount=amount, _min=min_might):
        _units, idx, unit = _resolve_unit(ctx.engine, ref)
        if unit is not None:
            _apply_might_delta(ctx, ref.split(":", 1)[0], idx, unit, _amount, _min)

    _register_unit_effect(spec, _apply)


def _register_deal_damage(spec: dict) -> None:
    """Deal N damage to matching unit(s) (rule 417). Uses engine.deal_damage so
    it goes through Bonus Damage + the cleanup lethal sweep like all damage."""
    amount = int(spec["amount"])

    def _apply(ctx, ref, _amount=amount):
        ctx.engine.deal_damage(ref, _amount)

    _register_unit_effect(spec, _apply)


def _register_draw(spec: dict) -> None:
    """The controller draws ``amount`` cards."""
    code = spec["code"]
    n = int(spec.get("amount", 1))
    _REGISTERED.add(code)

    def _handler(ctx, _n=n):
        from .effects import _draw_n

        _draw_n(ctx.engine, ctx.controller, _n)

    register_effect(code)(_handler)


def _register_opponent_discard(spec: dict) -> None:
    """Force the OPPONENT to discard a card YOU choose (Mindsplitter: "the
    opponent reveals their hand, choose a card, they discard it"). This is
    distinct from a SELF-discard (where you discard your own cards) — this
    family is always the opponent's hand, chosen by the controller. A forced
    single pick; an empty opponent hand simply fizzles. Tokens are dh1-/dh2- so
    the UI labels them against the right hand (both hands are in wire state)."""
    code = spec["code"]
    _REGISTERED.add(code)

    def _side(ctx):
        me = getattr(ctx.controller, "value", ctx.controller)
        return "player_2" if me == "player_1" else "player_1"

    def _options(ctx):
        side = _side(ctx)
        gs = ctx.engine._game_state
        hand = (gs.player_1_hand if side == "player_1" else gs.player_2_hand) or []
        pfx = "dh1" if side == "player_1" else "dh2"
        return [f"{pfx}-{i}" for i in range(len(hand))]

    def _apply(ctx, token):
        side = "player_1" if token.startswith("dh1") else "player_2"
        idx = int(token.split("-", 1)[1])
        gs = ctx.engine._game_state
        hand = gs.player_1_hand if side == "player_1" else gs.player_2_hand
        if hand is None or not (0 <= idx < len(hand)):
            return "no card to discard"
        card = hand.pop(idx)
        (gs.player_1_trash if side == "player_1" else gs.player_2_trash).append(card)
        return f"discard {card}"

    register_choice_effect(code, options=_options, apply=_apply, optional=False)


def _register_return_from_trash(spec: dict) -> None:
    """Move a card OUT OF the controller's own trash to a destination (rule:
    "return ... from your trash"). The base supports two destinations:

      - ``hand``          : the card goes to the controller's hand.
      - ``champion_zone`` : only meaningful for the Chosen Champion — it leaves
        trash and ``player_X_champion_played`` is reset to False, so the
        champion can be played again from the Champion Zone (the controller
        still pays its cost). (``play`` — entering play directly — is a later
        pass; it needs the placement machinery and is rejected here for now.)

    ``filter`` narrows what can be picked: ``champion`` (the deck's chosen
    champion only), ``unit``/``spell``/``gear`` (by card type), or ``any``.
    ``optional`` ⇒ a "may" (the controller can decline). ``condition`` is an
    optional gate; ``champion_zone_empty`` only offers the pick when the
    champion isn't currently available to play (it has been played — i.e. it's
    dead/in trash, so the zone is empty). Tokens are t1-/t2- (own trash refs),
    matching the existing trash-ref convention so the UI labels them."""
    code = spec["code"]
    filt = str(spec.get("filter", "any")).lower()
    dest = str(spec.get("destination", "hand")).lower()
    optional = bool(spec.get("optional", True))
    condition = spec.get("condition")
    if dest not in ("hand", "champion_zone"):
        raise ValueError(
            f"destination {dest!r} not supported yet (base family: hand, champion_zone)"
        )
    if filt not in ("any", "champion", "unit", "spell", "gear"):
        raise ValueError(f"filter {filt!r} must be any|champion|unit|spell|gear")
    _REGISTERED.add(code)

    def _side(ctx):
        return getattr(ctx.controller, "value", ctx.controller)

    def _trash(ctx, side):
        gs = ctx.engine._game_state
        return gs.player_1_trash if side == "player_1" else gs.player_2_trash

    def _champ_name(ctx, side):
        gs = ctx.engine._game_state
        deck = gs.player_1_deck if side == "player_1" else gs.player_2_deck
        return deck.chosen_champion if deck is not None else None

    def _champ_played(ctx, side):
        gs = ctx.engine._game_state
        return gs.player_1_champion_played if side == "player_1" else gs.player_2_champion_played

    def _matches(ctx, side, card):
        if filt == "any":
            return True
        if filt == "champion":
            return card == _champ_name(ctx, side)
        from .csv_data import card_type_of

        return (card_type_of(card) or "").lower() == filt

    def _condition_ok(ctx, side):
        if condition == "champion_zone_empty":
            return bool(_champ_played(ctx, side))
        return True

    def _options(ctx, _filt=filt):
        side = _side(ctx)
        if not _condition_ok(ctx, side):
            return []
        pfx = "t1" if side == "player_1" else "t2"
        return [
            f"{pfx}-{i}"
            for i, card in enumerate(_trash(ctx, side))
            if _matches(ctx, side, card)
        ]

    def _apply(ctx, token, _dest=dest):
        side = "player_1" if token.startswith("t1") else "player_2"
        idx = int(token.split("-", 1)[1])
        gs = ctx.engine._game_state
        trash = _trash(ctx, side)
        if not (0 <= idx < len(trash)):
            return "nothing to return"
        card = trash.pop(idx)
        if _dest == "hand":
            (gs.player_1_hand if side == "player_1" else gs.player_2_hand).append(card)
            return f"return {card} to hand"
        # champion_zone: make the champion playable again
        if side == "player_1":
            gs.player_1_champion_played = False
        else:
            gs.player_2_champion_played = False
        return f"return {card} to Champion Zone"

    register_choice_effect(code, options=_options, apply=_apply, optional=optional)


def _register_spend_buff_draw(spec: dict) -> None:
    """"You may spend a buff to draw N" (Monastery of Hirana). The controller
    MAY remove a [Buff] marker from one of their units (the cost) to draw
    ``amount`` cards. Modeled as an OPTIONAL choice: each of the controller's
    buffed units is a pick ("spend this one's buff"); declining — or having no
    buffed unit — does nothing. Spending the buff IS the cost, so the draw only
    happens when a buff is actually spent."""
    code = spec["code"]
    n = int(spec.get("amount", 1))
    _REGISTERED.add(code)

    def _my_units(ctx):
        gs = ctx.engine._game_state
        me = getattr(ctx.controller, "value", ctx.controller)
        return me, (gs.player_1_units if me == "player_1" else gs.player_2_units)

    def _options(ctx):
        me, units = _my_units(ctx)
        pfx = "p1" if me == "player_1" else "p2"
        return [f"{pfx}-{i}" for i, u in enumerate(units) if getattr(u, "buffed", False)]

    def _apply(ctx, token, _n=n):
        from .effects import _draw_n

        _me, units = _my_units(ctx)
        try:
            idx = int(token.split("-", 1)[1])
        except (ValueError, IndexError):
            return "invalid pick"
        if 0 <= idx < len(units) and getattr(units[idx], "buffed", False):
            units[idx].buffed = False  # spend the buff (the cost)
            drew = _draw_n(ctx.engine, ctx.controller, _n)
            return f"spent a buff, drew {drew}"
        return "no buff to spend"

    register_choice_effect(code, options=_options, apply=_apply, optional=True)


_KINDS = {
    "might_delta": _register_might_delta,
    "deal_damage": _register_deal_damage,
    "draw": _register_draw,
    "opponent_discard": _register_opponent_discard,
    "return_from_trash": _register_return_from_trash,
    "spend_buff_draw": _register_spend_buff_draw,
}


def load_generated_effects() -> None:
    """Register every spec in generated_effects.json. Defensive: a bad file or
    a bad spec is skipped, never raised, so engine import always succeeds."""
    try:
        with open(_SPEC_PATH, encoding="utf-8") as f:
            specs = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:  # malformed JSON
        print(f"[generated_effects] could not read {_SPEC_PATH}: {e}")
        return
    if not isinstance(specs, list):
        return
    for spec in specs:
        try:
            kind = spec.get("kind")
            builder = _KINDS.get(kind)
            if builder is None:
                print(f"[generated_effects] unknown kind {kind!r} for {spec.get('code')!r} — skipped")
                continue
            builder(spec)
        except Exception as e:
            print(f"[generated_effects] skipped {spec.get('code')!r}: {e}")


def _unregister_all() -> None:
    """Drop every handler this module previously registered, so reload() can
    re-read the specs without hitting the 'already registered' guard."""
    for code in _REGISTERED:
        _effects._REGISTRY.pop(code, None)
        _effects._CHOICE_REGISTRY.pop(code, None)
        _effects._TARGETED_CODES.discard(code)
        PLACEMENT_TARGETERS.pop(code, None)
    _REGISTERED.clear()


def reload() -> list[str]:
    """Re-register all generated effects from the (possibly just-updated) JSON,
    live — no engine restart. Returns the registered codes."""
    _unregister_all()
    load_generated_effects()
    return sorted(_REGISTERED)


def upsert_spec(spec: dict) -> list[str]:
    """Add or replace one effect spec in generated_effects.json (matched by
    ``code``), then reload so it runs immediately. Returns registered codes."""
    code = str(spec.get("code") or "").strip()
    if not code:
        raise ValueError("spec needs a non-empty 'code'")
    try:
        with open(_SPEC_PATH, encoding="utf-8") as f:
            specs = json.load(f)
        if not isinstance(specs, list):
            specs = []
    except (FileNotFoundError, ValueError):
        specs = []
    specs = [s for s in specs if s.get("code") != code]
    specs.append(spec)
    with open(_SPEC_PATH, "w", encoding="utf-8") as f:
        json.dump(specs, f, indent=2)
        f.write("\n")
    return reload()


load_generated_effects()
