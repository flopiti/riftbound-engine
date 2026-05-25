"""HTTP adapter for `GameEngine` — poll state and POST actions (FastAPI)."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .csv_data import card_domains_of, card_energy_of, card_power_of
from .deck_files import DECKS_DIR, deck_file_path, list_deck_ids, load_deck_file
from .engine import Deck, EngineOutput, GameEngine, GameState, RequiredTo
from .fake_fill import (
    FakeFillConfig,
    battlefields_dict,
    choices_dict,
    get_config as get_fake_fill_config,
    list_decks_with_battlefields,
    update_config as update_fake_fill_config,
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
    return get_fake_fill_config().enabled


def _auto_fake_fill(engine: GameEngine, output: EngineOutput) -> EngineOutput:
    cfg = get_fake_fill_config()
    if not cfg.enabled:
        return output

    choices = choices_dict()
    battlefields = battlefields_dict()
    first_turn = cfg.first_turn
    mulligan_bottom = cfg.mulligan_bottom

    for _ in range(64):
        ra = output.required_action
        if ra is None:
            break

        step = ra.name
        actor = ra.actor

        if step == RequiredStep.CHOOSE_DECK:
            deck_id = choices.get(actor)
            if not deck_id:
                break
            output = engine.apply_action(action=f"{ApplyVerb.CHOOSE_DECK.value}:{deck_id}", actor=actor)
            continue

        if step == RequiredStep.CHOOSE_FIRST_TURN:
            output = engine.apply_action(
                action=f"{ApplyVerb.CHOOSE_FIRST_TURN.value}:{first_turn.value}",
                actor=actor,
            )
            continue

        if step == RequiredStep.CHOOSE_BATTLEFIELDS:
            gs = output.game_state
            if gs.battlefield_1 is None:
                bf = battlefields.get(RequiredTo.PLAYER_1) or ""
                if not bf:
                    break
                output = engine.apply_action(
                    action=f"{ApplyVerb.CHOOSE_BATTLEFIELD_1.value}:{bf}",
                    actor=RequiredTo.PLAYER_1,
                )
                continue
            if gs.battlefield_2 is None:
                bf = battlefields.get(RequiredTo.PLAYER_2) or ""
                if not bf:
                    break
                output = engine.apply_action(
                    action=f"{ApplyVerb.CHOOSE_BATTLEFIELD_2.value}:{bf}",
                    actor=RequiredTo.PLAYER_2,
                )
                continue
            break

        if step == RequiredStep.CHOOSE_MULLIGAN:
            gs = output.game_state
            bottom = mulligan_bottom.strip()
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


def _serialize_decks() -> list[dict[str, Any]]:
    """Summary of every deck file (used by the control dashboard)."""
    out: list[dict[str, Any]] = []
    for deck_id in list_deck_ids():
        summary: dict[str, Any] = {
            "id": deck_id,
            "legend": None,
            "champion": None,
            "main_deck_count": 0,
            "battlefields": [],
            "runes_total": 0,
            "sideboard_count": 0,
            "error": None,
        }
        try:
            parsed = load_deck_file(deck_id)
            summary["legend"] = parsed.legend
            summary["champion"] = parsed.champion
            summary["main_deck_count"] = len(parsed.main_deck)
            summary["battlefields"] = list(parsed.battlefields)
            summary["runes_total"] = sum(count for _, count in parsed.runes)
            summary["sideboard_count"] = len(parsed.sideboard)
        except Exception as exc:  # pragma: no cover — surface as warning in UI
            summary["error"] = str(exc)
        out.append(summary)
    return out


def _patch_fake_fill_after_deck_removal(deleted_id: str) -> None:
    """If a deleted deck is the active fake-fill choice for either player, swap to the
    first remaining deck (or clear). Keeps the runtime config consistent so the next
    engine reset doesn't try to load a deck that no longer exists."""
    cfg = get_fake_fill_config()
    if cfg.player_1_deck != deleted_id and cfg.player_2_deck != deleted_id:
        return
    remaining = list_deck_ids()
    fallback = remaining[0] if remaining else ""
    updates: dict[str, Any] = {}
    if cfg.player_1_deck == deleted_id:
        updates["player_1_deck"] = fallback
        # Pick the fallback deck's first battlefield (or clear if none)
        try:
            updates["player_1_battlefield"] = (
                load_deck_file(fallback).battlefields[0] if fallback else ""
            )
        except Exception:
            updates["player_1_battlefield"] = ""
    if cfg.player_2_deck == deleted_id:
        updates["player_2_deck"] = fallback
        try:
            updates["player_2_battlefield"] = (
                load_deck_file(fallback).battlefields[0] if fallback else ""
            )
        except Exception:
            updates["player_2_battlefield"] = ""
    if updates:
        update_fake_fill_config(updates)


def _serialize_fake_fill(cfg: FakeFillConfig) -> dict[str, Any]:
    return {
        "enabled": cfg.enabled,
        "config": {
            "player_1_deck": cfg.player_1_deck,
            "player_2_deck": cfg.player_2_deck,
            "player_1_battlefield": cfg.player_1_battlefield,
            "player_2_battlefield": cfg.player_2_battlefield,
            "first_turn": cfg.first_turn.value,
            "mulligan_bottom": cfg.mulligan_bottom,
        },
        "decks": list_decks_with_battlefields(),
    }


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


def _serialize_hand_costs(hand: list[str] | None) -> list[dict[str, Any]]:
    """Per-hand cost metadata: aligned 1:1 with the hand so the UI can label
    each play_unit option with the card's Energy / Power cost without doing
    its own CSV lookups."""
    if not hand:
        return []
    out: list[dict[str, Any]] = []
    for name in hand:
        energy = card_energy_of(name)
        power = card_power_of(name)
        out.append(
            {
                "energy": energy if energy is not None else 0,
                "power": power if power is not None else 0,
                "domains": list(card_domains_of(name)),
            }
        )
    return out


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
        "player_1_hand_costs": _serialize_hand_costs(gs.player_1_hand),
        "player_2_hand_costs": _serialize_hand_costs(gs.player_2_hand),
        "player_1_units": [
            {"card": u.card, "location": u.location, "exhausted": u.exhausted}
            for u in gs.player_1_units
        ],
        "player_2_units": [
            {"card": u.card, "location": u.location, "exhausted": u.exhausted}
            for u in gs.player_2_units
        ],
        "pending_play": (
            None
            if gs.pending_play is None
            else {"actor": gs.pending_play.actor.value, "card": gs.pending_play.card}
        ),
        "pending_payment": (
            None
            if gs.pending_payment is None
            else {"actor": gs.pending_payment.actor.value, "remaining": gs.pending_payment.remaining}
        ),
        "pending_showdown": (
            None
            if gs.pending_showdown is None
            else {
                "battlefield": gs.pending_showdown.battlefield,
                "initiator": gs.pending_showdown.initiator.value,
                "initiator_passed": gs.pending_showdown.initiator_passed,
                "opponent_passed": gs.pending_showdown.opponent_passed,
            }
        ),
        "battlefield_1_controller": (
            gs.battlefield_1_controller.value if gs.battlefield_1_controller else None
        ),
        "battlefield_2_controller": (
            gs.battlefield_2_controller.value if gs.battlefield_2_controller else None
        ),
        "player_1_mulligan_hand": list(gs.player_1_mulligan_hand) if gs.player_1_mulligan_hand else None,
        "player_2_mulligan_hand": list(gs.player_2_mulligan_hand) if gs.player_2_mulligan_hand else None,
        "player_1_library_len": len(gs.player_1_library) if gs.player_1_library else None,
        "player_2_library_len": len(gs.player_2_library) if gs.player_2_library else None,
        "player_1_runes": [{"domain": r.domain, "exhausted": r.exhausted} for r in gs.player_1_runes],
        "player_2_runes": [{"domain": r.domain, "exhausted": r.exhausted} for r in gs.player_2_runes],
        "player_1_energy": gs.player_1_energy,
        "player_2_energy": gs.player_2_energy,
        "player_1_power": dict(gs.player_1_power),
        "player_2_power": dict(gs.player_2_power),
        "player_1_score": gs.player_1_score,
        "player_2_score": gs.player_2_score,
        "scored_bfs_this_turn": sorted(gs.scored_bfs_this_turn),
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


class FakeFillUpdateBody(BaseModel):
    """Partial update for the fake-fill runtime config. Any omitted field is left alone."""

    enabled: bool | None = None
    player_1_deck: str | None = None
    player_2_deck: str | None = None
    player_1_battlefield: str | None = None
    player_2_battlefield: str | None = None
    first_turn: str | None = Field(default=None, description="player_1 or player_2")
    mulligan_bottom: str | None = None


class DeckSaveBody(BaseModel):
    """Save a deck file to riftbound-engine/decks/{id}.txt. Overwrites if it exists."""

    id: str = Field(..., min_length=1, max_length=80)
    text: str = Field(..., min_length=1)


_DECK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")


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

        from .deck_files import list_deck_ids

        deck_ids = list_deck_ids()
        if deck_ids:
            sample_id = deck_ids[0]
            sample = deck_data_for_id(sample_id)["cards"][0]
            print(f"[riftbound-engine] Deck files loaded ({len(deck_ids)} decks, e.g. {sample_id} → {sample!r})")
        else:
            print("[riftbound-engine] No deck files in riftbound-engine/decks/*.txt")
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

    @app.get("/decks")
    def decks_list() -> dict[str, Any]:
        return {"decks": _serialize_decks()}

    @app.post("/decks")
    def decks_create(body: DeckSaveBody) -> dict[str, Any]:
        deck_id = body.id.strip().lower()
        if not _DECK_ID_RE.fullmatch(deck_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    "deck id must be lowercase letters/digits/underscore/dash "
                    "and start with a letter or digit"
                ),
            )

        DECKS_DIR.mkdir(parents=True, exist_ok=True)
        path = DECKS_DIR / f"{deck_id}.txt"
        existed = path.exists()
        previous: str | None = path.read_text(encoding="utf-8") if existed else None
        path.write_text(body.text, encoding="utf-8")

        # Validate by loading. If parsing fails, roll back so we don't leave a
        # broken file on disk that the engine would later trip over.
        try:
            load_deck_file(deck_id)
        except Exception as exc:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(previous, encoding="utf-8")
            raise HTTPException(status_code=400, detail=f"deck invalid: {exc}") from exc

        return {
            "saved": deck_id,
            "overwrote": existed,
            "decks": _serialize_decks(),
        }

    @app.delete("/decks/{deck_id}")
    def decks_delete(deck_id: str) -> dict[str, Any]:
        try:
            path = deck_file_path(deck_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            path.unlink()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"could not delete: {exc}") from exc
        _patch_fake_fill_after_deck_removal(deck_id)
        return {"deleted": deck_id, "decks": _serialize_decks()}

    @app.get("/fake-fill")
    def fake_fill_get() -> dict[str, Any]:
        return _serialize_fake_fill(get_fake_fill_config())

    @app.put("/fake-fill")
    def fake_fill_put(body: FakeFillUpdateBody) -> dict[str, Any]:
        updates = body.model_dump(exclude_unset=True)
        cfg = update_fake_fill_config(updates)
        return _serialize_fake_fill(cfg)

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
