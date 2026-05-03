"""Wire format: step names the engine requires and command tokens for apply_action.

Values are stable API — tests and any client should use these names, not ad hoc strings.
"""

from __future__ import annotations

from enum import StrEnum


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
    ABCD = "abcd"
    PLAY = "play"


def apply_prefix(verb: ApplyVerb) -> str:
    return f"{verb.value}:"
