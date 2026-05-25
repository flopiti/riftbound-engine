"""Runtime-mutable fake-fill config used to auto-pilot deck / battlefield / mulligan choices.

Historically these were module-level constants. They are now backed by a mutable
``FakeFillConfig`` so the control dashboard can update them at runtime via the HTTP
API. The original constant names are still exposed for backward compatibility with
``__main__.py`` (CLI), evaluated once at import time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from threading import Lock
from typing import Any

from .deck_files import list_deck_ids, load_deck_file
from .engine import RequiredTo

_DEFAULT_FALLBACK_BATTLEFIELD = "Altar to Unity"


@dataclass
class FakeFillConfig:
    # Fake fill is on by default — the dashboard auto-pilots deck /
    # battlefield / mulligan choices so the engine lands in the action turn
    # right away. Set the FAKE_FILL env var to "0"/"false"/"no"/"off" to
    # disable, or toggle it at runtime via the control dashboard.
    enabled: bool = True
    player_1_deck: str = ""
    player_2_deck: str = ""
    player_1_battlefield: str = ""
    player_2_battlefield: str = ""
    first_turn: RequiredTo = RequiredTo.PLAYER_1
    mulligan_bottom: str = ""  # comma-separated hand indices (0-3), max 2; empty = none


def _env_enabled_default() -> bool:
    """Read FAKE_FILL from the environment. Defaults to True when unset."""
    raw = os.getenv("FAKE_FILL", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return True


def _default_battlefield(deck_id: str) -> str:
    if not deck_id:
        return _DEFAULT_FALLBACK_BATTLEFIELD
    try:
        parsed = load_deck_file(deck_id)
        if parsed.battlefields:
            return parsed.battlefields[0]
    except Exception:
        pass
    return _DEFAULT_FALLBACK_BATTLEFIELD


def _initial_config() -> FakeFillConfig:
    ids = list_deck_ids()
    p1 = ids[0] if len(ids) >= 1 else ""
    p2 = ids[1] if len(ids) >= 2 else (ids[0] if ids else "")
    return FakeFillConfig(
        enabled=_env_enabled_default(),
        player_1_deck=p1,
        player_2_deck=p2,
        player_1_battlefield=_default_battlefield(p1),
        player_2_battlefield=_default_battlefield(p2),
        first_turn=RequiredTo.PLAYER_1,
        mulligan_bottom="",
    )


_lock = Lock()
_config: FakeFillConfig = _initial_config()


def get_config() -> FakeFillConfig:
    """Return a snapshot of the current config (callers may not mutate it)."""
    with _lock:
        return replace(_config)


_ALLOWED_FIELDS = {
    "enabled",
    "player_1_deck",
    "player_2_deck",
    "player_1_battlefield",
    "player_2_battlefield",
    "first_turn",
    "mulligan_bottom",
}


def update_config(updates: dict[str, Any]) -> FakeFillConfig:
    """Apply a partial update. Unknown keys are ignored. Returns the new config."""
    global _config
    with _lock:
        for key, value in updates.items():
            if key not in _ALLOWED_FIELDS or value is None:
                continue
            if key == "first_turn":
                if not isinstance(value, RequiredTo):
                    try:
                        value = RequiredTo(value)
                    except ValueError:
                        continue
                if value not in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
                    continue
            elif key == "enabled":
                value = bool(value)
            elif key == "mulligan_bottom":
                value = str(value)
            else:
                value = str(value)
            setattr(_config, key, value)
        return replace(_config)


def list_decks_with_battlefields() -> list[dict[str, Any]]:
    """List every deck file and the battlefields it offers (for UI dropdowns)."""
    out: list[dict[str, Any]] = []
    for deck_id in list_deck_ids():
        battlefields: list[str] = []
        try:
            parsed = load_deck_file(deck_id)
            battlefields = list(parsed.battlefields)
        except Exception:
            battlefields = []
        out.append({"id": deck_id, "battlefields": battlefields})
    return out


def choices_dict() -> dict[RequiredTo, str]:
    cfg = get_config()
    return {RequiredTo.PLAYER_1: cfg.player_1_deck, RequiredTo.PLAYER_2: cfg.player_2_deck}


def battlefields_dict() -> dict[RequiredTo, str]:
    cfg = get_config()
    return {
        RequiredTo.PLAYER_1: cfg.player_1_battlefield,
        RequiredTo.PLAYER_2: cfg.player_2_battlefield,
    }


# --- Backward-compat constants for the CLI (riftbound_engine/__main__.py) ---
# Evaluated once at import time. The CLI runs short-lived processes, so this is fine.
# HTTP API code uses get_config() directly so it always sees the latest values.
FAKE_FILL_CHOICES: dict[RequiredTo, str] = choices_dict()
FAKE_FILL_BATTLEFIELDS: dict[RequiredTo, str] = battlefields_dict()
FAKE_FILL_FIRST_TURN: RequiredTo = get_config().first_turn
FAKE_FILL_MULLIGAN_BOTTOM: str = get_config().mulligan_bottom
