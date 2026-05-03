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
    if actor != gs.current_player:
        raise ValueError("only the active player may take action-turn plays")
    if actor not in (RT.PLAYER_1, RT.PLAYER_2):
        raise ValueError("action turn requires player_1 or player_2")
    handler = _REGISTRY.get(verb)
    if handler is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(f"unknown play verb {verb!r}; known: {known}")

    handler(ActionTurnContext(engine=engine, actor=actor, verb=verb, payload=payload))


from . import builtins as _builtins  # noqa: F401 — register built-in verbs (e.g. end_turn)
