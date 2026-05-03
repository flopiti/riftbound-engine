from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Callable, Literal

from riftbound_engine import (
    ApplyVerb,
    EngineOutput,
    GameEngine,
    HARDCODED_DECKS,
    RequiredStep,
    RequiredTo,
    Rune,
    registered_turn_action_verbs,
)
from riftbound_engine.fake_fill import (
    FAKE_FILL_BATTLEFIELDS,
    FAKE_FILL_CHOICES,
    FAKE_FILL_FIRST_TURN,
    FAKE_FILL_MULLIGAN_BOTTOM,
)


def _rune_summary(deck) -> str:
    counts = Counter(rune.domain for rune in deck.runes)
    return ", ".join(f"{count} {domain}" for domain, count in sorted(counts.items()))


def _format_player_runes(runes: list[Rune]) -> str:
    if not runes:
        return "(empty)"
    return ", ".join(r.domain for r in runes)


def _player_lines(
    title: str,
    *,
    base: str | None,
    hand: list[str] | None,
    runes: list[Rune],
    deck,
) -> list[str]:
    base_s = base if base is not None else "pending"
    hand_s = ", ".join(hand) if hand is not None else "pending"
    champion_s = deck.chosen_champion if deck is not None else "pending"
    legend_s = deck.legend if deck is not None else "pending"
    return [
        f"{title}:",
        f"  base: {base_s}",
        f"  hand: {hand_s}",
        f"  runes: {_format_player_runes(runes)}",
        f"  chosen champion: {champion_s}",
        f"  legend: {legend_s}",
        "",
    ]


def _load_env_file() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _format_game_state(engine: GameEngine) -> str:
    state = engine.game_state
    player_1_deck_name = state.player_1_deck_id or "not selected"
    player_2_deck_name = state.player_2_deck_id or "not selected"
    player_1_valid = (
        bool(HARDCODED_DECKS[state.player_1_deck_id].get("valid", False)) if state.player_1_deck_id else False
    )
    player_2_valid = (
        bool(HARDCODED_DECKS[state.player_2_deck_id].get("valid", False)) if state.player_2_deck_id else False
    )

    if not state.started:
        return "\n".join(
            [
                "",
                "=== Riftbound Engine ===",
                "",
                f"Game started: {state.started}",
                f"First turn choice: {state.first_turn_choice.value if state.first_turn_choice else 'pending'}",
                f"First turn: {state.first_turn.value if state.first_turn else 'pending'}",
                f"Battlefield 1: {state.battlefield_1 or 'pending'}",
                f"Battlefield 2: {state.battlefield_2 or 'pending'}",
                f"Mulligan done: {state.is_mulligan_done}",
                "",
                *_player_lines(
                    "Player 1",
                    base=state.player_1_base,
                    hand=state.player_1_hand,
                    runes=state.player_1_runes,
                    deck=state.player_1_deck,
                ),
                *_player_lines(
                    "Player 2",
                    base=state.player_2_base,
                    hand=state.player_2_hand,
                    runes=state.player_2_runes,
                    deck=state.player_2_deck,
                ),
                "Player 1 deck:",
                f"  Deck id: {player_1_deck_name}",
                f"  Valid: {player_1_valid}",
                "",
                "Player 2 deck:",
                f"  Deck id: {player_2_deck_name}",
                f"  Valid: {player_2_valid}",
                "",
            ]
        )

    return "\n".join(
        [
            "",
            "=== Riftbound Engine ===",
            "",
            f"Counter: {state.counter}",
            f"Game started: {state.started}",
            f"Total turn #: {state.total_turn_number}",
            f"Active player: {state.current_player.value}",
            f"Player 1 — turn count: {state.player_1_turn_number}",
            f"Player 2 — turn count: {state.player_2_turn_number}",
            f"First turn choice: {state.first_turn_choice.value if state.first_turn_choice else 'pending'}",
            f"First turn (goes first): {state.first_turn.value if state.first_turn else 'pending'}",
            f"Battlefield 1: {state.battlefield_1 or 'pending'}",
            f"Battlefield 2: {state.battlefield_2 or 'pending'}",
            f"Mulligan done: {state.is_mulligan_done}",
            f"ABCD this turn: A={state.abcd_a_done} B={state.abcd_b_done} C={state.abcd_c_done} D={state.abcd_d_done}",
            "",
            *_player_lines(
                "Player 1",
                base=state.player_1_base,
                hand=state.player_1_hand,
                runes=state.player_1_runes,
                deck=state.player_1_deck,
            ),
            *_player_lines(
                "Player 2",
                base=state.player_2_base,
                hand=state.player_2_hand,
                runes=state.player_2_runes,
                deck=state.player_2_deck,
            ),
            "Player 1 deck:",
            f"  Deck id: {player_1_deck_name}",
            f"  Valid: {player_1_valid}",
            f"  Battlefields (3): {', '.join(state.player_1_deck.battlefields)}",
            f"  Chosen champion (1): {state.player_1_deck.chosen_champion}",
            f"  Legend (1): {state.player_1_deck.legend}",
            f"  Cards ({len(state.player_1_deck.cards)})",
            f"  Runes (12): {_rune_summary(state.player_1_deck)}",
            "",
            "Player 2 deck:",
            f"  Deck id: {player_2_deck_name}",
            f"  Valid: {player_2_valid}",
            f"  Battlefields (3): {', '.join(state.player_2_deck.battlefields)}",
            f"  Chosen champion (1): {state.player_2_deck.chosen_champion}",
            f"  Legend (1): {state.player_2_deck.legend}",
            f"  Cards ({len(state.player_2_deck.cards)})",
            f"  Runes (12): {_rune_summary(state.player_2_deck)}",
            "",
        ]
    )


CliStepResult = EngineOutput | None


def _cli_choose_deck(engine: GameEngine, output: EngineOutput, fake_fill_enabled: bool) -> CliStepResult:
    ra = output.required_action
    assert ra is not None
    options = output.player_1_options if ra.actor == RequiredTo.PLAYER_1 else output.player_2_options
    print(f"Available decks: {', '.join(options)}")
    fake_choice = FAKE_FILL_CHOICES.get(ra.actor) if fake_fill_enabled else None
    if fake_choice in options:
        deck_choice = fake_choice
        print(f"Auto-filled deck id: {deck_choice}")
    else:
        deck_choice = input("Choose deck id: ").strip().lower()

    user_input = f"{ApplyVerb.CHOOSE_DECK.value}:{deck_choice}"
    try:
        return engine.apply_action(action=user_input, actor=ra.actor)
    except ValueError as error:
        print(f"Invalid action: {error}")
        return output


def _cli_choose_first_turn(engine: GameEngine, output: EngineOutput, fake_fill_enabled: bool) -> CliStepResult:
    ra = output.required_action
    assert ra is not None
    fake_first_turn = FAKE_FILL_FIRST_TURN.value if fake_fill_enabled else None
    if fake_first_turn in (RequiredTo.PLAYER_1.value, RequiredTo.PLAYER_2.value):
        first_turn = fake_first_turn
        print(f"Auto-filled first_turn: {first_turn}")
    else:
        first_turn = input("Choose first turn ('player_1' or 'player_2'): ").strip().lower()
    if first_turn == "quit":
        print("Exiting engine.")
        return None
    try:
        return engine.apply_action(
            action=f"{ApplyVerb.CHOOSE_FIRST_TURN.value}:{first_turn}",
            actor=ra.actor,
        )
    except ValueError as error:
        print(f"Invalid action: {error}")
        return output


def _cli_choose_battlefields(engine: GameEngine, output: EngineOutput, fake_fill_enabled: bool) -> CliStepResult:
    if output.player_1_options:
        options = list(output.player_1_options)
        print(f"Player 1 battlefield options: {', '.join(options)}")
        fake_battlefield_1 = FAKE_FILL_BATTLEFIELDS.get(RequiredTo.PLAYER_1) if fake_fill_enabled else None
        if fake_battlefield_1 in options:
            battlefield_1 = fake_battlefield_1
            print(f"Auto-filled battlefield_1: {battlefield_1}")
        else:
            battlefield_1 = input("Choose battlefield_1: ").strip()
        if battlefield_1 == "quit":
            print("Exiting engine.")
            return None
        try:
            return engine.apply_action(
                action=f"{ApplyVerb.CHOOSE_BATTLEFIELD_1.value}:{battlefield_1}",
                actor=RequiredTo.PLAYER_1,
            )
        except ValueError as error:
            print(f"Invalid action: {error}")
            return output

    if output.player_2_options:
        options = list(output.player_2_options)
        print(f"Player 2 battlefield options: {', '.join(options)}")
        fake_battlefield_2 = FAKE_FILL_BATTLEFIELDS.get(RequiredTo.PLAYER_2) if fake_fill_enabled else None
        if fake_battlefield_2 in options:
            battlefield_2 = fake_battlefield_2
            print(f"Auto-filled battlefield_2: {battlefield_2}")
        else:
            battlefield_2 = input("Choose battlefield_2: ").strip()
        if battlefield_2 == "quit":
            print("Exiting engine.")
            return None
        try:
            return engine.apply_action(
                action=f"{ApplyVerb.CHOOSE_BATTLEFIELD_2.value}:{battlefield_2}",
                actor=RequiredTo.PLAYER_2,
            )
        except ValueError as error:
            print(f"Invalid action: {error}")
            return output

    return output


def _cli_choose_mulligan(engine: GameEngine, output: EngineOutput, fake_fill_enabled: bool) -> CliStepResult:
    state = engine.game_state
    if not state.mulligan_player_1_resolved:
        hand = state.player_1_mulligan_hand or output.player_1_options
        print(f"Player 1 mulligan hand (4): {', '.join(hand)}")
        if fake_fill_enabled:
            bottom_indices = FAKE_FILL_MULLIGAN_BOTTOM.strip()
            print(f"Auto-filled mulligan bottom indices: {bottom_indices!r}")
        else:
            bottom_indices = input(
                "Player 1 — indices (0–3) of cards to put on bottom, max 2, comma-separated (empty=none): "
            ).strip()
        if bottom_indices == "quit":
            print("Exiting engine.")
            return None
        try:
            return engine.apply_action(
                action=f"{ApplyVerb.MULLIGAN_RESOLVE.value}:{RequiredTo.PLAYER_1.value}:{bottom_indices}",
                actor=RequiredTo.PLAYER_1,
            )
        except ValueError as error:
            print(f"Invalid action: {error}")
            return output

    state = engine.game_state
    if not state.mulligan_player_2_resolved:
        hand = state.player_2_mulligan_hand or output.player_2_options
        print(f"Player 2 mulligan hand (4): {', '.join(hand)}")
        if fake_fill_enabled:
            bottom_indices = FAKE_FILL_MULLIGAN_BOTTOM.strip()
            print(f"Auto-filled mulligan bottom indices: {bottom_indices!r}")
        else:
            bottom_indices = input(
                "Player 2 — indices (0–3) of cards to put on bottom, max 2, comma-separated (empty=none): "
            ).strip()
        if bottom_indices == "quit":
            print("Exiting engine.")
            return None
        try:
            return engine.apply_action(
                action=f"{ApplyVerb.MULLIGAN_RESOLVE.value}:{RequiredTo.PLAYER_2.value}:{bottom_indices}",
                actor=RequiredTo.PLAYER_2,
            )
        except ValueError as error:
            print(f"Invalid action: {error}")
            return output

    return output


def _normalize_cli_play_command(raw: str) -> str:
    """Engine wire format is always `play:<verb>:…`. Accept bare shortcuts for end_turn in the CLI."""
    s = raw.strip()
    if not s or s.startswith(f"{ApplyVerb.PLAY.value}:"):
        return s
    word = s.lower().split(None, 1)[0]
    if word in {"end_turn", "end", "eot"}:
        return f"{ApplyVerb.PLAY.value}:end_turn"
    return s


def _cli_action_turn(engine: GameEngine, output: EngineOutput, fake_fill_enabled: bool) -> CliStepResult:
    del fake_fill_enabled
    ra = output.required_action
    assert ra is not None
    verbs = ", ".join(sorted(registered_turn_action_verbs()))
    print(
        f"Action turn — engine uses `play:<verb>` (see verbs: {verbs}). "
        "You can type end_turn or play:end_turn.\n"
        "  end_turn — end your turn; opponent starts their ABCD."
    )
    raw = input("Command (play:end_turn, end_turn, …), or quit: ").strip()
    if raw.lower() == "quit":
        print("Exiting engine.")
        return None
    action = _normalize_cli_play_command(raw)
    if not action:
        print("Empty command — use end_turn or play:end_turn.")
        return output
    try:
        return engine.apply_action(action=action, actor=ra.actor)
    except ValueError as error:
        print(f"Invalid action: {error}")
        return output


def _cli_abcd(engine: GameEngine, output: EngineOutput, fake_fill_enabled: bool) -> CliStepResult:
    del fake_fill_enabled
    ra = output.required_action
    assert ra is not None
    actor = ra.actor
    print("Applying ABCD automatically (no decisions in these steps yet).")
    try:
        current = output
        for letter in ("a", "b", "c", "d"):
            current = engine.apply_action(action=f"{ApplyVerb.ABCD.value}:{letter}", actor=actor)
        return current
    except ValueError as error:
        print(f"Invalid action: {error}")
        return output


_CLI_STEP_HANDLERS: dict[RequiredStep, Callable[[GameEngine, EngineOutput, bool], CliStepResult]] = {
    RequiredStep.CHOOSE_DECK: _cli_choose_deck,
    RequiredStep.CHOOSE_FIRST_TURN: _cli_choose_first_turn,
    RequiredStep.CHOOSE_BATTLEFIELDS: _cli_choose_battlefields,
    RequiredStep.CHOOSE_MULLIGAN: _cli_choose_mulligan,
    RequiredStep.ABCD: _cli_abcd,
    RequiredStep.ACTION_TURN: _cli_action_turn,
}


def _dispatch_required_cli_step(
    engine: GameEngine,
    output: EngineOutput,
    fake_fill_enabled: bool,
) -> tuple[Literal["again", "exit", "tail"], EngineOutput]:
    """Resolve `required_action` via the handler table; returns where the main loop should go next."""
    ra = output.required_action
    if ra is None:
        return ("tail", output)
    raw_step = getattr(ra.name, "value", ra.name)
    try:
        step = RequiredStep(str(raw_step))
    except ValueError:
        print(f"CLI: unknown required_action.name {ra.name!r} — add a RequiredStep / handler mapping.")
        return ("tail", output)
    handler = _CLI_STEP_HANDLERS.get(step)
    if handler is None:
        print(f"CLI: no handler for step {step.value!r}.")
        return ("tail", output)
    next_output = handler(engine, output, fake_fill_enabled)
    if next_output is None:
        return ("exit", output)
    return ("again", next_output)


def _print_required_action_line(output: EngineOutput) -> None:
    if output.required_action is None:
        print("Required action: null")
        return
    print(
        "Required action:",
        {"player": output.required_action.actor.value, "name": output.required_action.name},
    )


def main() -> None:
    _load_env_file()
    fake_fill_enabled = os.getenv("FAKE_FILL", "").strip().lower() in {"1", "true", "yes", "on"}
    engine = GameEngine()
    output = engine.start()

    while True:
        print(_format_game_state(engine))
        _print_required_action_line(output)
        print("")

        branch, output = _dispatch_required_cli_step(engine, output, fake_fill_enabled)
        match branch:
            case "again":
                continue
            case "exit":
                return
            case "tail":
                pass

        state = engine.game_state
        setup_pending = (
            state.player_1_deck is None
            or state.player_2_deck is None
            or state.first_turn is None
            or state.battlefield_1 is None
            or state.battlefield_2 is None
            or not state.is_mulligan_done
        )

        if setup_pending:
            continue

        if state.started and output.required_action is None:
            print("No required engine step (action turn ended or not applicable).")
            return

        print(f"Unhandled required_action: {output.required_action!r}")
        return


if __name__ == "__main__":
    main()
