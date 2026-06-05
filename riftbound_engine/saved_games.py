"""Persistent list of saved games.

A *saved game* is an encoding sufficient to drop the engine back into a known
position — board, branch tree, and Control view all follow. It records:

* ``setup`` — the fake-fill configuration (both decks, both battlefields,
  first turn, mulligan, mode, advanced seed). This alone reproduces the EARLY
  (hand back at the action turn) and ADVANCED (seeded shuffle + fast-forward to
  a contested battlefield) presets deterministically.
* ``moves`` — an OPTIONAL explicit list of ``{actor, action}`` engine moves to
  replay on top of the setup baseline, so an arbitrary in-progress position can
  be captured, not just the mode-derived ones. The seeded presets leave this
  empty and rely on the mode.

Stored as a single JSON file (``riftbound-engine/saved_games.json``) holding a
list, so the whole library is one human-readable, version-controllable file.

The engine glue that actually applies a game (reset + replay) lives in
``http_api.py``; this module is pure storage + CRUD.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from threading import Lock
from typing import Any

from .fake_fill import get_config

SAVED_GAMES_PATH = Path(__file__).resolve().parent.parent / "saved_games.json"
_STORE_VERSION = 1
_lock = Lock()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "game"


def _current_setup() -> dict[str, Any]:
    """Snapshot the live fake-fill config as a saved-game ``setup`` block."""
    cfg = get_config()
    return {
        "player_1_deck": cfg.player_1_deck,
        "player_2_deck": cfg.player_2_deck,
        "player_1_battlefield": cfg.player_1_battlefield,
        "player_2_battlefield": cfg.player_2_battlefield,
        "first_turn": cfg.first_turn.value,
        "mulligan_bottom": cfg.mulligan_bottom,
        "mode": cfg.mode.value,
        "advanced_seed": cfg.advanced_seed,
    }


def _preset(name: str, description: str, mode: str) -> dict[str, Any]:
    setup = _current_setup()
    setup["mode"] = mode
    return {
        "id": _slugify(name),
        "name": name,
        "description": description,
        "created_at": time.time(),
        "setup": setup,
        "moves": [],
    }


def _seed_store() -> dict[str, Any]:
    """The initial library: just the two presets Nathan already had."""
    return {
        "version": _STORE_VERSION,
        "games": [
            _preset(
                "Early",
                "Fresh shuffle; auto-pilots setup and hands control back at the action turn.",
                "early",
            ),
            _preset(
                "Advanced",
                "Seeded shuffle + hardcoded moves until a battlefield is contested by both players.",
                "advanced",
            ),
        ],
    }


def _read_store() -> dict[str, Any]:
    if not SAVED_GAMES_PATH.exists():
        store = _seed_store()
        _write_store(store)
        return store
    try:
        store = json.loads(SAVED_GAMES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        store = _seed_store()
        _write_store(store)
        return store
    if not isinstance(store, dict) or not isinstance(store.get("games"), list):
        store = _seed_store()
        _write_store(store)
    return store


def _write_store(store: dict[str, Any]) -> None:
    SAVED_GAMES_PATH.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")


def list_saved_games() -> list[dict[str, Any]]:
    with _lock:
        return list(_read_store()["games"])


def get_saved_game(game_id: str) -> dict[str, Any] | None:
    with _lock:
        for g in _read_store()["games"]:
            if g.get("id") == game_id:
                return g
    return None


def add_saved_game(
    name: str,
    description: str,
    setup: dict[str, Any],
    moves: list[dict[str, Any]],
) -> dict[str, Any]:
    """Append a new game. The id is a slug of the name, de-duplicated with a
    numeric suffix so saving two "My game"s doesn't collide."""
    with _lock:
        store = _read_store()
        existing = {g.get("id") for g in store["games"]}
        base = _slugify(name)
        gid = base
        n = 2
        while gid in existing:
            gid = f"{base}-{n}"
            n += 1
        game = {
            "id": gid,
            "name": name.strip() or gid,
            "description": description.strip(),
            "created_at": time.time(),
            "setup": setup,
            "moves": moves,
        }
        store["games"].append(game)
        _write_store(store)
        return game


def delete_saved_game(game_id: str) -> bool:
    with _lock:
        store = _read_store()
        before = len(store["games"])
        store["games"] = [g for g in store["games"] if g.get("id") != game_id]
        if len(store["games"]) == before:
            return False
        _write_store(store)
        return True
