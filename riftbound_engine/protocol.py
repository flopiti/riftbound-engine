"""Wire format: step names the engine requires and command tokens for apply_action.

Values are stable API — tests and any client should use these names, not ad hoc strings.
"""

from __future__ import annotations

try:  # Python 3.11+
    from enum import StrEnum
except ImportError:  # Python 3.10 and earlier — minimal stand-in
    from enum import Enum

    class StrEnum(str, Enum):
        """Compat shim: members are real ``str``s and ``str(member)`` is the
        value (matching 3.11's StrEnum), so the engine imports/runs on 3.10."""

        def __str__(self) -> str:
            return str(self.value)


class RequiredStep(StrEnum):
    """Value of `RequiredAction.name`: what the UI should collect before the next apply_action."""

    CHOOSE_DECK = "choose_deck"
    CHOOSE_FIRST_TURN = "choose_first_turn"
    CHOOSE_BATTLEFIELDS = "choose_battlefields"
    CHOOSE_MULLIGAN = "choose_mulligan"
    ABCD = "abcd"
    #: Discretionary plays after ABCD (`play:<verb>:`); use `play:end_turn` when done.
    ACTION_TURN = "action_turn"


class ApplyVerb(StrEnum):
    """Leading token in `GameEngine.apply_action` strings (before the first `:`)."""

    CHOOSE_DECK = "choose_deck"
    CHOOSE_FIRST_TURN = "choose_first_turn"
    CHOOSE_BATTLEFIELD_1 = "choose_battlefield_1"
    CHOOSE_BATTLEFIELD_2 = "choose_battlefield_2"
    MULLIGAN_RESOLVE = "mulligan_resolve"
    #: One-card-at-a-time mulligan: bottom a single card by index
    #: (`mulligan_bottom:player_N:<i>`); MULLIGAN_DONE is the "No mulligan" /
    #: stop option that finalizes. MULLIGAN_RESOLVE is kept for the legacy
    #: one-shot path (fake-fill auto-setup, CLI, older clients).
    MULLIGAN_BOTTOM = "mulligan_bottom"
    MULLIGAN_DONE = "mulligan_done"
    ABCD = "abcd"
    PLAY = "play"


def apply_prefix(verb: ApplyVerb) -> str:
    return f"{verb.value}:"
