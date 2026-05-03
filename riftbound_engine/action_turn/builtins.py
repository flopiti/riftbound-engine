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


# Example: register more handlers in this file or import sibling modules:
#
# @register_turn_action("play_card")
# def _play_card(ctx: ActionTurnContext) -> None:
#     card_id = ctx.payload.strip()
#     ...
