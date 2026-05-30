"""HTTP adapter for `GameEngine` — poll state and POST actions (FastAPI)."""

from __future__ import annotations

import copy
import os
import random
import re
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .csv_data import card_domains_of, card_energy_of, card_power_of
from .deck_files import DECKS_DIR, deck_file_path, list_deck_ids, load_deck_file
from .engine import Deck, EngineOutput, GameEngine, GameState, RequiredTo
from .fake_fill import (
    FakeFillConfig,
    FakeFillMode,
    battlefields_dict,
    choices_dict,
    get_config as get_fake_fill_config,
    list_decks_with_battlefields,
    update_config as update_fake_fill_config,
)
from .protocol import ApplyVerb, RequiredStep
from .shortcuts import Shortcut, compute_play_intents, serialize_play_intent

# Reentrant: several of the endpoint handlers acquire this lock and then call
# helpers that internally re-acquire it (e.g. /branch handlers wrap
# _serialize_branch -> get_snapshot, and get_snapshot does its own `with
# _engine_lock`). A plain Lock would deadlock the whole worker thread; RLock
# lets the same thread re-enter freely.
_engine_lock = threading.RLock()
_engine: GameEngine = GameEngine()
_last_output: EngineOutput | None = None

# Saved game states for the branch tree, parallel to `_branch_path`.
# `_branch_states[i]` is a deep copy of the GameState produced AFTER the
# action recorded in `_branch_path[i]` was applied. /branch/back restores
# `_branch_states[len(new_path) - 1]` (or `_initial_state` when trimming
# the path all the way back to empty) so the engine actually moves back
# to that step's exact game state — same library shuffle, same hand, same
# pending_play, etc. — without ever calling /reset.
#
# Why path-indexed instead of counter-indexed:
#   The engine's `counter` field is incremented exactly once per game
#   (during _finalize_setup_after_mulligan), NOT once per action. Keying
#   saved states by counter therefore collapses every post-setup state
#   onto the same slot — every /branch/forward overwrites the previous
#   save, and /goto-by-counter restores whichever state was most recently
#   written. This is the bug that made "go back to Play X unit and the
#   Place option disappears" — backward navigation was silently restoring
#   the deepest state instead of the intended step.
#
# Forking: when the user trims and then extends, the trailing states get
# truncated alongside `_branch_path`, so the freshly-overwritten slot
# matches the new fork. The visited set still remembers prefixes from
# discarded branches for connector-line highlighting.
_initial_state: GameState | None = None
_branch_states: list[GameState] = []

# ---------------------------------------------------------------------------
# Branch-tree state (server-authoritative).
#
# The /branch UI is a "branch exploration tool" — the user clicks options to
# advance the game, then can navigate backward to inspect earlier states or
# fork into a different sub-tree. We keep ALL of that state here on the
# server so:
#   1. Reloading the browser preserves the exact tree position (the frontend
#      simply re-polls /branch on mount and rehydrates from the response).
#   2. Backward navigation actually moves the engine (via /goto). The Control
#      board, which renders straight from the engine snapshot, follows along
#      because both views read from the same underlying state.
#
# The path is a single linear sequence — when the user navigates back and
# then picks a different forward option, the old future branch is discarded
# (matches the "real branch tree" semantics the user picked). The visited
# set still remembers every prefix that's been walked so the UI can paint
# already-explored connector lines in purple.
# ---------------------------------------------------------------------------

# One step in the visible branch-tree path. Stores enough to (a) re-render
# the node on the client and (b) rewind the engine via /goto to any earlier
# step's counter. We keep the entry as a plain dict (not a dataclass) so it
# JSON-roundtrips trivially — the client treats it as opaque.
BranchStep = dict[str, Any]

_branch_path: list[BranchStep] = []
# Set of pathKey strings ("actor|action||actor|action||...") representing
# every prefix the user has ever walked this session. Survives backward
# navigation so the connector lines for previously-explored branches stay
# highlighted even after the user trims back past them.
_branch_visited: set[str] = set()
# Monotonic counter — total forward clicks in this session, independent of
# rewinds. Powers the "Visited N" header chip.
_branch_nodes_visited: int = 0


def _branch_path_key(steps: list[BranchStep]) -> str:
    """Stable, comparable key for a path prefix. Matches the client-side
    pathKey() format in BranchApp.tsx so the visited set stays mutually
    intelligible between the two sides."""
    return "||".join(f"{s.get('actor', '')}|{s.get('action', '')}" for s in steps)


def _reset_branch_tree() -> None:
    """Wipe the branch-tree state. Called whenever the engine itself is
    reset (the path's saved counters would be stale references to a game
    that no longer exists)."""
    global _branch_path, _branch_visited, _branch_nodes_visited
    _branch_path = []
    _branch_visited = set()
    _branch_nodes_visited = 0


def _capture_state() -> GameState:
    """Return a deep copy of the current engine state, isolated from future
    engine mutations. The `_engine.game_state` property is already a partial
    copy (re-creates lists, etc.) but Rune/PendingPlay/PendingPayment are
    nested mutables on its result, so we deep-copy once more for safety."""
    return copy.deepcopy(_engine.game_state)


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


def _battlefield_contested(gs: GameState) -> bool:
    """Stop condition for the advanced auto-play loop.

    A battlefield counts as "contested by both players" the moment any
    single battlefield (battlefield_1 OR battlefield_2) has at least one
    unit from each player at it. This is the literal interpretation Nathan
    picked in the design questions — base territories don't count, and we
    don't require BOTH battlefields to be contested.
    """
    p1_bfs = {u.location for u in gs.player_1_units if u.location.startswith("battlefield_")}
    p2_bfs = {u.location for u in gs.player_2_units if u.location.startswith("battlefield_")}
    return bool(p1_bfs & p2_bfs)


def _shortcut_to_actions(shortcut: Shortcut) -> list[str]:
    """Expand one Shortcut into the engine-action strings that execute it.

    Port of the client-side ``executionSteps`` in
    ``riftbound/src/utils/shortcuts.ts``. The tricky bit is that
    ``recycle_rune`` / ``exhaust_and_recycle_rune`` POP runes from the pool,
    shifting indices for everything still in the pool; plain ``exhaust_rune``
    does not. We therefore:

    1. Run all pop-actions FIRST, in descending rune_index order, so no pop
       disturbs the indices of pending pop targets.
    2. For each ``exhaust_rune``, remap its original index by subtracting the
       count of already-popped lower-index runes.
    3. Finally emit ``play:play_unit:<i>`` / ``play:play_spell:<i>``.

    The advanced auto-play loop appends ``play:choose_location:*`` separately
    when a play_unit ends up with a pending_play.
    """
    pops = sorted(
        (s for s in shortcut.plan if s.action != "exhaust_rune"),
        key=lambda s: -s.rune_index,
    )
    exhausts = [s for s in shortcut.plan if s.action == "exhaust_rune"]
    pop_indices_asc = sorted(p.rune_index for p in pops)

    out: list[str] = []
    for p in pops:
        out.append(f"play:{p.action}:{p.rune_index}")
    for e in exhausts:
        shift = sum(1 for i in pop_indices_asc if i < e.rune_index)
        out.append(f"play:exhaust_rune:{e.rune_index - shift}")
    out.append(f"play:{shortcut.play_action}:{shortcut.card_index}")
    return out


def _best_play_unit_location(gs: GameState, actor: RequiredTo) -> str:
    """Where to place a FRESHLY PLAYED unit.

    ``play_unit`` is gated by ``player_controls_location`` — the engine
    refuses to place a fresh unit on a BF the active player doesn't
    already control. So this function only ever returns a CONTROLLABLE
    location:

      1. A battlefield the active player already controls (consolidate
         board presence at the front line).
      2. Base (always controllable; the fallback for turn 1 when no BF
         is yet controlled).

    Aggression onto an uncontrolled or opponent BF goes through
    ``move_unit`` instead — see ``_advanced_auto_play``.
    """
    for bf_key, ctrl in (
        ("battlefield_1", gs.battlefield_1_controller),
        ("battlefield_2", gs.battlefield_2_controller),
    ):
        if ctrl == actor:
            return bf_key
    return "base"


def _aggression_target(gs: GameState, actor: RequiredTo) -> str | None:
    """Pick a battlefield ``actor`` should move a ready unit into to push
    the game toward the contested-BF stop condition.

    Preference order:
      1. A battlefield the OPPONENT controls — moving onto it directly
         contests the BF (both players will have units there once the
         move applies).
      2. An UNCONTROLLED battlefield — opens a fresh showdown, the loop's
         pass/pass resolution hands it to the initiator. The opponent's
         next aggression move into that BF is what eventually contests it.

    Returns ``None`` when both BFs are already controlled by ``actor``
    (nothing to push into) or when no BFs exist yet.
    """
    opponent = GameEngine.opponent_of(actor)
    # First pass: an opponent-controlled BF (contests immediately).
    for bf_key, ctrl in (
        ("battlefield_1", gs.battlefield_1_controller),
        ("battlefield_2", gs.battlefield_2_controller),
    ):
        if ctrl == opponent:
            return bf_key
    # Second pass: an uncontrolled BF.
    for bf_key, ctrl in (
        ("battlefield_1", gs.battlefield_1_controller),
        ("battlefield_2", gs.battlefield_2_controller),
    ):
        if ctrl is None:
            return bf_key
    return None


def _ready_unit_at_base(gs: GameState, actor: RequiredTo) -> int | None:
    """Index of a ready (non-exhausted) unit owned by ``actor`` sitting at
    base, or ``None`` if no such unit exists. Used by the advanced loop to
    decide whether ``move_unit`` is available this turn.
    """
    units = gs.player_1_units if actor == RequiredTo.PLAYER_1 else gs.player_2_units
    for i, u in enumerate(units):
        if u.location == "base" and not u.exhausted:
            return i
    return None


#: When set (only during reset's advanced fast-forward), every action the
#: auto-play applies is reported here so reset_engine can rebuild the
#: branch-tree path/states from the exact moves it made. None ⇒ no recording.
_step_recorder: "Callable[[str, str], None] | None" = None


def _apply(engine: GameEngine, action: str, actor: RequiredTo) -> EngineOutput:
    """Apply one auto-play action AND report it to the active step recorder
    (if any), so each fast-forwarded move becomes a branch-tree node instead
    of vanishing into a single 'start' node."""
    out = engine.apply_action(action=action, actor=actor)
    if _step_recorder is not None:
        _step_recorder(actor.value, action)
    return out


def _resolve_pending_showdown(engine: GameEngine, output: EngineOutput) -> EngineOutput:
    """Both players pass any pending showdown.

    Advanced auto-play is non-interactive: we don't play "showdown spells",
    so the deterministic resolution is simply initiator passes, then
    opponent passes. The engine takes care of awarding the battlefield to
    the initiator.
    """
    while output.game_state.pending_showdown is not None:
        sd = output.game_state.pending_showdown
        if not sd.initiator_passed:
            output = _apply(engine, "play:pass_showdown", sd.initiator)
            continue
        opp = GameEngine.opponent_of(sd.initiator)
        if not sd.opponent_passed:
            output = _apply(engine, "play:pass_showdown", opp)
            continue
        # Both flagged passed but pending_showdown still set — shouldn't
        # happen, but guard against an infinite loop.
        break
    return output


def _advanced_auto_play(engine: GameEngine, output: EngineOutput) -> EngineOutput:
    """Run hardcoded moves through the action turn until a battlefield is
    contested by both players.

    The "hardcoded script" is deterministic in two senses:

    1. The library shuffle is seeded (see ``reset_engine``), so the cards
       drawn at each index are reproducible across resets.
    2. The play algorithm itself is fully deterministic — given the same
       hand and pool it always picks the same action: try to play the
       first affordable Unit (via the leftmost combo from
       ``compute_play_intents``), place it at the most aggressive
       controllable battlefield, end the turn when no Unit can be played.

    Together those two properties mean: same seed + same decks ⇒ same
    sequence of moves, every reset. That's the "fixed draw + hardcoded
    moves" Nathan asked for.

    The loop short-circuits the moment ``_battlefield_contested`` returns
    True, so as soon as both players have a unit at the same BF we stop
    and hand control back. Any illegal move raises (per the "this can't
    happen" design call) — see the docstring on ``FakeFillMode.ADVANCED``.
    """
    # Outer safety cap. A typical run resolves in 5–15 actions; 256 is a
    # generous upper bound that prevents a runaway loop if the script ever
    # ends up in a state where neither player can play a unit AND no
    # battlefield gets contested.
    for _ in range(256):
        gs = output.game_state
        if _battlefield_contested(gs):
            return output

        ra = output.required_action
        if ra is None:
            return output
        if ra.name != RequiredStep.ACTION_TURN:
            # Still in setup, or a state the advanced loop doesn't drive.
            return output

        # Showdowns can also pop in the middle of a turn (e.g. moving a
        # ready unit into an uncontrolled battlefield). Resolve before
        # planning the next play.
        if gs.pending_showdown is not None:
            output = _resolve_pending_showdown(engine, output)
            continue

        # Defensive: if anything ever leaves a pending_play around at the
        # top of the loop (the inner "play then choose_location" pair
        # already handles the normal case), resolve it before trying to
        # compute new intents — compute_play_intents short-circuits to []
        # when pending_play is set, which would otherwise end the turn
        # mid-play and crash.
        if gs.pending_play is not None:
            active_pp = gs.pending_play.actor
            loc = _best_play_unit_location(gs, active_pp)
            output = _apply(engine, f"play:choose_location:{loc}", active_pp)
            if output.game_state.pending_showdown is not None:
                output = _resolve_pending_showdown(engine, output)
            continue

        active = gs.current_player

        # Priority 1: push a ready base unit onto a BF we don't control.
        # This is the action that actually moves the game toward the
        # contested-BF stop condition — fresh plays go to base, and only
        # move_unit can contest a battlefield. We pick this before
        # play_unit so a ready unit doesn't sit idle at base.
        ready_idx = _ready_unit_at_base(gs, active)
        target_bf = _aggression_target(gs, active) if ready_idx is not None else None
        if ready_idx is not None and target_bf is not None:
            output = _apply(engine, f"play:move_unit:{ready_idx}:{target_bf}", active)
            # move_unit onto an uncontrolled-or-opponent BF opens a showdown.
            # The stop condition can already be true at this point (the
            # opponent had a unit at that BF) — checked at the top of the
            # next iteration.
            if output.game_state.pending_showdown is not None:
                output = _resolve_pending_showdown(engine, output)
            continue

        # Priority 2: play the cheapest affordable Unit from hand to a
        # controllable location (base, or a BF we already own).
        intents = compute_play_intents(engine, active)
        unit_intents = [i for i in intents if i.play_action == "play_unit"]
        target = unit_intents[0] if unit_intents else None

        if target is not None and target.combos:
            for action_str in _shortcut_to_actions(target.combos[0]):
                output = _apply(engine, action_str, active)
            # play_unit always leaves a pending_play waiting for a location.
            if output.game_state.pending_play is not None:
                loc = _best_play_unit_location(output.game_state, active)
                output = _apply(engine, f"play:choose_location:{loc}", active)
            continue

        # Priority 3: nothing to play, nothing to move — end the turn.
        output = _apply(engine, "play:end_turn", active)

    return output


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

    # NOTE: ADVANCED mode's gameplay fast-forward is intentionally NOT run
    # here. It used to run on every snapshot poll, which (a) re-advanced the
    # game after a /branch/back, defeating rewind, and (b) recorded no branch
    # nodes, so the tree showed only "start". Advanced fast-forward now runs
    # ONCE in reset_engine, recording each move as a branch step. This
    # function only auto-resolves the setup choices (idempotent on re-poll).
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
            "mode": cfg.mode.value,
            "advanced_seed": cfg.advanced_seed,
        },
        "decks": list_decks_with_battlefields(),
    }


def reset_engine() -> EngineOutput:
    """Recreate the engine, run start(), then apply fake-fill auto-pilot.

    In ADVANCED mode the engine is constructed with a SEEDED RNG so the
    library and rune-library shuffles produce the same order every time —
    this is what lets the advanced auto-play script's hardcoded moves stay
    valid across resets. EARLY mode (and the case when fake-fill is
    disabled entirely) uses a fresh ``random.Random()``, matching the
    original non-deterministic behaviour.
    """
    global _engine, _last_output, _initial_state, _branch_states
    cfg = get_fake_fill_config()
    if cfg.enabled and cfg.mode == FakeFillMode.ADVANCED:
        _engine = GameEngine(rng=random.Random(cfg.advanced_seed))
    else:
        _engine = GameEngine()
    _last_output = _engine.start()
    _last_output = _auto_fake_fill(_engine, _last_output)
    # Branch-tree ROOT = the state at the start of the action turn (setup
    # auto-resolved, nothing played yet). /branch/back to an empty path
    # restores this.
    _initial_state = _capture_state()
    _branch_states = []
    _reset_branch_tree()

    # ADVANCED: fast-forward the opening moves ONCE, recording each as a
    # branch node so the tree reflects the jumped-forward position (and the
    # moves are rewindable). EARLY mode lands at the action turn with an
    # empty path, as before.
    if (
        cfg.enabled
        and cfg.mode == FakeFillMode.ADVANCED
        and _last_output.required_action is not None
        and _last_output.required_action.name == RequiredStep.ACTION_TURN
    ):
        _last_output = _record_advanced_fast_forward(_last_output)
    return _last_output


def _record_advanced_fast_forward(output: EngineOutput) -> EngineOutput:
    """Run the advanced gameplay fast-forward ONCE, recording every move as a
    branch-tree node (with the post-move state captured) so the tree shows
    the jumped-forward position and each move can be rewound to."""
    global _step_recorder, _branch_path, _branch_states, _branch_nodes_visited
    path: list[BranchStep] = []
    states: list[GameState] = []

    def rec(actor_val: str, action: str) -> None:
        path.append(
            {
                "actor": actor_val,
                "action": action,
                "label": action,
                "intent_only": False,
                "shortcut": None,
                "intent": None,
            }
        )
        states.append(_capture_state())

    _step_recorder = rec
    try:
        output = _advanced_auto_play(_engine, output)
    finally:
        _step_recorder = None

    _branch_path = path
    _branch_states = states
    _branch_nodes_visited = len(path)
    for i in range(1, len(path) + 1):
        _branch_visited.add(_branch_path_key(path[:i]))
    return output


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
        "action_log": [
            {"sequence": e.sequence, "actor": e.actor, "action": e.action}
            for e in gs.action_log
        ],
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
        "player_1_spells": [
            {"card": s.card, "targets": list(s.targets), "order": s.order}
            for s in gs.player_1_spells
        ],
        "player_2_spells": [
            {"card": s.card, "targets": list(s.targets), "order": s.order}
            for s in gs.player_2_spells
        ],
        "player_1_gears": [
            {"card": g.card, "location": g.location, "exhausted": g.exhausted}
            for g in gs.player_1_gears
        ],
        "player_2_gears": [
            {"card": g.card, "location": g.location, "exhausted": g.exhausted}
            for g in gs.player_2_gears
        ],
        # Dead units (e.g. killed in combat), by card name, in death order.
        "player_1_trash": list(gs.player_1_trash),
        "player_2_trash": list(gs.player_2_trash),
        "pending_play": (
            None
            if gs.pending_play is None
            else {"actor": gs.pending_play.actor.value, "card": gs.pending_play.card}
        ),
        "pending_spell_choice": (
            None
            if gs.pending_spell_choice is None
            else {
                "actor": gs.pending_spell_choice.actor.value,
                "card": gs.pending_spell_choice.card,
                "requirement": gs.pending_spell_choice.requirement,
            }
        ),
        "pending_chain": (
            None
            if gs.pending_chain is None
            else {
                "priority": gs.pending_chain.priority.value,
                "consecutive_passes": gs.pending_chain.consecutive_passes,
                "items": [
                    {"actor": it.actor.value, "card": it.card, "targets": list(it.targets)}
                    for it in gs.pending_chain.items
                ],
            }
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
        "pending_combat": (
            None
            if gs.pending_combat is None
            else {
                "battlefield": gs.pending_combat.battlefield,
                "player_1_might": gs.pending_combat.player_1_might,
                "player_2_might": gs.pending_combat.player_2_might,
                # `null` = "still picking", list = "committed".
                "player_1_targets": (
                    None
                    if gs.pending_combat.player_1_targets is None
                    else list(gs.pending_combat.player_1_targets)
                ),
                "player_2_targets": (
                    None
                    if gs.pending_combat.player_2_targets is None
                    else list(gs.pending_combat.player_2_targets)
                ),
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
    # Play intents = the tier-1 "play this card from hand" picker. One
    # entry per playable hand card carrying the PRINTED cost and every
    # valid rune-payment combo. The client renders single-card chips
    # from these; single-combo intents auto-skip to the chain on click,
    # multi-combo intents drop into the tier-2 disambiguation column.
    # Returns [] for any player who isn't the active actor or whose
    # state is mid-resolution (pending_play / pending_payment /
    # showdown).
    p1_intents = [serialize_play_intent(i) for i in compute_play_intents(_engine, RequiredTo.PLAYER_1)]
    p2_intents = [serialize_play_intent(i) for i in compute_play_intents(_engine, RequiredTo.PLAYER_2)]
    return {
        "state": _serialize_state(out.game_state),
        "player_1_options": list(out.player_1_options),
        "player_2_options": list(out.player_2_options),
        "player_1_intents": p1_intents,
        "player_2_intents": p2_intents,
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


def _serialize_branch() -> dict[str, Any]:
    """Bundle the live engine snapshot together with the branch-tree path /
    visited set / nodes-visited counter. This is the single payload the
    /branch GET endpoint returns — the client hydrates ALL of its UI state
    from it on every poll, including initial mount after a page refresh.

    The snapshot is computed via get_snapshot() so it includes the same
    start() + fake-fill auto-advance the regular /state endpoint runs."""
    return {
        "path": [dict(step) for step in _branch_path],
        "visited": sorted(_branch_visited),
        "nodes_visited": _branch_nodes_visited,
        "snapshot": get_snapshot(),
    }


def _restore_state(state: GameState) -> EngineOutput:
    """Rebuild the engine on a deep copy of `state`. Used by /branch/back
    to point the engine at an earlier path step's snapshot without ever
    calling /reset (which would reshuffle the deck).

    Deep-copying the source isolates our saved entry from any subsequent
    engine mutations — if the user re-walks the same branch later we need
    to be able to restore from the same entry again without it having
    drifted along with the engine's continued play."""
    global _engine, _last_output
    _engine = GameEngine(game_state=copy.deepcopy(state))
    # start() recomputes the EngineOutput's option list and required_action
    # from the freshly-installed state. Idempotent for an already-started
    # state — for mid-play snapshots (pending_play set) it returns the
    # location-choice options, which is exactly what the user expects to
    # see after stepping back to a play_unit node.
    _last_output = _engine.start()
    return _last_output


class ActionBody(BaseModel):
    actor: str = Field(..., description="player_1, player_2, or both")
    action: str = Field(..., min_length=1)


class BranchChainStep(BaseModel):
    """One engine action in a multi-step "shortcut" chain. The /branch
    endpoint expects shortcuts pre-expanded into the raw actions the engine
    accepts — the server doesn't run the shortcut-planning logic itself."""

    actor: str = Field(..., description="player_1, player_2, or both")
    action: str = Field(..., min_length=1)


class BranchForwardBody(BaseModel):
    """Append one step to the branch-tree path.

    Three flavours of forward-click are routed through this endpoint:

    * Regular engine action — `chain` is empty (or one entry equal to
      {actor, action}); the server applies {actor, action} and snapshots
      the resulting GameState into `_branch_states[new_path_length - 1]`
      so /branch/back can restore the engine to this exact point later.
    * Shortcut / combo — `chain` carries the pre-planned sequence of rune
      actions and the final play_unit/play_spell. The server applies them
      in order; only ONE state is snapshotted (the final one).
    * UI-only intent (`intent_only: True`) — no engine work; the step is
      recorded and the snapshot is just the current state (unchanged).
      Used by the two-tier shortcut picker between "I chose this card"
      and "I picked a specific combo".
    """

    actor: str = Field(..., description="actor that owns the displayed option")
    action: str = Field(..., min_length=1, description="raw or synthetic action string")
    label: str = Field(..., description="human-readable label captured client-side")
    intent_only: bool = Field(default=False, description="True for UI-only intent picks")
    chain: list[BranchChainStep] = Field(
        default_factory=list,
        description="pre-expanded engine actions to apply in order; empty for plain {actor,action}",
    )
    # Opaque metadata passed through verbatim. The client uses `shortcut`
    # and `intent` to keep its rendering rich (icons, costs, disambiguation
    # combos). The server treats them as a black box.
    shortcut: dict[str, Any] | None = None
    intent: dict[str, Any] | None = None


class BranchBackBody(BaseModel):
    """Trim the tail of the branch path and roll the engine back to the new
    tip. `steps=1` undoes the most recent click; `steps=len(path)` walks
    all the way back to the start screen (path becomes empty, engine sits
    at the initial post-fake-fill counter)."""

    steps: int = Field(..., gt=0, description="number of path entries to remove")


class FakeFillUpdateBody(BaseModel):
    """Partial update for the fake-fill runtime config. Any omitted field is left alone."""

    enabled: bool | None = None
    player_1_deck: str | None = None
    player_2_deck: str | None = None
    player_1_battlefield: str | None = None
    player_2_battlefield: str | None = None
    first_turn: str | None = Field(default=None, description="player_1 or player_2")
    mulligan_bottom: str | None = None
    mode: str | None = Field(default=None, description="'early' or 'advanced'")
    advanced_seed: int | None = Field(
        default=None,
        description="RNG seed used to shuffle the library in advanced mode (reproducible draw)",
    )


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
            # NOTE: /action is the Control board's path; it does NOT touch the
            # branch-tree state because actions taken there are outside the
            # exploration model. If you need a branch-tree-aware action use
            # /branch/forward instead.
            return _serialize_output(_last_output)

    # ---------------- Branch-tree (server-authoritative) ----------------

    @app.get("/branch")
    def branch_get() -> dict[str, Any]:
        """Return the current branch-tree path + visited set + nodes-visited
        counter alongside the live engine snapshot. The frontend polls this
        instead of caching path state locally, which is what lets a page
        refresh restore the exact same tree position."""
        with _engine_lock:
            return _serialize_branch()

    @app.post("/branch/forward")
    def branch_forward(body: BranchForwardBody) -> dict[str, Any]:
        """Append one step to the branch-tree path.

        Forking is handled implicitly: by the time the client calls this
        endpoint it has already called /branch/back to trim the path back
        to the fork point, so /branch/forward only ever extends the live
        tail. The discarded future branch is wiped from `_branch_path`
        AND from `_branch_states`, but the visited set keeps every
        prefix the user has ever walked so the connector lines for
        already-explored options keep their "visited" highlight.
        """
        global _branch_path, _branch_states, _branch_visited, _branch_nodes_visited, _last_output
        with _engine_lock:
            if not body.intent_only:
                # Validate actor up-front so a bad client payload doesn't
                # half-apply a chain. Per-step actors validated inside the
                # loop the same way.
                try:
                    main_actor = RequiredTo(body.actor)
                except ValueError as e:
                    raise HTTPException(
                        status_code=400, detail=f"invalid actor: {body.actor}"
                    ) from e

                if body.chain:
                    steps_to_run: list[tuple[RequiredTo, str]] = []
                    for s in body.chain:
                        try:
                            steps_to_run.append((RequiredTo(s.actor), s.action))
                        except ValueError as e:
                            raise HTTPException(
                                status_code=400,
                                detail=f"invalid actor in chain: {s.actor}",
                            ) from e
                else:
                    steps_to_run = [(main_actor, body.action)]

                try:
                    for actor, action in steps_to_run:
                        _last_output = _engine.apply_action(action=action, actor=actor)
                    _last_output = _auto_fake_fill(_engine, _last_output)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e

            # Snapshot AFTER the action(s) (or AFTER no-op for intent_only).
            # _branch_states is parallel to _branch_path, so the new state
            # gets appended right alongside the step we're about to record.
            saved = _capture_state()

            step: BranchStep = {
                "actor": body.actor,
                "action": body.action,
                "label": body.label,
                "intent_only": body.intent_only,
                "shortcut": body.shortcut,
                "intent": body.intent,
            }
            _branch_path = [*_branch_path, step]
            _branch_states = [*_branch_states, saved]

            # Visited set: mark every prefix of the new path so its
            # connector line stays highlighted even if the user later
            # forks past this point and discards the suffix.
            for i in range(1, len(_branch_path) + 1):
                _branch_visited.add(_branch_path_key(_branch_path[:i]))

            _branch_nodes_visited += 1
            return _serialize_branch()

    @app.post("/branch/back")
    def branch_back(body: BranchBackBody) -> dict[str, Any]:
        """Trim the tail of the path and restore the engine to the new
        tip's saved state. `steps` must be in [1, len(path)]; trimming
        all the way to `[]` rolls the engine back to the initial state
        captured at reset_engine time (post-fake-fill).

        This is what makes the Control board follow when the user
        navigates backward in the branch tree: both views read from the
        live engine snapshot, and rebuilding the engine on the saved
        GameState means the snapshot the next poll returns reflects the
        earlier game state — same library shuffle, same hand, same
        pending_play, no deck reshuffle."""
        global _branch_path, _branch_states
        with _engine_lock:
            if body.steps > len(_branch_path):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"cannot rewind {body.steps} steps from a path of length "
                        f"{len(_branch_path)}"
                    ),
                )

            new_len = len(_branch_path) - body.steps
            # Target state: the snapshot taken after the (new) tip's
            # action, or the starting state when we trim back to empty.
            if new_len > 0:
                target_state = _branch_states[new_len - 1]
            elif _initial_state is not None:
                target_state = _initial_state
            else:
                raise HTTPException(
                    status_code=500,
                    detail="no saved starting state to rewind to; please reset the engine",
                )

            _restore_state(target_state)
            _branch_path = _branch_path[:new_len]
            _branch_states = _branch_states[:new_len]
            return _serialize_branch()

    @app.post("/branch/reset")
    def branch_reset_endpoint() -> dict[str, Any]:
        """Clear the path + visited set WITHOUT touching the engine. Use
        /reset for a full game restart that also reshuffles the deck."""
        with _engine_lock:
            _reset_branch_tree()
            return _serialize_branch()

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("ENGINE_API_HOST", "127.0.0.1")
    port = int(os.getenv("ENGINE_API_PORT", "8790"))
    uvicorn.run("riftbound_engine.http_api:app", host=host, port=port, reload=False)
