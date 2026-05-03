"""Per-invocation context for a single `play:<verb>:…` action during the action turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..engine import GameEngine, RequiredTo


@dataclass(frozen=True)
class ActionTurnContext:
    """Passed to every registered turn-action handler."""

    engine: GameEngine
    actor: RequiredTo
    verb: str
    #: Everything after `play:<verb>:` (may be empty).
    payload: str
