"""Register and dispatch discretionary `play:<verb>:` actions during the action-turn phase.

Add a new action:
1. Implement a function `(ctx: ActionTurnContext) -> None` that mutates `ctx.engine` state.
2. Decorate it with `@register_turn_action("your_verb")`.
3. Import the module from `builtins.py` (or another module imported by `builtins`) so registration runs.

Wire format: `play:<verb>` or `play:<verb>:<payload>` — payload can contain extra `:` characters.
"""

from __future__ import annotations

from collections.abc import Callable

from .context import ActionTurnContext

Handler = Callable[[ActionTurnContext], None]

_REGISTRY: dict[str, Handler] = {}


def register_turn_action(verb: str) -> Callable[[Handler], Handler]:
    """Declare a handler for `play:{verb}:…` during the action turn."""

    def decorator(fn: Handler) -> Handler:
        if verb in _REGISTRY:
            raise ValueError(f"turn action {verb!r} is already registered")
        _REGISTRY[verb] = fn
        return fn

    return decorator


def registered_turn_action_verbs() -> frozenset[str]:
    return frozenset(_REGISTRY.keys())


def parse_play_command(action: str) -> tuple[str, str]:
    """Split `play:<verb>` or `play:<verb>:<payload>`."""
    if not action.startswith("play:"):
        raise ValueError("action must start with play:")
    rest = action.removeprefix("play:")
    if not rest:
        raise ValueError("missing play verb after play:")
    idx = rest.find(":")
    if idx == -1:
        return rest, ""
    return rest[:idx], rest[idx + 1 :]


#: Verbs the OPPONENT (non-active player) is allowed to send. Most action-turn
#: plays are gated to ``current_player``, but a few are interactive responses
#: that BOTH players take part in and must accept the opponent as actor:
#:   * ``pass_showdown`` — initiator then opponent each pass.
#:   * ``assign_damage`` — a contested combat needs BOTH sides to assign their
#:     combat damage (the defender is the non-active player), otherwise the
#:     combat can never resolve.
#: Handlers in this set are responsible for their own actor validation.
_OPPONENT_OK_VERBS: frozenset[str] = frozenset({"pass_showdown", "assign_damage"})


def dispatch_turn_play(engine, actor, action: str) -> None:
    """Validate phase/actor and run the handler for `play:*`."""
    from ..engine import RequiredTo as RT
    from ..engine import is_abcd_done

    gs = engine.game_state
    if not gs.started:
        raise ValueError("game has not started")
    if not is_abcd_done(gs):
        raise ValueError("ABCD must be finished before the action turn")
    verb, payload = parse_play_command(action)
    # While the chain is open, the player HOLDING PRIORITY may act even if
    # they're not the active player (e.g. the opponent responding with a
    # Reaction, paying for it, or passing). The individual handlers do their
    # own priority/Reaction validation, so it's safe to let the verb through
    # here. Outside a chain, only the active player (plus the always-allowed
    # opponent verbs like pass_showdown) may play.
    chain = getattr(gs, "pending_chain", None)
    holder_has_priority = chain is not None and actor == chain.priority
    # During a showdown the FOCUS holder (which may be the non-active
    # defender) may act — play an [Action]/[Reaction] spell or pass focus.
    # Only when no chain is open; once a chain is up, the chain's own
    # priority governs (handled by holder_has_priority above).
    showdown = getattr(gs, "pending_showdown", None)
    holder_has_focus = (
        showdown is not None
        and chain is None
        and actor == showdown.focus_holder
    )
    # A pending EFFECT CHOICE belongs to the resolved ability's controller,
    # who may be the non-active player (e.g. P2 cast a Reaction on P1's turn
    # and must now pick a unit for Abandoned Hall's trigger). Let the chooser
    # through; the handler does its own actor validation.
    effect_choice = getattr(gs, "pending_effect_choice", None)
    holder_has_effect_choice = (
        effect_choice is not None and actor == effect_choice.actor
    )
    if (
        verb not in _OPPONENT_OK_VERBS
        and actor != gs.current_player
        and not holder_has_priority
        and not holder_has_focus
        and not holder_has_effect_choice
    ):
        raise ValueError("only the active player may take action-turn plays")
    if actor not in (RT.PLAYER_1, RT.PLAYER_2):
        raise ValueError("action turn requires player_1 or player_2")
    handler = _REGISTRY.get(verb)
    if handler is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(f"unknown play verb {verb!r}; known: {known}")

    handler(ActionTurnContext(engine=engine, actor=actor, verb=verb, payload=payload))


from . import builtins as _builtins  # noqa: F401 — register built-in verbs (e.g. end_turn)
