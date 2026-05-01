from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class GameState:
    counter: int = 0


class RequiredTo(str, Enum):
    PLAYER_1 = "player_1"
    PLAYER_2 = "player_2"
    BOTH = "both"


@dataclass
class EngineOutput:
    game_state: GameState
    player_1_options: list[str] = field(default_factory=list)
    player_2_options: list[str] = field(default_factory=list)


class GameEngine:
    def __init__(self, game_state: GameState | None = None, max_counter: int | None = None) -> None:
        self._game_state = game_state or GameState()
        self._max_counter = max_counter

    @property
    def game_state(self) -> GameState:
        return GameState(counter=self._game_state.counter)

    def _can_increment(self) -> bool:
        return self._max_counter is None or self._game_state.counter < self._max_counter

    def start(self, required_to: RequiredTo = RequiredTo.BOTH) -> EngineOutput:
        if self._can_increment():
            self._game_state.counter += 1
            return EngineOutput(game_state=self.game_state)

        required_option = "resolve_counter_block"
        player_1_options: list[str] = []
        player_2_options: list[str] = []

        if required_to in (RequiredTo.PLAYER_1, RequiredTo.BOTH):
            player_1_options.append(required_option)
        if required_to in (RequiredTo.PLAYER_2, RequiredTo.BOTH):
            player_2_options.append(required_option)

        return EngineOutput(
            game_state=self.game_state,
            player_1_options=player_1_options,
            player_2_options=player_2_options,
        )
