from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

from riftbound_engine import GameEngine, HARDCODED_DECKS, RequiredTo, Rune
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
    hand: list[str] | None,
    runes: list[Rune],
    deck,
) -> list[str]:
    hand_s = ", ".join(hand) if hand is not None else "pending"
    champion_s = deck.chosen_champion if deck is not None else "pending"
    legend_s = deck.legend if deck is not None else "pending"
    return [
        f"{title}:",
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
                    hand=state.player_1_hand,
                    runes=state.player_1_runes,
                    deck=state.player_1_deck,
                ),
                *_player_lines(
                    "Player 2",
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
            f"First turn choice: {state.first_turn_choice.value if state.first_turn_choice else 'pending'}",
            f"First turn: {state.first_turn.value if state.first_turn else 'pending'}",
            f"Battlefield 1: {state.battlefield_1 or 'pending'}",
            f"Battlefield 2: {state.battlefield_2 or 'pending'}",
            f"Mulligan done: {state.is_mulligan_done}",
            "",
            *_player_lines(
                "Player 1",
                hand=state.player_1_hand,
                runes=state.player_1_runes,
                deck=state.player_1_deck,
            ),
            *_player_lines(
                "Player 2",
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


def main() -> None:
    _load_env_file()
    fake_fill_enabled = os.getenv("FAKE_FILL", "").strip().lower() in {"1", "true", "yes", "on"}
    engine = GameEngine()
    output = engine.start()

    while True:
        print(_format_game_state(engine))
        if output.required_action is None:
            print("Required action: null")
        else:
            required_action_view = {
                "player": output.required_action.actor.value,
                "name": output.required_action.name,
            }
            print(f"Required action: {required_action_view}")
        print("")

        if output.required_action is not None and output.required_action.name == "choose_deck":
            options = (
                output.player_1_options if output.required_action.actor == RequiredTo.PLAYER_1 else output.player_2_options
            )
            print(f"Available decks: {', '.join(options)}")
            fake_choice = FAKE_FILL_CHOICES.get(output.required_action.actor) if fake_fill_enabled else None
            if fake_choice in options:
                deck_choice = fake_choice
                print(f"Auto-filled deck id: {deck_choice}")
            else:
                deck_choice = input("Choose deck id: ").strip().lower()
            if deck_choice == "quit":
                print("Exiting engine.")
                return
            user_input = f"choose_deck:{deck_choice}"
            try:
                actor = output.required_action.actor
                output = engine.apply_action(action=user_input, actor=actor)
            except ValueError as error:
                print(f"Invalid action: {error}")
            continue

        if output.required_action is not None and output.required_action.name == "choose_first_turn":
            fake_first_turn = FAKE_FILL_FIRST_TURN.value if fake_fill_enabled else None
            if fake_first_turn in (RequiredTo.PLAYER_1.value, RequiredTo.PLAYER_2.value):
                first_turn = fake_first_turn
                print(f"Auto-filled first_turn: {first_turn}")
            else:
                first_turn = input("Choose first turn ('player_1' or 'player_2'): ").strip().lower()
            if first_turn == "quit":
                print("Exiting engine.")
                return
            try:
                output = engine.apply_action(action=f"choose_first_turn:{first_turn}", actor=output.required_action.actor)
            except ValueError as error:
                print(f"Invalid action: {error}")
            continue

        state = engine.game_state
        both_decks_selected = state.player_1_deck is not None and state.player_2_deck is not None
        first_turn_selected = state.first_turn is not None
        battlefields_pending = both_decks_selected and first_turn_selected and (
            state.battlefield_1 is None or state.battlefield_2 is None
        )

        if battlefields_pending:
            if state.battlefield_1 is None:
                options = list(state.player_1_deck.battlefields)
                print(f"Player 1 battlefield options: {', '.join(options)}")
                fake_battlefield_1 = FAKE_FILL_BATTLEFIELDS.get(RequiredTo.PLAYER_1) if fake_fill_enabled else None
                if fake_battlefield_1 in options:
                    battlefield_1 = fake_battlefield_1
                    print(f"Auto-filled battlefield_1: {battlefield_1}")
                else:
                    battlefield_1 = input("Choose battlefield_1: ").strip()
                if battlefield_1 == "quit":
                    print("Exiting engine.")
                    return
                try:
                    output = engine.apply_action(action=f"choose_battlefield_1:{battlefield_1}", actor=RequiredTo.PLAYER_1)
                except ValueError as error:
                    print(f"Invalid action: {error}")
                    continue

            state = engine.game_state
            if state.battlefield_2 is None:
                options = list(state.player_2_deck.battlefields)
                print(f"Player 2 battlefield options: {', '.join(options)}")
                fake_battlefield_2 = FAKE_FILL_BATTLEFIELDS.get(RequiredTo.PLAYER_2) if fake_fill_enabled else None
                if fake_battlefield_2 in options:
                    battlefield_2 = fake_battlefield_2
                    print(f"Auto-filled battlefield_2: {battlefield_2}")
                else:
                    battlefield_2 = input("Choose battlefield_2: ").strip()
                if battlefield_2 == "quit":
                    print("Exiting engine.")
                    return
                try:
                    output = engine.apply_action(action=f"choose_battlefield_2:{battlefield_2}", actor=RequiredTo.PLAYER_2)
                except ValueError as error:
                    print(f"Invalid action: {error}")
                    continue

            continue

        if output.required_action is not None and output.required_action.name == "choose_mulligan":
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
                    return
                try:
                    output = engine.apply_action(
                        action=f"mulligan_resolve:{RequiredTo.PLAYER_1.value}:{bottom_indices}",
                        actor=RequiredTo.PLAYER_1,
                    )
                except ValueError as error:
                    print(f"Invalid action: {error}")
                    continue
                continue

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
                    return
                try:
                    output = engine.apply_action(
                        action=f"mulligan_resolve:{RequiredTo.PLAYER_2.value}:{bottom_indices}",
                        actor=RequiredTo.PLAYER_2,
                    )
                except ValueError as error:
                    print(f"Invalid action: {error}")
                    continue
                continue

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

        if state.first_turn_choice is not None:
            print(f"{state.first_turn_choice.value} has first turn choice.")
        print("Game setup complete. No gameplay actions are implemented yet.")
        return


if __name__ == "__main__":
    main()
