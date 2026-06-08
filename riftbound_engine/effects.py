"""Effect registry + executor.

Each ``activeEffect`` code authored in the wizard maps to a handler that
mutates engine state when a triggered ability resolves off the chain. This
mirrors ``action_turn/registry.py``: declare a handler, decorate it with
``@register_effect("CODE")``, and the chain resolver dispatches to it.

Codes without a registered handler are **not** errors — they resolve as a
recorded no-op (logged to the on-screen event feed) so the chain always
drains cleanly. The Implementation Planner tab tracks which codes still need
a real handler; every ``@register_effect`` below is one ticked off.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

# Imported lazily inside handlers to avoid an import cycle with engine.py.


@dataclass
class EffectContext:
    """Everything an effect handler needs to resolve.

    ``engine`` is the live ``GameEngine``; ``controller`` is the player who
    controls the resolving ability (a ``RequiredTo``); ``source`` is the card
    ref that owns it (``"player_1:0"``) when there is one; ``code`` is the
    effect being run.
    """

    engine: object
    controller: object  # RequiredTo
    source: str | None
    code: str
    trigger: str = ""
    event_kind: str = ""
    #: Chosen target unit refs ("player_1:0" form). Set for spell effects
    #: (the units picked to satisfy the Spell Choice Requirement) and any
    #: targeted ability; empty for self/owner-scoped effects.
    targets: tuple[str, ...] = ()


Handler = Callable[[EffectContext], None]
_REGISTRY: dict[str, Handler] = {}


def register_effect(code: str) -> Callable[[Handler], Handler]:
    """Declare the handler for an ``activeEffect`` code."""

    def decorator(fn: Handler) -> Handler:
        if code in _REGISTRY:
            raise ValueError(f"effect {code!r} is already registered")
        _REGISTRY[code] = fn
        return fn

    return decorator


# Pattern handlers — for FAMILIES of codes that differ only by a number
# (e.g. every GIVE_UNIT_+1M / +2M / -3M). One regex covers the whole family,
# so a new amount needs no new registration. Exact handlers in ``_REGISTRY``
# always win; patterns are the fallback (checked in registration order).
_PATTERN_HANDLERS: list[tuple[re.Pattern[str], Handler]] = []


def register_effect_pattern(pattern: str) -> Callable[[Handler], Handler]:
    """Declare a handler for every code matching ``pattern`` (a full-match
    regex). The handler reads ``ctx.code`` to recover the specific amount."""

    compiled = re.compile(pattern)

    def decorator(fn: Handler) -> Handler:
        _PATTERN_HANDLERS.append((compiled, fn))
        return fn

    return decorator


def registered_effect_codes() -> frozenset[str]:
    return frozenset(_REGISTRY)


def is_implemented(code: str) -> bool:
    return code in _REGISTRY or any(p.fullmatch(code) for p, _ in _PATTERN_HANDLERS)


def execute_effect(ctx: EffectContext) -> bool:
    """Run the handler for ``ctx.code``. Returns ``True`` if a real handler
    ran, ``False`` if the code is unimplemented (caller logs a no-op).

    Exact registry first, then pattern handlers (signed-amount families)."""
    handler = _REGISTRY.get(ctx.code)
    if handler is not None:
        handler(ctx)
        return True
    for pattern, fn in _PATTERN_HANDLERS:
        if pattern.fullmatch(ctx.code):
            fn(ctx)
            return True
    return False


# --------------------------------------------------------------------------- #
# Small state accessors. Effects work directly on engine internals; keeping
# the pokes in one place makes the handlers read like rules.
# --------------------------------------------------------------------------- #
def _both_players(engine):
    from .engine import RequiredTo

    return (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2)


def _hand(engine, who) -> list | None:
    from .engine import RequiredTo

    gs = engine._game_state
    return gs.player_1_hand if who == RequiredTo.PLAYER_1 else gs.player_2_hand


def _library(engine, who) -> list | None:
    from .engine import RequiredTo

    gs = engine._game_state
    return gs.player_1_library if who == RequiredTo.PLAYER_1 else gs.player_2_library


def _trash(engine, who) -> list:
    from .engine import RequiredTo

    gs = engine._game_state
    return gs.player_1_trash if who == RequiredTo.PLAYER_1 else gs.player_2_trash


def _rune_pool(engine, who) -> list:
    from .engine import RequiredTo

    gs = engine._game_state
    return gs.player_1_runes if who == RequiredTo.PLAYER_1 else gs.player_2_runes


def _rune_library(engine, who) -> list | None:
    from .engine import RequiredTo

    gs = engine._game_state
    return gs.player_1_rune_library if who == RequiredTo.PLAYER_1 else gs.player_2_rune_library


def _draw_n(engine, who, n: int) -> int:
    """Move up to ``n`` cards from ``who``'s library to hand. Returns the count
    actually drawn (library may run dry)."""
    library = _library(engine, who)
    hand = _hand(engine, who)
    if library is None or hand is None:
        return 0
    drawn = 0
    for _ in range(n):
        if not library:
            break
        hand.append(library.pop(0))
        drawn += 1
    return drawn


def _channel_n(engine, who, n: int, *, exhausted: bool) -> int:
    """Move up to ``n`` runes from ``who``'s rune library into their pool."""
    from .engine import Rune

    pile = _rune_library(engine, who)
    pool = _rune_pool(engine, who)
    if pile is None or pool is None:
        return 0
    moved = 0
    for _ in range(n):
        if not pile:
            break
        rune = pile.pop(0)
        if isinstance(rune, Rune):
            rune.exhausted = exhausted
        pool.append(rune)
        moved += 1
    return moved


def _resolve_unit(engine, ref: str | None):
    """Resolve a ``"player_1:3"`` ref to its (units_list, index, PlayedUnit),
    or ``(None, -1, None)`` if it doesn't point at a live unit."""
    from .engine import RequiredTo

    if not ref or ":" not in ref:
        return None, -1, None
    side, _, idx_s = ref.partition(":")
    try:
        idx = int(idx_s)
    except ValueError:
        return None, -1, None
    gs = engine._game_state
    units = gs.player_1_units if side == RequiredTo.PLAYER_1.value else (
        gs.player_2_units if side == RequiredTo.PLAYER_2.value else None
    )
    if units is None or not (0 <= idx < len(units)):
        return None, -1, None
    return units, idx, units[idx]


# --------------------------------------------------------------------------- #
# Implemented effects. Each is a complete, mechanical Riftbound effect that
# needs no further player choice.
# --------------------------------------------------------------------------- #
@register_effect("DRAW_1")
def _draw_1(ctx: EffectContext) -> None:
    _draw_n(ctx.engine, ctx.controller, 1)


@register_effect("SCORE_1_POINT")
def _score_1(ctx: EffectContext) -> None:
    ctx.engine.add_score(ctx.controller, 1)


@register_effect("CHANNEL_1_RUNE")
def _channel_1(ctx: EffectContext) -> None:
    _channel_n(ctx.engine, ctx.controller, 1, exhausted=False)


@register_effect("CHANNEL_1_RUNE_EXHAUSTED")
def _channel_1_exhausted(ctx: EffectContext) -> None:
    _channel_n(ctx.engine, ctx.controller, 1, exhausted=True)


@register_effect("EACH_PLAYER_CHANNEL_1_RUNE_EXHAUSTED")
def _each_channel_1_exhausted(ctx: EffectContext) -> None:
    for who in _both_players(ctx.engine):
        _channel_n(ctx.engine, who, 1, exhausted=True)


@register_effect("PUT_TOP_2_CARDS_OF_MAIN_DECK_INTO_TRASH")
def _mill_2(ctx: EffectContext) -> None:
    library = _library(ctx.engine, ctx.controller)
    trash = _trash(ctx.engine, ctx.controller)
    if library is None:
        return
    for _ in range(2):
        if not library:
            break
        trash.append(library.pop(0))


@register_effect("OPPONENT_DISCARD_1")
def _opponent_discard_1(ctx: EffectContext) -> None:
    opp = ctx.engine.opponent_of(ctx.controller)
    hand = _hand(ctx.engine, opp)
    trash = _trash(ctx.engine, opp)
    if hand:
        trash.append(hand.pop())


@register_effect("DISCARD_1_DRAW_1")
def _discard_1_draw_1(ctx: EffectContext) -> None:
    hand = _hand(ctx.engine, ctx.controller)
    trash = _trash(ctx.engine, ctx.controller)
    if hand:
        trash.append(hand.pop())
    _draw_n(ctx.engine, ctx.controller, 1)


# --------------------------------------------------------------------------- #
# Might modifiers — one DYNAMIC family. The amount is parsed from the code, so
# every GIVE_UNIT_±NM / GIVE_ME_±N (any N, any sign) is covered by one handler
# each; no new registration when a new value shows up. (Durations aren't
# modelled yet — a "+2 this turn" buff is a permanent ``bonus_might`` bump,
# consistent across all might effects until an end-of-turn layer exists.)
# --------------------------------------------------------------------------- #
#: Trailing signed amount, with an optional "M" suffix ("+2M", "-1M", "+1").
_SIGNED_AMOUNT_RE = re.compile(r"([+-]\d+)M?$")


def _amount_from_code(code: str) -> int:
    m = _SIGNED_AMOUNT_RE.search(code)
    return int(m.group(1)) if m else 0


def _buff_unit(unit, amount: int) -> None:
    if unit is not None:
        unit.bonus_might += amount


def _buff_source(ctx: EffectContext, amount: int) -> None:
    """Apply ``amount`` to the ability's OWN unit (the source)."""
    _, _, unit = _resolve_unit(ctx.engine, ctx.source)
    _buff_unit(unit, amount)


def _buff_targets(ctx: EffectContext, amount: int) -> None:
    """Apply ``amount`` to each chosen target unit."""
    for ref in ctx.targets:
        _, _, unit = _resolve_unit(ctx.engine, ref)
        _buff_unit(unit, amount)


@register_effect_pattern(r"GIVE_UNIT_[+-]\d+M")
def _give_unit_delta(ctx: EffectContext) -> None:
    """GIVE_UNIT_+2M / -1M / +3M …: the chosen target unit(s) get ±N Might.
    With [Repeat] the spell re-runs per round, so the delta stacks."""
    _buff_targets(ctx, _amount_from_code(ctx.code))


@register_effect_pattern(r"GIVE_ME_[+-]\d+M?")
def _give_me_delta(ctx: EffectContext) -> None:
    """GIVE_ME_+1 / +2M …: the source unit gets ±N Might."""
    _buff_source(ctx, _amount_from_code(ctx.code))


# --------------------------------------------------------------------------- #
# CHOICE effects — effects that need a player decision when they resolve.
#
# The engine pauses the resolving ability on these (see
# ``GameEngine._run_effect_codes``): the registered ``options`` builder
# enumerates the valid pick tokens ("p1-0" wire format, same as spell
# targets), the chooser answers via ``play:choose_effect_target:<token|pass>``,
# and the registered ``apply`` runs the actual mutation for the picked token.
# No valid options ⇒ the effect fizzles as a logged no-op without pausing.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChoiceEffect:
    """Option enumerator + applier for one choice-effect code."""

    options: Callable[[EffectContext], list[str]]
    #: Applies the picked token; returns a human line for the event feed.
    apply: Callable[[EffectContext, str], str]


_CHOICE_REGISTRY: dict[str, ChoiceEffect] = {}


def register_choice_effect(
    code: str,
    *,
    options: Callable[[EffectContext], list[str]],
    apply: Callable[[EffectContext, str], str],
) -> None:
    if code in _CHOICE_REGISTRY:
        raise ValueError(f"choice effect {code!r} is already registered")
    _CHOICE_REGISTRY[code] = ChoiceEffect(options=options, apply=apply)


def is_choice_effect(code: str) -> bool:
    return code in _CHOICE_REGISTRY


def choice_effect_options(ctx: EffectContext) -> list[str]:
    return _CHOICE_REGISTRY[ctx.code].options(ctx)


def apply_choice_effect(ctx: EffectContext, token: str) -> str:
    return _CHOICE_REGISTRY[ctx.code].apply(ctx, token)


def _resolve_wire_token(engine, token: str):
    """Resolve a ``p1-0`` / ``p2-1`` wire token to its PlayedUnit (or None)."""
    from .engine import RequiredTo

    m = token.strip().lower()
    if "-" not in m:
        return None
    side, _, idx_s = m.partition("-")
    try:
        idx = int(idx_s)
    except ValueError:
        return None
    gs = engine._game_state
    units = gs.player_1_units if side == "p1" else (gs.player_2_units if side == "p2" else None)
    if units is None or not (0 <= idx < len(units)):
        return None
    return units[idx]


def _units_here_options(ctx: EffectContext) -> list[str]:
    """Wire tokens for every unit the controller has at ``ctx.source``."""
    from .engine import RequiredTo

    gs = ctx.engine._game_state
    p1 = ctx.controller == RequiredTo.PLAYER_1
    units = gs.player_1_units if p1 else gs.player_2_units
    prefix = "p1" if p1 else "p2"
    return [f"{prefix}-{i}" for i, u in enumerate(units) if u.location == ctx.source]


def _give_picked_unit_1m(ctx: EffectContext, token: str) -> str:
    unit = _resolve_wire_token(ctx.engine, token)
    if unit is None:
        raise ValueError(f"{token!r} does not point at a unit in play")
    unit.bonus_might += 1
    return f"+1 Might → {unit.card}"


# Abandoned Hall: "When a player plays a spell, they may give a unit they
# control here +1 might this turn." ``ctx.source`` is the battlefield slot,
# ``ctx.controller`` the player who played the spell; they pick which of
# their units here gets the +1, or pass (the "may").
register_choice_effect(
    "MAY_GIVE_UNIT_HERE_+1M",
    options=_units_here_options,
    apply=_give_picked_unit_1m,
)


@register_effect("ADDITIONAL_2M")
def _additional_2m(ctx: EffectContext) -> None:
    # "+2 Might" on the source — same as GIVE_ME_+2M but a distinct code.
    _buff_source(ctx, 2)


@register_effect("RETURN_TO_HAND")
def _return_to_hand(ctx: EffectContext) -> None:
    """Gust: return the chosen target unit(s) to their owner's hand. The
    Spell Choice Requirement already restricts the pick (a unit at a
    battlefield with ≤3 Might), so here we just move it. Units are resolved
    BEFORE any removal so multiple targets don't shift each other's indices."""
    from .engine import RequiredTo

    pending = []
    for ref in ctx.targets:
        units, _, unit = _resolve_unit(ctx.engine, ref)
        if unit is None:
            continue
        side = ref.split(":", 1)[0]
        owner = RequiredTo.PLAYER_1 if side == RequiredTo.PLAYER_1.value else RequiredTo.PLAYER_2
        pending.append((units, unit, owner))
    for units, unit, owner in pending:
        if unit in units:
            units.remove(unit)
            hand = _hand(ctx.engine, owner)
            if hand is not None:
                hand.append(unit.card)


@register_effect("GIVE_ENEMY_UNITS_-3M_MIN_1")
def _enemy_units_minus_3_min_1(ctx: EffectContext) -> None:
    """Thousand-Tailed Watcher: every ENEMY unit gets -3 Might, but a unit
    can't be reduced below 1 (and a unit already at/below 1 is untouched —
    the floor only caps the reduction, it never buffs)."""
    from .csv_data import card_might_of
    from .engine import RequiredTo

    opp = ctx.engine.opponent_of(ctx.controller)
    gs = ctx.engine._game_state
    units = gs.player_1_units if opp == RequiredTo.PLAYER_1 else gs.player_2_units
    for unit in units:
        effective = (card_might_of(unit.card) or 0) + unit.bonus_might
        reduction = max(0, min(3, effective - 1))  # cap so might floors at 1
        unit.bonus_might -= reduction
