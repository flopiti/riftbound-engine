"""HTTP adapter for `GameEngine` — poll state and POST actions (FastAPI)."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .engine import Deck, EngineOutput, GameEngine, GameState, RequiredTo
from .fake_fill import (
    FAKE_FILL_BATTLEFIELDS,
    FAKE_FILL_CHOICES,
    FAKE_FILL_FIRST_TURN,
    FAKE_FILL_MULLIGAN_BOTTOM,
)
from .protocol import ApplyVerb, RequiredStep

_engine_lock = threading.Lock()
_engine: GameEngine = GameEngine()
_last_output: EngineOutput | None = None


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _fake_fill_enabled() -> bool:
    return os.getenv("FAKE_FILL", "").strip().lower() in {"1", "true", "yes", "on"}


def _auto_fake_fill(engine: GameEngine, output: EngineOutput) -> EngineOutput:
    if not _fake_fill_enabled():
        return output

    for _ in range(64):
        ra = output.required_action
        if ra is None:
            break

        step = ra.name
        actor = ra.actor

        if step == RequiredStep.CHOOSE_DECK:
            deck_id = FAKE_FILL_CHOICES.get(actor)
            if deck_id is None:
                break
            output = engine.apply_action(action=f"{ApplyVerb.CHOOSE_DECK.value}:{deck_id}", actor=actor)
            continue

        if step == RequiredStep.CHOOSE_FIRST_TURN:
            output = engine.apply_action(
                action=f"{ApplyVerb.CHOOSE_FIRST_TURN.value}:{FAKE_FILL_FIRST_TURN.value}",
                actor=actor,
            )
            continue

        if step == RequiredStep.CHOOSE_BATTLEFIELDS:
            gs = output.game_state
            if gs.battlefield_1 is None:
                bf = FAKE_FILL_BATTLEFIELDS[RequiredTo.PLAYER_1]
                output = engine.apply_action(
                    action=f"{ApplyVerb.CHOOSE_BATTLEFIELD_1.value}:{bf}",
                    actor=RequiredTo.PLAYER_1,
                )
                continue
            if gs.battlefield_2 is None:
                bf = FAKE_FILL_BATTLEFIELDS[RequiredTo.PLAYER_2]
                output = engine.apply_action(
                    action=f"{ApplyVerb.CHOOSE_BATTLEFIELD_2.value}:{bf}",
                    actor=RequiredTo.PLAYER_2,
                )
                continue
            break

        if step == RequiredStep.CHOOSE_MULLIGAN:
            gs = output.game_state
            bottom = FAKE_FILL_MULLIGAN_BOTTOM.strip()
            if not gs.mulligan_player_1_resolved:
                output = engine.apply_action(
                    action=f"{ApplyVerb.MULLIGAN_RESOLVE.value}:{RequiredTo.PLAYER_1.value}:{bottom}",
                    actor=RequiredTo.PLAYER_1,
                )
                continue
            if not gs.mulligan_player_2_resolved:
                output = engine.apply_action(
                    action=f"{ApplyVerb.MULLIGAN_RESOLVE.value}:{RequiredTo.PLAYER_2.value}:{bottom}",
                    actor=RequiredTo.PLAYER_2,
                )
                continue
            break

        break

    return output


def reset_engine() -> EngineOutput:
    global _engine, _last_output
    _engine = GameEngine()
    _last_output = _engine.start()
    _last_output = _auto_fake_fill(_engine, _last_output)
    return _last_output


def _deck_to_json(deck: Deck | None) -> dict[str, Any] | None:
    if deck is None:
        return None
    return {
        "battlefields": list(deck.battlefields),
        "chosen_champion": deck.chosen_champion,
        "legend": deck.legend,
        "cards": list(deck.cards),
        "runes": [{"domain": r.domain} for r in deck.runes],
    }


def _serialize_state(gs: GameState) -> dict[str, Any]:
    return {
        "counter": gs.counter,
        "started": gs.started,
        "total_turn_number": gs.total_turn_number,
        "player_1_turn_number": gs.player_1_turn_number,
        "player_2_turn_number": gs.player_2_turn_number,
        "current_player": gs.current_player.value,
        "first_turn_choice": gs.first_turn_choice.value if gs.first_turn_choice else None,
        "first_turn": gs.first_turn.value if gs.first_turn else None,
        "battlefield_1": gs.battlefield_1,
        "battlefield_2": gs.battlefield_2,
        "is_mulligan_done": gs.is_mulligan_done,
        "mulligan_player_1_resolved": gs.mulligan_player_1_resolved,
        "mulligan_player_2_resolved": gs.mulligan_player_2_resolved,
        "player_1_deck_id": gs.player_1_deck_id,
        "player_2_deck_id": gs.player_2_deck_id,
        "player_1_hand": list(gs.player_1_hand) if gs.player_1_hand is not None else None,
        "player_2_hand": list(gs.player_2_hand) if gs.player_2_hand is not None else None,
        "player_1_mulligan_hand": list(gs.player_1_mulligan_hand) if gs.player_1_mulligan_hand else None,
        "player_2_mulligan_hand": list(gs.player_2_mulligan_hand) if gs.player_2_mulligan_hand else None,
        "player_1_library_len": len(gs.player_1_library) if gs.player_1_library else None,
        "player_2_library_len": len(gs.player_2_library) if gs.player_2_library else None,
        "player_1_runes": [{"domain": r.domain} for r in gs.player_1_runes],
        "player_2_runes": [{"domain": r.domain} for r in gs.player_2_runes],
        "player_1_rune_library_len": len(gs.player_1_rune_library) if gs.player_1_rune_library else None,
        "player_2_rune_library_len": len(gs.player_2_rune_library) if gs.player_2_rune_library else None,
        "player_1_base": gs.player_1_base,
        "player_2_base": gs.player_2_base,
        "player_1_deck": _deck_to_json(gs.player_1_deck),
        "player_2_deck": _deck_to_json(gs.player_2_deck),
        "abcd_a_done": gs.abcd_a_done,
        "abcd_b_done": gs.abcd_b_done,
        "abcd_c_done": gs.abcd_c_done,
        "abcd_d_done": gs.abcd_d_done,
        "global_channel_count": gs.global_channel_count,
    }


def _serialize_output(out: EngineOutput) -> dict[str, Any]:
    ra = out.required_action
    return {
        "state": _serialize_state(out.game_state),
        "player_1_options": list(out.player_1_options),
        "player_2_options": list(out.player_2_options),
        "required_action": (
            None
            if ra is None
            else {"actor": ra.actor.value, "name": ra.name}
        ),
    }


def get_snapshot() -> dict[str, Any]:
    """Always run `start()` so ABCD auto-completion and any logic upgrades apply to every poll."""
    with _engine_lock:
        global _last_output
        _last_output = _engine.start()
        _last_output = _auto_fake_fill(_engine, _last_output)
        return _serialize_output(_last_output)


class ActionBody(BaseModel):
    actor: str = Field(..., description="player_1, player_2, or both")
    action: str = Field(..., min_length=1)


def create_app() -> FastAPI:
    app = FastAPI(title="Riftbound Engine API", version="0.1.0")

    origins = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def _startup() -> None:
        _load_dotenv()
        from .decks import deck_data_for_id

        sample = deck_data_for_id("ember_vanguard")["cards"][0]
        print(f"[riftbound-engine] CSV decks loaded (e.g. ember_vanguard → {sample!r})")
        reset_engine()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/state")
    def state() -> dict[str, Any]:
        return get_snapshot()

    @app.post("/reset")
    def reset() -> dict[str, Any]:
        with _engine_lock:
            global _last_output
            _last_output = reset_engine()
            return _serialize_output(_last_output)

    @app.post("/action")
    def action(body: ActionBody) -> dict[str, Any]:
        try:
            actor = RequiredTo(body.actor)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"invalid actor: {body.actor}") from e

        with _engine_lock:
            global _last_output
            if _last_output is None:
                _last_output = _engine.start()
            try:
                _last_output = _engine.apply_action(action=body.action, actor=actor)
                _last_output = _auto_fake_fill(_engine, _last_output)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            return _serialize_output(_last_output)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("ENGINE_API_HOST", "127.0.0.1")
    port = int(os.getenv("ENGINE_API_PORT", "8790"))
    uvicorn.run("riftbound_engine.http_api:app", host=host, port=port, reload=False)
