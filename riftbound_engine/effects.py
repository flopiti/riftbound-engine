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


def registered_effect_codes() -> frozenset[str]:
    return frozenset(_REGISTRY)


def is_implemented(code: str) -> bool:
    return code in _REGISTRY


def execute_effect(ctx: EffectContext) -> bool:
    """Run the handler for ``ctx.code``. Returns ``True`` if a real handler
    ran, ``False`` if the code is unimplemented (caller logs a no-op)."""
    handler = _REGISTRY.get(ctx.code)
    if handler is None:
        return False
    handler(ctx)
    return True


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


def _buff_source(ctx: EffectContext, amount: int) -> None:
    _, _, unit = _resolve_unit(ctx.engine, ctx.source)
    if unit is not None:
        unit.bonus_might += amount


@register_effect("GIVE_ME_+1")
def _give_me_1(ctx: EffectContext) -> None:
    _buff_source(ctx, 1)


@register_effect("GIVE_ME_+2M")
def _give_me_2(ctx: EffectContext) -> None:
    _buff_source(ctx, 2)


@register_effect("ADDITIONAL_2M")
def _additional_2m(ctx: EffectContext) -> None:
    _buff_source(ctx, 2)
