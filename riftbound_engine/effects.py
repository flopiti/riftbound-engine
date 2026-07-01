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

#: Codes whose effect ACTS ON the chosen target(s) (``ctx.targets``). These are
#: the only effects that should fizzle when a spell's target leaves play /
#: stops satisfying the requirement; UNtargeted effects in the same spell (e.g.
#: Stupefy's "Draw 1") still resolve. Populated via ``targeted=True`` below.
_TARGETED_CODES: set[str] = set()
_TARGETED_PATTERNS: list[re.Pattern[str]] = []


def register_effect(code: str, *, targeted: bool = False) -> Callable[[Handler], Handler]:
    """Declare the handler for an ``activeEffect`` code. Pass ``targeted=True``
    when the effect acts on the chosen target(s) so it (and only it) fizzles
    if the target is no longer valid at resolution."""

    def decorator(fn: Handler) -> Handler:
        if code in _REGISTRY:
            raise ValueError(f"effect {code!r} is already registered")
        _REGISTRY[code] = fn
        if targeted:
            _TARGETED_CODES.add(code)
        return fn

    return decorator


# Pattern handlers — for FAMILIES of codes that differ only by a number
# (e.g. every GIVE_UNIT_+1M / +2M / -3M). One regex covers the whole family,
# so a new amount needs no new registration. Exact handlers in ``_REGISTRY``
# always win; patterns are the fallback (checked in registration order).
_PATTERN_HANDLERS: list[tuple[re.Pattern[str], Handler]] = []


def register_effect_pattern(
    pattern: str, *, targeted: bool = False
) -> Callable[[Handler], Handler]:
    """Declare a handler for every code matching ``pattern`` (a full-match
    regex). The handler reads ``ctx.code`` to recover the specific amount.
    Pass ``targeted=True`` when the family acts on the chosen target(s)."""

    compiled = re.compile(pattern)

    def decorator(fn: Handler) -> Handler:
        _PATTERN_HANDLERS.append((compiled, fn))
        if targeted:
            _TARGETED_PATTERNS.append(compiled)
        return fn

    return decorator


def registered_effect_codes() -> frozenset[str]:
    """Every exact code the engine runs as-written — instant handlers AND
    choice effects (``register_choice_effect``). Pattern families aren't exact
    codes, so they're reported by ``is_implemented`` instead."""
    return frozenset(_REGISTRY) | frozenset(_CHOICE_REGISTRY)


def is_implemented(code: str) -> bool:
    """True if the engine has a faithful handler for ``code``: an instant
    effect, a CHOICE effect (exact or family pattern), or a signed-amount
    instant pattern family. Choice effects live in their own registry
    (dispatched via ``is_choice_effect``) but are still 'implemented', so
    ``is_choice_effect`` is folded in here too."""
    return (
        code in _REGISTRY
        or is_choice_effect(code)
        or any(p.fullmatch(code) for p, _ in _PATTERN_HANDLERS)
    )


def effect_uses_targets(code: str) -> bool:
    """True if ``code`` acts on the spell/ability's chosen target(s). Such an
    effect fizzles when the target is gone at resolution; an untargeted effect
    (Draw, Channel, Score, …) in the same ability still resolves."""
    return code in _TARGETED_CODES or any(p.fullmatch(code) for p in _TARGETED_PATTERNS)


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


def _gears(engine, who) -> list:
    from .engine import RequiredTo

    gs = engine._game_state
    return gs.player_1_gears if who == RequiredTo.PLAYER_1 else gs.player_2_gears


def _units(engine, who) -> list:
    from .engine import RequiredTo

    gs = engine._game_state
    return gs.player_1_units if who == RequiredTo.PLAYER_1 else gs.player_2_units


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


@register_effect("PLAY_GOLD_EXHAUSTED")
def _play_gold_exhausted(ctx: EffectContext) -> None:
    """Create a Gold gear token (a Colorless Gear token) under the controller,
    entering EXHAUSTED at their base. The token carries its own activated
    ability ("Kill this, exhaust: Add 1 Power of any domain"), which the engine
    exposes as the ``play:use_gold`` action once the token is ready (it readies
    on the controller's next Awake step). Used by Plundering Poro and other
    Gold-token producers."""
    from .engine import PlayedGear

    _gears(ctx.engine, ctx.controller).append(
        PlayedGear(card="Gold", location="base", exhausted=True, token=True)
    )


#: The 1-Might Recruit unit token. The DE/NX/ZN CSV rows (ogn-271/272/273) are
#: identical — 1 Might, Colorless, tribe "Recruit" — and differ only in art, so
#: the choice is cosmetic; card_might_of/type/domain all resolve off this row.
RECRUIT_TOKEN = "Recruit (DE)"

#: One location literal per UNIT_LOCATIONS entry, used to recognise a ``ctx.source``
#: that is already a bare location (a battlefield ability's slot).
_LOCATION_LITERALS = ("base", "battlefield_1", "battlefield_2")


def _here_location(ctx: EffectContext) -> str:
    """Resolve the "here" location for ``ctx.source`` — i.e. where a "play a
    token HERE" effect drops it. ``source`` may be a bare location literal (a
    battlefield ability), a unit ref ``player_1:3`` (a unit's own ability →
    that unit's current location), a gear ref ``gear:player_1:0`` (gears sit at
    base), or ``None`` (a spell with no source). Anything unresolved falls back
    to ``"base"`` — always a legal destination for a Recruit token."""
    src = ctx.source
    if not src:
        return "base"
    if src in _LOCATION_LITERALS:
        return src
    if src.startswith("gear:"):
        return "base"
    _, _, unit = _resolve_unit(ctx.engine, src)
    return unit.location if unit is not None else "base"


#: ``PLAY_RECRUIT_BASE`` / ``PLAY_RECRUIT_HERE`` and counted variants
#: ``PLAY_<N>_RECRUIT_<BASE|HERE>`` (e.g. PLAY_2_RECRUIT_HERE, PLAY_3_RECRUIT_BASE).
#: One family covers every Recruit-token producer; the count and destination are
#: parsed from the code so a new count needs no new registration.
_RECRUIT_CODE_RE = re.compile(r"PLAY_(?:(\d+)_)?RECRUIT_(BASE|HERE)")


@register_effect_pattern(r"PLAY_(?:\d+_)?RECRUIT_(?:BASE|HERE)")
def _play_recruit(ctx: EffectContext) -> None:
    """Play N 1-might Recruit unit TOKENS for the controller (Altar to Unity,
    Faithful Manufactor, Vanguard Captain, Machine Evangel, Recruit the Vanguard,
    …). ``_BASE`` drops them in the controller's base; ``_HERE`` drops them at
    the source's location (the unit/battlefield whose ability fired). N defaults
    to 1. Each enters EXHAUSTED (summoning sickness) and, as a token
    (``token=True``), ceases to exist — never to trash or hand — when it later
    dies or is bounced.

    Flexible-destination cards ("base OR battlefields you control") are mapped to
    the ``_BASE`` form: base is always a legal destination, so this is rules-legal
    even though it does not offer the per-token location choice."""
    from .engine import PlayedUnit

    m = _RECRUIT_CODE_RE.fullmatch(ctx.code)
    count = int(m.group(1)) if m and m.group(1) else 1
    location = "base" if (m and m.group(2) == "BASE") else _here_location(ctx)
    units = _units(ctx.engine, ctx.controller)
    for _ in range(count):
        units.append(
            PlayedUnit(card=RECRUIT_TOKEN, location=location, exhausted=True, token=True)
        )


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


# OPPONENT_DISCARD_1 ("choose a player. They discard 1.", Bewitching Spirit) is
# a CHOICE effect — the caster picks a player, then THAT player picks a card —
# registered via register_choice_effect further below (after the choice
# machinery is defined), not as an instant handler here.


# NOTE: DISCARD_1 and DISCARD_1_DRAW_1 are CHOICE effects (the player picks
# WHICH card to discard) — registered via register_choice_effect further
# below, not as instant handlers here.


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


@register_effect_pattern(r"GIVE_UNIT_[+-]\d+M", targeted=True)
def _give_unit_delta(ctx: EffectContext) -> None:
    """GIVE_UNIT_+2M / -1M / +3M …: the chosen target unit(s) get ±N Might.
    With [Repeat] the spell re-runs per round, so the delta stacks."""
    _buff_targets(ctx, _amount_from_code(ctx.code))


@register_effect("GIVE_UNIT_+2_AND_ANOTHER_-2", targeted=True)
def _give_unit_plus2_another_minus2(ctx: EffectContext) -> None:
    """Defiant Dance: "Give a unit +2 might this turn and another unit -2 might
    this turn." The requirement is ``ANY UNIT (1)|ANY UNIT (1)``, so the caster
    picks two units in order: the FIRST chosen target gets +2 Might, the SECOND
    gets -2 (this-turn bonus_might, like the other GIVE_UNIT effects)."""
    refs = list(ctx.targets)
    if refs:
        _, _, first = _resolve_unit(ctx.engine, refs[0])
        _buff_unit(first, 2)
    if len(refs) > 1:
        _, _, second = _resolve_unit(ctx.engine, refs[1])
        _buff_unit(second, -2)


def _current_might(unit) -> int:
    """A unit's current Might in play: printed Might + this-turn bonuses
    (matches the per-battlefield Might totals; excludes defend-only Shield)."""
    from .csv_data import card_might_of

    return (card_might_of(unit.card) or 0) + unit.bonus_might


@register_effect("INCREASE_MIGHT_TO", targeted=True)
def _increase_might_to(ctx: EffectContext) -> None:
    """Convergent Mutation: "Choose a friendly unit. This turn, increase its
    Might to the Might of another friendly unit." The requirement picks TWO
    friendly units as an unordered pair, and "increase to" only RAISES, so the
    only beneficial resolution is to bring the lower-Might unit up to the
    higher one's CURRENT Might (printed + buffs), snapshotted now. Equal Might
    ⇒ no change. Fizzles if fewer than two targets survive to resolution."""
    units = []
    for ref in ctx.targets:
        _, _, unit = _resolve_unit(ctx.engine, ref)
        if unit is not None:
            units.append(unit)
    if len(units) < 2:
        return
    lower = min(units, key=_current_might)
    higher = max(units, key=_current_might)
    delta = _current_might(higher) - _current_might(lower)
    if delta > 0:
        lower.bonus_might += delta


@register_effect("GIVE_FRIENDLY_+1M_AND_+1M_IF_ALONE", targeted=True)
def _give_friendly_plus_alone(ctx: EffectContext) -> None:
    """En Garde: "Give a friendly unit +1 Might this turn, then an additional
    +1 Might this turn if it is the only unit you control THERE." So +1 always,
    and +1 more when the target is the lone unit its controller has at its
    location (base / either battlefield). Buff is this-turn ``bonus_might``."""
    for ref in ctx.targets:
        units, _, unit = _resolve_unit(ctx.engine, ref)
        if unit is None:
            continue
        # "you control there" = the friendly target's controller (``units`` is
        # that controller's unit list) at the target's own location.
        here = sum(1 for u in units if u.location == unit.location)
        unit.bonus_might += 2 if here == 1 else 1


@register_effect_pattern(r"GIVE_ME_[+-]\d+M?")
def _give_me_delta(ctx: EffectContext) -> None:
    """GIVE_ME_+1 / +2M …: the source unit gets ±N Might."""
    _buff_source(ctx, _amount_from_code(ctx.code))


# --------------------------------------------------------------------------- #
# [Buff] — a DISTINCT keyword marker, not a Might tweak. "Buff a unit" sets the
# unit's ``buffed`` flag; the +1 Might is read live from that flag in
# ``effective_unit_might``. A buff does NOT stack: buffing a unit that already
# has one is a no-op (matches the rules reminder "If it doesn't have a buff, it
# gets a +1 might buff"). Unlike a ``bonus_might`` bump, a buff PERSISTS across
# turns until spent/removed.
# --------------------------------------------------------------------------- #
def _apply_buff(unit) -> bool:
    """Give ``unit`` a buff if it doesn't already have one. Returns True if a
    buff was newly applied, False if it was already buffed (no stacking) or the
    unit is gone."""
    if unit is None or unit.buffed:
        return False
    unit.buffed = True
    return True


@register_effect("BUFF_ME")
def _buff_me(ctx: EffectContext) -> None:
    """BUFF_ME (Adaptatron, Poro Herder, Cithria, …): the ability's OWN unit
    gains a [Buff] (+1 Might). No-op if it's already buffed — buffs don't stack."""
    _, _, unit = _resolve_unit(ctx.engine, ctx.source)
    _apply_buff(unit)


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
    """Option enumerator + applier for one choice-effect code.

    ``optional`` ⇒ the chooser may decline (a "may" effect, e.g. Abandoned
    Hall). When False the choice is FORCED (e.g. "discard 1"): the engine
    won't offer a pass option, so the player must pick one of ``options``.

    ``on_empty`` runs when there are NO options (nothing to pick) — instead
    of the choice silently fizzling. It lets "do X, then do Y" effects still
    do Y when X is impossible: e.g. DISCARD_1_DRAW_1 with an empty hand has
    nothing to discard, but ``on_empty`` still draws. Returns a feed line (or
    None for a plain fizzle)."""

    options: Callable[[EffectContext], list[str]]
    #: Applies the picked token; returns a human line for the event feed.
    apply: Callable[[EffectContext, str], str]
    optional: bool = True
    #: Ran when ``options`` is empty (no valid pick). Returns a feed line.
    on_empty: Callable[[EffectContext], str | None] | None = None


_CHOICE_REGISTRY: dict[str, ChoiceEffect] = {}

#: Pattern handlers for FAMILIES of choice codes that differ only by a number
#: (e.g. every DISCARD_1_DRAW_<n>). Mirrors ``_PATTERN_HANDLERS`` for instant
#: effects: one regex covers the whole family, so a new amount needs no new
#: registration. Exact codes in ``_CHOICE_REGISTRY`` always win; patterns are
#: the fallback (checked in registration order). The handlers read ``ctx.code``
#: to recover the specific amount.
_CHOICE_PATTERN_HANDLERS: list[tuple[re.Pattern[str], ChoiceEffect]] = []


def register_choice_effect(
    code: str,
    *,
    options: Callable[[EffectContext], list[str]],
    apply: Callable[[EffectContext, str], str],
    optional: bool = True,
    on_empty: Callable[[EffectContext], str | None] | None = None,
) -> None:
    if code in _CHOICE_REGISTRY:
        raise ValueError(f"choice effect {code!r} is already registered")
    _CHOICE_REGISTRY[code] = ChoiceEffect(
        options=options, apply=apply, optional=optional, on_empty=on_empty
    )


def register_choice_effect_pattern(
    pattern: str,
    *,
    options: Callable[[EffectContext], list[str]],
    apply: Callable[[EffectContext, str], str],
    optional: bool = True,
    on_empty: Callable[[EffectContext], str | None] | None = None,
) -> None:
    """Declare one ChoiceEffect for every code matching ``pattern`` (a
    full-match regex). The handlers read ``ctx.code`` to recover the specific
    amount, so a new value in the family needs no new registration."""
    _CHOICE_PATTERN_HANDLERS.append(
        (
            re.compile(pattern),
            ChoiceEffect(options=options, apply=apply, optional=optional, on_empty=on_empty),
        )
    )


def _choice_for(code: str) -> ChoiceEffect | None:
    """Resolve the ChoiceEffect for ``code`` — exact registry first, then
    family patterns (registration order). ``None`` if ``code`` is not a choice
    effect."""
    ce = _CHOICE_REGISTRY.get(code)
    if ce is not None:
        return ce
    for pattern, ce in _CHOICE_PATTERN_HANDLERS:
        if pattern.fullmatch(code):
            return ce
    return None


def is_choice_effect(code: str) -> bool:
    return _choice_for(code) is not None


def choice_effect_options(ctx: EffectContext) -> list[str]:
    return _choice_for(ctx.code).options(ctx)


def choice_effect_has_on_empty(code: str) -> bool:
    """Whether the choice effect has an ``on_empty`` fallback that still does
    something when there are NO options (e.g. DISCARD_1_DRAW_1 still draws on an
    empty hand). Used to decide whether a triggered ability with no legal
    choices is still worth putting on the chain (rule 402.3)."""
    ce = _choice_for(code)
    return ce is not None and ce.on_empty is not None


def choice_effect_optional(code: str) -> bool:
    """Whether the choice may be declined (a "may"). Forced choices return
    False so the engine omits the pass option."""
    ce = _choice_for(code)
    return ce.optional if ce is not None else True


def apply_choice_effect(ctx: EffectContext, token: str) -> str:
    return _choice_for(ctx.code).apply(ctx, token)


def run_choice_effect_on_empty(ctx: EffectContext) -> str | None:
    """Run the choice's ``on_empty`` fallback (when there were no options),
    or ``None`` if it has none. Lets a combined effect still do its non-choice
    half (e.g. DISCARD_1_DRAW_1 still draws when the hand is empty)."""
    ce = _choice_for(ctx.code)
    if ce is None or ce.on_empty is None:
        return None
    return ce.on_empty(ctx)


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


# --------------------------------------------------------------------------- #
# Gear-kill choice effect. "You may kill a gear" — an OPTIONAL pick over every
# gear on the board (gears carry no side qualifier, so either player's gear is
# fair game). Tokens are ``g1-<i>`` / ``g2-<i>``, the same wire refs the engine
# already uses for gear targets (``GameEngine._all_gear_refs``). No gears in
# play ⇒ no options ⇒ the choice fizzles silently as a logged no-op.
# --------------------------------------------------------------------------- #
def _gear_from_token(engine, token: str):
    """``"g1-0"`` / ``"g2-1"`` → (owner, gears_list, index) or ``None``."""
    from .engine import RequiredTo

    m = token.strip().lower()
    side, _, idx_s = m.partition("-")
    try:
        idx = int(idx_s)
    except ValueError:
        return None
    if side == "g1":
        owner = RequiredTo.PLAYER_1
    elif side == "g2":
        owner = RequiredTo.PLAYER_2
    else:
        return None
    gears = _gears(engine, owner)
    if not (0 <= idx < len(gears)):
        return None
    return owner, gears, idx


def _all_gear_options(ctx: EffectContext) -> list[str]:
    """Wire tokens for every gear on the board, either side."""
    return ctx.engine._all_gear_refs()


def _kill_picked_gear(ctx: EffectContext, token: str) -> str:
    """Remove the chosen gear from play. A real card goes to its owner's trash;
    a gear TOKEN (e.g. Gold) ceases to exist (mirrors token handling in
    ``_return_to_hand``). Detaching is implicit — popping the gear from the
    list drops any equipment grant, since Might is read live from the list."""
    loc = _gear_from_token(ctx.engine, token)
    if loc is None:
        raise ValueError(f"{token!r} does not point at a gear in play")
    owner, gears, idx = loc
    gear = gears.pop(idx)
    if not getattr(gear, "token", False):
        _trash(ctx.engine, owner).append(gear.card)
    return f"kill {gear.card}"


register_choice_effect(
    "YOU_MAY_KILL_GEAR",
    options=_all_gear_options,
    apply=_kill_picked_gear,
)


# --------------------------------------------------------------------------- #
# Adaptatron: "When I conquer, you may kill a gear. If you do, buff me." This is
# one OPTIONAL decision with an "if you do" rider — modelled exactly like
# DISCARD_1_DRAW_N (the follow-up lives INSIDE the choice's apply, so it only
# happens when the player actually picks). Declining (the "may") or having no
# gears to kill ⇒ the choice fizzles and NO buff is applied, which is the
# correct "if you do" gate. Authoring a card with the inert
# ``cost: YOU_MAY_KILL_GEAR`` + ``condition: IF_YOU_KILL_GEAR`` pair does NOT
# work (the engine can't charge a gear-kill cost, nor evaluate that condition);
# this single combined effect is the supported way to express it.
# --------------------------------------------------------------------------- #
def _kill_gear_then_buff_me(ctx: EffectContext, token: str) -> str:
    line = _kill_picked_gear(ctx, token)
    _, _, unit = _resolve_unit(ctx.engine, ctx.source)
    buffed = _apply_buff(unit)
    return f"{line}, buff me" if buffed else f"{line} (already buffed)"


register_choice_effect(
    "KILL_GEAR_BUFF_ME",
    options=_all_gear_options,
    apply=_kill_gear_then_buff_me,
    optional=True,  # "you MAY kill a gear"
    # No ``on_empty``: no gears in play ⇒ nothing to kill ⇒ no buff ("if you do").
)


# --------------------------------------------------------------------------- #
# Hand-card choice effects (discard). Tokens are ``h-<i>`` hand indices for
# the effect's controller — the choice machinery is token-agnostic, so each
# effect interprets its own token scheme.
# --------------------------------------------------------------------------- #
def _own_hand_options(ctx: EffectContext) -> list[str]:
    """One ``h-<i>`` token per card in the controller's hand."""
    hand = _hand(ctx.engine, ctx.controller) or []
    return [f"h-{i}" for i in range(len(hand))]


def _hand_index_from_token(token: str) -> int | None:
    m = token.strip().lower()
    if not m.startswith("h-"):
        return None
    try:
        return int(m[2:])
    except ValueError:
        return None


def _discard_picked(ctx: EffectContext, token: str) -> str:
    """Move the chosen hand card to the controller's trash. Returns a feed
    line; raises if the token doesn't point at a hand card."""
    i = _hand_index_from_token(token)
    hand = _hand(ctx.engine, ctx.controller)
    trash = _trash(ctx.engine, ctx.controller)
    if i is None or hand is None or not (0 <= i < len(hand)):
        raise ValueError(f"{token!r} does not point at a card in hand")
    card = hand.pop(i)
    trash.append(card)
    return f"discard {card}"


# Standalone "discard 1" — a FORCED choice (you pick WHICH card, but can't
# decline). Composable: tag a card ``[DISCARD_1, DRAW_1]`` and the engine
# pauses on the discard, then runs the instant DRAW_1 next — so the draw
# happens even if the hand is emptied by the discard.
register_choice_effect(
    "DISCARD_1",
    options=_own_hand_options,
    apply=_discard_picked,
    optional=False,
)


# Combined "discard 1, then draw N" — a DYNAMIC family (Zaun Warrens is
# DISCARD_1_DRAW_1). Single forced choice: pick a card to discard, then draw N.
# With an empty hand there's nothing to discard, so the choice has no options —
# but the "then draw N" still happens via on_empty (you just don't discard). One
# pattern covers every N; the count is parsed from ``ctx.code`` so a new value
# (DISCARD_1_DRAW_3, …) lights up with no new registration.
_DISCARD_DRAW_RE = re.compile(r"DISCARD_1_DRAW_(\d+)")


def _discard_draw_count(code: str) -> int:
    m = _DISCARD_DRAW_RE.fullmatch(code)
    return int(m.group(1)) if m else 0


def _discard_then_draw_apply(ctx: EffectContext, token: str) -> str:
    line = _discard_picked(ctx, token)
    # Report the ACTUAL draw count — _draw_n stops when the library runs dry, so
    # a short deck reads honestly rather than claiming a full draw.
    drawn = _draw_n(ctx.engine, ctx.controller, _discard_draw_count(ctx.code))
    return f"{line}, draw {drawn}" if drawn else f"{line} (deck empty, no draw)"


def _discard_then_draw_on_empty(ctx: EffectContext) -> str:
    drawn = _draw_n(ctx.engine, ctx.controller, _discard_draw_count(ctx.code))
    return f"no card to discard, draw {drawn}" if drawn else "no card to discard, deck empty"


register_choice_effect_pattern(
    r"DISCARD_1_DRAW_\d+",
    options=_own_hand_options,
    apply=_discard_then_draw_apply,
    optional=False,
    on_empty=_discard_then_draw_on_empty,
)


# OPPONENT_DISCARD_1 — Bewitching Spirit: "When you play me, choose a player.
# They discard 1." Two decisions by (potentially) two players: the caster picks
# WHICH player, then THAT player picks WHICH card. Modeled as a forced player
# pick whose apply opens a second forced DISCARD_1 choice owned by the chosen
# player (so they choose their own card, not the caster).
def _choose_a_player_options(ctx: EffectContext) -> list[str]:
    from .engine import RequiredTo

    return [RequiredTo.PLAYER_1.value, RequiredTo.PLAYER_2.value]


def _chosen_player_discards(ctx: EffectContext, token: str) -> str:
    from .engine import RequiredTo

    try:
        chosen = RequiredTo(token)
    except ValueError:
        raise ValueError(f"{token!r} is not a player")
    if not (_hand(ctx.engine, chosen) or []):
        return f"{token} has no cards to discard"
    # Hand off to a forced discard owned by the chosen player — THEY pick.
    ctx.engine._open_effect_choice(
        actor=chosen,
        code="DISCARD_1",
        source=ctx.source,
        label=f"{token} discards 1",
        trigger=ctx.trigger,
        event_kind=ctx.event_kind,
    )
    return f"{token} to discard 1"


register_choice_effect(
    "OPPONENT_DISCARD_1",
    options=_choose_a_player_options,
    apply=_chosen_player_discards,
    optional=False,
)


@register_effect("ADDITIONAL_2M")
def _additional_2m(ctx: EffectContext) -> None:
    # "+2 Might" on the source — same as GIVE_ME_+2M but a distinct code.
    _buff_source(ctx, 2)


@register_effect("RETURN_TO_HAND", targeted=True)
def _return_to_hand(ctx: EffectContext) -> None:
    """Gust: return the chosen target unit(s) to their owner's hand. The
    Spell Choice Requirement already restricts the pick (a unit at a
    battlefield with ≤3 Might), so here we just move it. Units are resolved
    BEFORE any removal so multiple targets don't shift each other's indices."""
    from .engine import RequiredTo

    from .triggers import GameEvent

    pending = []
    for ref in ctx.targets:
        units, _, unit = _resolve_unit(ctx.engine, ref)
        if unit is None:
            continue
        side = ref.split(":", 1)[0]
        owner = RequiredTo.PLAYER_1 if side == RequiredTo.PLAYER_1.value else RequiredTo.PLAYER_2
        pending.append((units, unit, owner, unit.location))
    for units, unit, owner, origin in pending:
        if unit in units:
            units.remove(unit)
            is_token = getattr(unit, "token", False)
            # Tokens are not real cards: bouncing one removes it from play but
            # it ceases to exist rather than returning to hand.
            if not is_token:
                hand = _hand(ctx.engine, owner)
                if hand is not None:
                    hand.append(unit.card)
            # "When a unit here is returned to a player's hand" (Ripper's Bay).
            # The owner is the player who gets the unit back; origin is where it
            # sat, so an ORIGIN_HERE battlefield ability can match.
            ctx.engine._emit(
                GameEvent(
                    kind="ON_RETURN_TO_HAND",
                    controller=owner.value,
                    data={"origin": origin, "token": is_token},
                )
            )


@register_effect("MAY_MOVE_ENEMY_UNIT", targeted=True)
def _may_move_enemy_unit(ctx: EffectContext) -> None:
    """Charm: move the chosen enemy unit to the location the caster picked.

    Both halves are the spell's Spell Choice Requirement ("MOVE ENEMY UNIT"),
    captured at cast: by resolution ``ctx.targets`` is the picked unit ref(s)
    followed by one ``move_dest:<location>`` token per moved unit (in pick
    order). An effect-driven move is a FREE relocation — it does NOT exhaust the
    unit and may send it anywhere (base <-> either battlefield); the cast-time
    destination options already enforced "any location except the current one".

    Units are re-located by uid before this runs, so a target that left play is
    already dropped (and the round fizzles via the requirement re-check)."""
    from .engine import UNIT_LOCATIONS

    from .triggers import GameEvent

    unit_refs = [t for t in ctx.targets if t.startswith(("player_1:", "player_2:"))]
    dests = [t.split(":", 1)[1] for t in ctx.targets if t.startswith("move_dest:")]
    for ref, dest in zip(unit_refs, dests):
        if dest not in UNIT_LOCATIONS:
            continue
        _, _, unit = _resolve_unit(ctx.engine, ref)
        if unit is not None:
            unit.location = dest
            # "When you move an enemy unit" (Blast Cone). Fires for the player
            # who did the moving; the moved unit rides along as the target.
            ctx.engine._emit(
                GameEvent(
                    kind="ON_MOVE_ENEMY",
                    controller=ctx.controller.value,
                    data={"unit": ref},
                )
            )


@register_effect("MOVE_FRIENDLY_UNIT_BF_TO_BASE", targeted=True)
def _move_friendly_unit_bf_to_base(ctx: EffectContext) -> None:
    """The Syren: move the chosen friendly unit from a battlefield to its base.

    An activated Gear ability ("1 energy, exhaust"), so unlike the MOVE spells
    there is NO destination pick — the destination is fixed (base). The target
    is picked up front via the derived "FRIENDLY UNIT (1[BF])" requirement
    (a friendly unit currently AT a battlefield), captured by uid and
    re-validated at resolution; if it left the battlefield first the effect
    fizzles. The move is a free relocation — it does NOT exhaust the unit."""
    for ref in ctx.targets:
        if not ref.startswith(("player_1:", "player_2:")):
            continue
        _, _, unit = _resolve_unit(ctx.engine, ref)
        if unit is not None:
            unit.location = "base"


# --------------------------------------------------------------------------- #
# Move / ready effects (the "move a unit" + "ready a unit/legend" family). The
# ones that need the player to PICK a unit use the choice mechanism (mirroring
# GIVE_UNIT_SHIELD); the spell that targets up front (Ride the Wind) reads
# ``ctx.targets`` like the other MOVE spells. All moves are FREE relocations —
# they never exhaust the moved unit and never open a showdown.
# --------------------------------------------------------------------------- #
def _wire_for(controller_value: str, index: int) -> str:
    """``p1-<i>`` / ``p2-<i>`` wire token (the choice machinery's unit handle)."""
    return f"{'p1' if controller_value == 'player_1' else 'p2'}-{index}"


def _friendly_units_here_options(ctx: EffectContext) -> list[str]:
    """The controller's units at the source's location ("here")."""
    here = _here_location(ctx)
    units = _units(ctx.engine, ctx.controller)
    return [_wire_for(ctx.controller.value, i) for i, u in enumerate(units) if u.location == here]


def _units_at_battlefield_options(ctx: EffectContext) -> list[str]:
    """EVERY unit (either player) currently standing at a battlefield."""
    gs = ctx.engine._game_state
    out: list[str] = []
    for pv, units in (("player_1", gs.player_1_units), ("player_2", gs.player_2_units)):
        out += [
            _wire_for(pv, i)
            for i, u in enumerate(units)
            if u.location in ("battlefield_1", "battlefield_2")
        ]
    return out


def _enemy_units_options(ctx: EffectContext) -> list[str]:
    """Every unit the OPPONENT of the controller has in play (any location)."""
    opp = ctx.engine.opponent_of(ctx.controller)
    units = _units(ctx.engine, opp)
    return [_wire_for(opp.value, i) for i in range(len(units))]


def _friendly_units_anywhere_options(ctx: EffectContext) -> list[str]:
    """Every unit the controller has in play (any location)."""
    units = _units(ctx.engine, ctx.controller)
    return [_wire_for(ctx.controller.value, i) for i in range(len(units))]


def _move_picked_to_base(ctx: EffectContext, token: str) -> str:
    unit = _resolve_wire_token(ctx.engine, token)
    if unit is None:
        raise ValueError(f"{token!r} does not point at a unit in play")
    unit.location = "base"
    return f"move {unit.card} to base"


def _move_picked_here(ctx: EffectContext, token: str) -> str:
    unit = _resolve_wire_token(ctx.engine, token)
    if unit is None:
        raise ValueError(f"{token!r} does not point at a unit in play")
    dest = _here_location(ctx)
    unit.location = dest
    return f"move {unit.card} to {dest}"


def _ready_picked_unit(ctx: EffectContext, token: str) -> str:
    unit = _resolve_wire_token(ctx.engine, token)
    if unit is None:
        raise ValueError(f"{token!r} does not point at a unit in play")
    unit.exhausted = False
    return f"ready {unit.card}"


# Reaver's Row: "when you defend here, you may move a FRIENDLY unit HERE to base."
register_choice_effect(
    "MAY_MOVE_FRIENDLY_UNIT_HERE_TO_BASE",
    options=_friendly_units_here_options,
    apply=_move_picked_to_base,
)
# Amateur Recital: "when you hold here, you may move a unit at A battlefield (any
# of them, either player's) to its base."
register_choice_effect(
    "MAY_MOVE_UNIT_AT_BF_TO_BASE",
    options=_units_at_battlefield_options,
    apply=_move_picked_to_base,
)
# Star Spring: "the first time a player plays a non-token unit here each turn,
# they may move another unit they control here to its base." (The "another"
# exclusion needs the just-played unit's identity, which the trigger doesn't yet
# carry, so this offers the controller's units here — they simply won't pick the
# one they just played.)
register_choice_effect(
    "MAY_MOVE_ANOTHER_UNIT_TO_BASE",
    options=_friendly_units_here_options,
    apply=_move_picked_to_base,
)
# Evelynn, Entrancing: "move an enemy unit … here" (to Evelynn's location).
register_choice_effect(
    "MAY_MOVE_ENEMY_UNIT_HERE",
    options=_enemy_units_options,
    apply=_move_picked_here,
)
# Blood Rose: "Spend 3 XP, exhaust: Ready a unit." (The 3XP cost is not yet
# payable — XP isn't modelled — so the card stays partial; the effect is real.)
register_choice_effect(
    "READY_UNIT",
    options=_friendly_units_anywhere_options,
    apply=_ready_picked_unit,
    optional=False,
)


@register_effect("MOVE_FRIENDLY_UNIT_AND_READY", targeted=True)
def _move_friendly_and_ready(ctx: EffectContext) -> None:
    """Ride the Wind: "Move a friendly unit and ready it." The spell's
    ``MOVE FRIENDLY UNIT (1)`` requirement captures the chosen unit and its
    destination up front; here we relocate it (free move) and clear exhaustion."""
    from .engine import UNIT_LOCATIONS

    unit_refs = [t for t in ctx.targets if t.startswith(("player_1:", "player_2:"))]
    dests = [t.split(":", 1)[1] for t in ctx.targets if t.startswith("move_dest:")]
    for i, ref in enumerate(unit_refs):
        _, _, unit = _resolve_unit(ctx.engine, ref)
        if unit is None:
            continue
        if i < len(dests) and dests[i] in UNIT_LOCATIONS:
            unit.location = dests[i]
        unit.exhausted = False  # "and ready it"


@register_effect("CANT_MOVE", targeted=True)
def _cant_move(ctx: EffectContext) -> None:
    """Mark the chosen target unit(s) as unable to move this turn (Vex). Resets
    at end of turn alongside this-turn buffs; the move-option builders skip a
    unit flagged this way."""
    for ref in ctx.targets:
        if not ref.startswith(("player_1:", "player_2:")):
            continue
        _, _, unit = _resolve_unit(ctx.engine, ref)
        if unit is not None:
            unit.cant_move = True


@register_effect("READY_LEGEND")
def _ready_legend(ctx: EffectContext) -> None:
    """Hall of Legends: "ready your legend." Clears the controller's legend's
    exhausted flag (see GameState.player_X_legend_exhausted)."""
    ctx.engine.set_legend_exhausted(ctx.controller, False)


@register_effect("READY_2_RUNES_AT_END_TURN")
def _ready_2_runes_at_end_turn(ctx: EffectContext) -> None:
    """Targon's Peak: "ready up to 2 runes at the end of this turn." Schedules a
    deferred ready that the engine applies when the turn ends (in _advance_turn);
    multiple sources add up."""
    ctx.engine.schedule_end_turn_ready_runes(ctx.controller, 2)


# Emperor's Dais: "return a unit you control here to its owner's hand. If you do,
# play a 2-might Sand Soldier token here." The RETURN half is implemented as a
# forced choice (pick one of your units here); the Sand-Soldier token spawn is
# pending a token card row (absent from the card data), so it's not yet played.
def _return_picked_unit_to_hand(ctx: EffectContext, token: str) -> str:
    unit = _resolve_wire_token(ctx.engine, token)
    if unit is None:
        raise ValueError(f"{token!r} does not point at a unit in play")
    units = _units(ctx.engine, ctx.controller)
    if unit in units:
        units.remove(unit)
        if not getattr(unit, "token", False):
            hand = _hand(ctx.engine, ctx.controller)
            if hand is not None:
                hand.append(unit.card)
    return f"return {unit.card} to hand (Sand Soldier token pending)"


register_choice_effect(
    "RETURN_UNIT_YOU_CONTROL_HERE_HAND_PLAY_SS_HERE",
    options=_friendly_units_here_options,
    apply=_return_picked_unit_to_hand,
)


def _shield_grant_amount(ctx: EffectContext) -> int:
    """[Shield N] amount the granting card confers (Fortified Position → 2),
    read from its reminder text; 1 if unspecified."""
    from .csv_data import card_ability_of, shield_amount_in_text

    src_card = ctx.engine._card_name_for_ref(ctx.source)
    return shield_amount_in_text(card_ability_of(src_card) if src_card else "")


def _give_picked_unit_shield(ctx: EffectContext, token: str) -> str:
    """Grant the chosen unit [Shield N] this turn (bonus_shield, expires at end
    of turn; only matters while the unit defends)."""
    unit = _resolve_wire_token(ctx.engine, token)
    if unit is None:
        raise ValueError(f"{token!r} does not point at a unit in play")
    amount = _shield_grant_amount(ctx)
    unit.bonus_shield += amount
    return f"[Shield {amount}] → {unit.card}"


# Fortified Position: "When you defend here, choose a unit. It gains [Shield N]
# this turn." A triggered (battlefield) ability with no Spell Choice
# Requirement, so the target is picked through the choice mechanism — the
# defender chooses one of their units HERE (``ctx.source`` is the battlefield
# slot). Forced (not a "may"); if somehow no units are here, it just does
# nothing.
register_choice_effect(
    "GIVE_UNIT_SHIELD",
    options=_units_here_options,
    apply=_give_picked_unit_shield,
    optional=False,
)


@register_effect("COUNTER_SPELL", targeted=True)
def _counter_spell(ctx: EffectContext) -> None:
    """Defy: counter the chosen spell on the chain. The target was captured by
    its stable ``cid`` (token ``spell_cid:<n>``) at cast time, so we re-locate
    it now even though the chain shifted. Defy is a [Reaction] resolving on TOP
    of the chain, so its target is still sitting below it — we remove it before
    it can resolve and send it to its caster's trash WITHOUT running its effect
    or firing ON_PLAY_SPELL. If the target already left the chain (resolved or
    countered first), this is a clean no-op."""
    from .engine import RequiredTo

    gs = ctx.engine._game_state
    for token in ctx.targets:
        if not token.startswith("spell_cid:"):
            continue
        rest = token.split(":", 1)[1]
        if not rest.lstrip("-").isdigit():
            continue
        idx, item = ctx.engine._chain_item_by_cid(int(rest))
        # Only card-spells can be countered (not triggered-ability items), and
        # only if still on the chain.
        if item is None or item.card is None or gs.pending_chain is None:
            continue
        del gs.pending_chain.items[idx]
        trash = (
            gs.player_1_trash if item.actor == RequiredTo.PLAYER_1 else gs.player_2_trash
        )
        trash.append(item.card)
        ctx.engine._log_event("effect", f"{item.card} countered → trash")


@register_effect("GIVE_ENEMY_UNITS_-3M_MIN_1")
def _enemy_units_minus_3_min_1(ctx: EffectContext) -> None:
    """Thousand-Tailed Watcher: every ENEMY unit gets -3 Might, but a unit
    can't be reduced below 1 (and a unit already at/below 1 is untouched —
    the floor only caps the reduction, it never buffs)."""
    from .engine import RequiredTo

    opp = ctx.engine.opponent_of(ctx.controller)
    gs = ctx.engine._game_state
    units = gs.player_1_units if opp == RequiredTo.PLAYER_1 else gs.player_2_units
    for i, unit in enumerate(units):
        # CURRENT Might includes buffs AND attached-equipment grants, so the
        # -3 (floored at 1) reduces against the unit's real in-play Might.
        effective = ctx.engine.effective_unit_might(opp.value, i)
        reduction = max(0, min(3, effective - 1))  # cap so might floors at 1
        unit.bonus_might -= reduction


# --------------------------------------------------------------------------- #
# Equipment self-effects. These resolve from an ``equip:<ctrl>:<gidx>:<huid>``
# source token (see GameEngine._abilities_in_play) so "this" = the gear and
# "self" = the equipped unit, addressed by stable uid.
# --------------------------------------------------------------------------- #
def _parse_equip_source(source: str | None) -> tuple[str, int, int] | None:
    """``"equip:<controller>:<gear-index>:<host-uid>"`` → (controller, gidx, huid)."""
    if not source or not source.startswith("equip:"):
        return None
    parts = source.split(":")
    if len(parts) != 4:
        return None
    _, ctrl, gidx_s, huid_s = parts
    if ctrl not in ("player_1", "player_2") or not gidx_s.isdigit() or not huid_s.lstrip("-").isdigit():
        return None
    return ctrl, int(gidx_s), int(huid_s)


def _self_unit(ctx: EffectContext) -> tuple[str, int] | None:
    """Resolve the 'self' unit for an effect-text ability: the equipped unit
    (via the equip token's host uid) or, if the source is a plain unit ref, that
    unit. ``None`` if it can't be located (e.g. it left play)."""
    parsed = _parse_equip_source(ctx.source)
    if parsed is not None:
        return ctx.engine._unit_by_uid(parsed[2])
    ref = ctx.source
    if ref and ":" in ref and not ref.startswith("equip:"):
        _, idx, unit = _resolve_unit(ctx.engine, ref)
        if unit is not None:
            return (ref.split(":", 1)[0], idx)
    return None


@register_effect("UNATTACH_THIS")
def _unattach_this(ctx: EffectContext) -> None:
    """Detach the equipment that owns this ability from its unit (Blighted
    Battleaxe's end-of-turn cleanup)."""
    parsed = _parse_equip_source(ctx.source)
    if parsed is None:
        return
    ctrl, gidx, _huid = parsed
    gs = ctx.engine._game_state
    gears = gs.player_1_gears if ctrl == "player_1" else gs.player_2_gears
    if 0 <= gidx < len(gears):
        gears[gidx].attached_uid = None
        gears[gidx].attached_to = None
        gears[gidx].attached_on_turn = None


@register_effect_pattern(r"DEAL_\d+_SELF")
def _deal_n_self(ctx: EffectContext) -> None:
    """Deal N damage to the ability's SELF unit (the equipped unit for an
    equipment). Marks N damage (rule 417); the unit dies at the cleanup lethal
    sweep if its total marked damage reaches its Might (143.2.a/323). Resolves
    the host by stable uid, so it's order-independent with UNATTACH_THIS."""
    m = re.fullmatch(r"DEAL_(\d+)_SELF", ctx.code)
    if m is None:
        return
    amount = int(m.group(1))
    loc = _self_unit(ctx)
    if loc is None:
        return
    ctrl, idx = loc
    ctx.engine.deal_damage(f"{ctrl}:{idx}", amount)


def _deal_to_targets(ctx: EffectContext, amount: int) -> None:
    """Deal ``amount`` to EACH chosen target unit (rule 417). Bonus Damage and
    the lethal check are handled by the engine's deal_damage + cleanup sweep."""
    for ref in ctx.targets:
        ctx.engine.deal_damage(ref, amount)


@register_effect("DEAL_3_DAMAGE", targeted=True)
def _deal_3_damage(ctx: EffectContext) -> None:
    """Wages of Pain: "Deal 3 to a unit at a battlefield." (The requirement
    picks one battlefield unit; the separate Gold-token effect is its own code.)"""
    _deal_to_targets(ctx, 3)


@register_effect("DEAL_6_TO_2_UNITS", targeted=True)
def _deal_6_to_2_units(ctx: EffectContext) -> None:
    """Singularity: "Deal 6 to each of up to two units." 6 is dealt to EACH
    chosen target separately (so Bonus Damage applies to each — rule 715.2)."""
    _deal_to_targets(ctx, 6)


@register_effect("DEAL_1_TO_3_UNITS_SAME_LOC", targeted=True)
def _deal_1_to_3_units_same_loc(ctx: EffectContext) -> None:
    """Bellows Breath: "Deal 1 to up to three units at the same location."
    (The SAME_LOC requirement enforces the shared location at target choice.)"""
    _deal_to_targets(ctx, 1)
