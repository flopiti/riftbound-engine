# riftbound-engine

A minimal game engine prototype.

## What exists now

- `GameState` model with an integer `counter` that defaults to `0`.
- `GameEngine` with a `start()` method:
  - If increment is possible, it increments `counter` and returns the new state.
  - If increment is not possible, it returns required options for Player 1, Player 2, or both.
- `EngineOutput` always includes:
  - `game_state`
  - `player_1_options`
  - `player_2_options`

## Quick example

```python
from riftbound_engine import GameEngine, RequiredTo

engine = GameEngine()
result = engine.start()
print(result.game_state.counter)  # 1

blocked_engine = GameEngine(max_counter=0)
blocked = blocked_engine.start(required_to=RequiredTo.BOTH)
print(blocked.game_state.counter)    # 0
print(blocked.player_1_options)      # ["resolve_counter_block"]
print(blocked.player_2_options)      # ["resolve_counter_block"]
```

## Run tests

```bash
python -m pytest
```
