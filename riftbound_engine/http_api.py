"""HTTP adapter for `GameEngine` — poll state and POST actions (FastAPI)."""

from __future__ import annotations

import copy
import multiprocessing as mp
import os
import queue as _queue
import random
import re
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .action_label import label_for_action
from .abilities import attached_might_bonus, reset_caches as _reset_ability_caches
from .csv_data import card_domains_of, card_energy_of, card_might_of, card_power_of
from .deck_files import DECKS_DIR, deck_file_path, list_deck_ids, load_deck_file
from .engine import VICTORY_SCORE, Deck, EngineOutput, GameEngine, GameState, RequiredTo
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
from .saved_games import (
    add_saved_game,
    delete_saved_game,
    get_saved_game,
    list_saved_games,
    update_saved_game,
)
from .search import (
    SearchResult,
    apply_deck_changes,
    bfs_search,
)


def _search_worker(start, predicate, max_depth, node_budget, time_budget_s, out_q):
    """Run the brute-force search in a CHILD process so a pathological engine
    state (e.g. a deep combat exploration that loops) can be hard-killed via
    timeout instead of freezing the request thread."""
    try:
        out_q.put(
            bfs_search(
                start,
                predicate,
                max_depth=max_depth,
                node_budget=node_budget,
                time_budget_s=time_budget_s,
                verbose=True,
            )
        )
    except Exception as e:  # pragma: no cover - defensive
        out_q.put(e)


def _run_search_guarded(start, predicate, max_depth, node_budget, time_budget_s) -> SearchResult:
    """Run bfs_search in a child process; if it overruns its time budget (or
    hangs in an engine edge case), terminate it and report not-found rather
    than blocking forever."""
    # 'fork' (not 'spawn'): the child is a memory copy — no module re-import
    # (which would re-run uvicorn/__main__) — and a forked CPU-bound child is
    # reliably killed by terminate() between bytecodes.
    ctx = mp.get_context("fork")
    out_q = ctx.Queue()
    proc = ctx.Process(
        target=_search_worker,
        args=(start, predicate, max_depth, node_budget, time_budget_s, out_q),
        daemon=True,
    )
    proc.start()
    try:
        result = out_q.get(timeout=time_budget_s + 15)
    except _queue.Empty:
        result = SearchResult(found=False, reason="search timed out / hung — killed")
    finally:
        if proc.is_alive():
            proc.terminate()
        proc.join(timeout=2)
    if isinstance(result, Exception):
        return SearchResult(found=False, reason=f"search error: {result}")
    return result
from .shortcuts import (
    Shortcut,
    compute_accelerate_intents,
    compute_equip_intents,
    compute_move_intents,
    compute_play_intents,
    compute_quick_draw_intents,
    compute_repeat_intents,
    compute_shortcuts,
    serialize_play_intent,
)

# Reentrant: several of the endpoint handlers acquire this lock and then call
# helpers that internally re-acquire it (e.g. /branch handlers wrap
# _serialize_branch -> get_snapshot, and get_snapshot does its own `with
# _engine_lock`). A plain Lock would deadlock the whole worker thread; RLock
# lets the same thread re-enter freely.
_engine_lock = threading.RLock()
_engine: GameEngine = GameEngine()
_last_output: EngineOutput | None = None
# The RNG seed that produced the CURRENT engine's shuffle. Recorded on every
# reset (even in `early` mode, where the seed itself is random) so "save game"
# can capture it and a later load re-deals the exact same cards.
_last_shuffle_seed: int | None = None

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
# The post-setup, start-of-action-turn baseline (decks/battlefields/mulligan
# resolved, nothing played). This is the CLEAN ROOT that /branch/search generates
# examples from — distinct from `_initial_state`, which is now the empty
# pre-setup state used as the rewind floor for the recorded setup nodes. Keeping
# them separate means "reach a state" still searches from a real turn-1 game (so
# e.g. "X in hand" just stacks the opening hand) instead of from a deckless game.
_post_setup_state: GameState | None = None
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


def _expand_synthetic_action(
    engine: GameEngine, actor: RequiredTo, action: str
) -> list[str]:
    """Translate a synthetic branch action into the concrete engine actions
    that execute it, re-derived against the engine's CURRENT state.

    The branch tree records card plays under synthetic identifiers — a
    tier-1 intent (``intent:<card_index>``) or a specific rune-payment combo
    (``shortcut:<play_action>:<card_index>:<key>``) — while the real engine
    actions live only in the (unsaved) ``chain``. So a saved game's move
    list can contain these synthetic strings, which ``apply_action`` would
    reject ("unknown action"). Because the setup + library shuffle are
    seeded, the same intent/shortcut resolves identically on replay, so we
    recompute it here and expand to the real rune+play actions.

    Plain engine actions (``play:...``) pass through unchanged."""
    if action.startswith("shortcut:"):
        rest = action[len("shortcut:") :]
        try:
            play_action, card_index_s, key = rest.split(":", 2)
            card_index = int(card_index_s)
        except ValueError as e:
            raise ValueError(f"malformed shortcut action {action!r}") from e
        for sc in compute_shortcuts(engine, actor):
            if (
                sc.play_action == play_action
                and sc.card_index == card_index
                and sc.key == key
            ):
                return _shortcut_to_actions(sc)
        raise ValueError(
            f"could not resolve {action!r} for {actor.value} in the replayed state"
        )
    # [Repeat] payment synthetics — recompute against the (seeded) replay
    # state and expand to the rune steps + play:choose_repeat:yes chain. The
    # tier-1 "intent:repeat" auto-applies its first/only combo, same as a
    # card intent.
    if action == "intent:repeat" or action.startswith("shortcut:repeat:"):
        from .shortcuts import compute_repeat_intents, execution_steps

        intents = compute_repeat_intents(engine, actor)
        combos = intents[0].combos if intents else ()
        if action == "intent:repeat":
            if not combos:
                raise ValueError("intent:repeat has no affordable combo in the replayed state")
            return [a for _, a in execution_steps(combos[0])]
        key = action[len("shortcut:repeat:") :]
        for c in combos:
            if c.key == key:
                return [a for _, a in execution_steps(c)]
        raise ValueError(f"could not resolve {action!r} for {actor.value} in the replayed state")
    if action.startswith("intent:"):
        try:
            card_index = int(action[len("intent:") :])
        except ValueError as e:
            raise ValueError(f"malformed intent action {action!r}") from e
        for intent in compute_play_intents(engine, actor):
            if intent.card_index == card_index:
                if not intent.combos:
                    raise ValueError(
                        f"intent {action!r} has no affordable combo in the replayed state"
                    )
                # The client auto-applies the first combo for a tier-1 click.
                return _shortcut_to_actions(intent.combos[0])
        raise ValueError(
            f"could not resolve {action!r} for {actor.value} in the replayed state"
        )
    return [action]


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
#: branch-tree path/states from the exact moves it made. Receives
#: (actor_value, action, label). None ⇒ no recording.
_step_recorder: "Callable[[str, str, str], None] | None" = None


def _apply(engine: GameEngine, action: str, actor: RequiredTo) -> EngineOutput:
    """Apply one auto-play action AND report it to the active step recorder
    (if any), so each fast-forwarded move becomes a branch-tree node instead
    of vanishing into a single 'start' node.

    The human label is derived from the PRE-action state (the state the action
    is an option in), matching how the interactive client labels a live option,
    so fast-forwarded nodes read identically to hand-played ones."""
    label = label_for_action(engine._game_state, actor, action) if _step_recorder is not None else action
    out = engine.apply_action(action=action, actor=actor)
    if _step_recorder is not None:
        _step_recorder(actor.value, action, label)
    return out


def _resolve_pending_showdown(engine: GameEngine, output: EngineOutput) -> EngineOutput:
    """Both players pass any pending showdown.

    Advanced auto-play is non-interactive: we don't play "showdown spells"
    or muster extra units, so the deterministic resolution is simply the
    current FOCUS holder passing focus, repeatedly, until two consecutive
    passes resolve the showdown. The engine awards the battlefield (or opens
    combat) on the second pass.
    """
    guard = 0
    while output.game_state.pending_showdown is not None:
        sd = output.game_state.pending_showdown
        output = _apply(engine, "play:pass_showdown", sd.focus_holder)
        guard += 1
        if guard > 8:
            # Defensive: a healthy showdown resolves in 2 passes. Bail to
            # avoid any chance of an infinite loop.
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
            output = _apply(engine, f"{ApplyVerb.CHOOSE_DECK.value}:{deck_id}", actor)
            continue

        if step == RequiredStep.CHOOSE_FIRST_TURN:
            output = _apply(engine, f"{ApplyVerb.CHOOSE_FIRST_TURN.value}:{first_turn.value}", actor)
            continue

        if step == RequiredStep.CHOOSE_BATTLEFIELDS:
            gs = output.game_state
            # A configured battlefield that isn't in that side's deck (e.g. a
            # stale fake_fill_config left over from a different deck) makes
            # apply_action raise. Auto-fill must NEVER crash the engine over a
            # bad config — catch it and just stop auto-resolving, leaving the
            # game at the (manual) battlefield-choice step instead of failing
            # startup / every poll.
            if gs.battlefield_1 is None:
                bf = battlefields.get(RequiredTo.PLAYER_1) or ""
                if not bf:
                    break
                try:
                    output = _apply(engine, f"{ApplyVerb.CHOOSE_BATTLEFIELD_1.value}:{bf}", RequiredTo.PLAYER_1)
                except ValueError:
                    break
                continue
            if gs.battlefield_2 is None:
                bf = battlefields.get(RequiredTo.PLAYER_2) or ""
                if not bf:
                    break
                try:
                    output = _apply(engine, f"{ApplyVerb.CHOOSE_BATTLEFIELD_2.value}:{bf}", RequiredTo.PLAYER_2)
                except ValueError:
                    break
                continue
            break

        if step == RequiredStep.CHOOSE_MULLIGAN:
            gs = output.game_state
            bottom = mulligan_bottom.strip()
            if not gs.mulligan_player_1_resolved:
                output = _apply(
                    engine, f"{ApplyVerb.MULLIGAN_RESOLVE.value}:{RequiredTo.PLAYER_1.value}:{bottom}", RequiredTo.PLAYER_1
                )
                continue
            if not gs.mulligan_player_2_resolved:
                output = _apply(
                    engine, f"{ApplyVerb.MULLIGAN_RESOLVE.value}:{RequiredTo.PLAYER_2.value}:{bottom}", RequiredTo.PLAYER_2
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


def _capture_current_save() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Snapshot the CURRENT position as a saved-game payload: the fake-fill
    setup (plus the seed that produced the current shuffle, so loading
    re-deals the exact same cards even in `early` mode) and one move per real
    (non-intent-only) branch node. Each move keeps the display action/label
    AND the `chain` of raw engine actions — the latter is what replay applies,
    since `action` may be a synthetic UI string. Caller must hold
    ``_engine_lock``."""
    setup = {
        k: v for k, v in _serialize_fake_fill(get_fake_fill_config())["config"].items()
    }
    if _last_shuffle_seed is not None:
        setup["shuffle_seed"] = _last_shuffle_seed
    moves = [
        {
            "actor": s["actor"],
            "action": s["action"],
            "label": s.get("label", ""),
            "chain": s.get("chain") or [{"actor": s["actor"], "action": s["action"]}],
        }
        for s in _branch_path
        # Skip the auto-resolved setup nodes (deck / first-turn / battlefield /
        # mulligan): the saved `setup` block already reproduces them via the
        # fake-fill config on load, so replaying them too would double-apply.
        if not s.get("intent_only") and not s.get("setup")
    ]
    return setup, moves


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


def reset_engine(
    replay_moves: list[dict[str, Any]] | None = None,
    seed_override: int | None = None,
) -> EngineOutput:
    """Recreate the engine, run start(), then apply fake-fill auto-pilot.

    In ADVANCED mode the engine is constructed with a SEEDED RNG so the
    library and rune-library shuffles produce the same order every time —
    this is what lets the advanced auto-play script's hardcoded moves stay
    valid across resets. EARLY mode (and the case when fake-fill is
    disabled entirely) still deals a fresh shuffle on every reset, but the
    shuffle is now drawn through a freshly-generated RECORDED seed
    (``_last_shuffle_seed``) so "save game" can capture the deal and a later
    load reproduces it exactly instead of replaying onto a different hand.

    ``replay_moves`` (used when LOADING a saved game) is an explicit list of
    ``{actor, action}`` dicts to apply on top of the setup baseline. When
    given it fully defines the position, so the advanced auto fast-forward is
    skipped — otherwise an advanced saved game would double-apply its opening.

    ``seed_override`` (also used when loading a saved game) forces the shuffle
    seed regardless of mode — this is what makes loading an `early`-mode save
    deterministic.
    """
    global _engine, _last_output, _initial_state, _branch_states, _last_shuffle_seed
    global _branch_path, _branch_visited, _branch_nodes_visited, _step_recorder, _post_setup_state
    # Re-read the card taxonomy on every reset so newly-tagged triggers/effects
    # take effect just by setting up a fresh game — no engine restart needed
    # (the taxonomy is otherwise memoized for the life of the process).
    _reset_ability_caches()
    cfg = get_fake_fill_config()
    if seed_override is not None:
        seed = seed_override
    elif cfg.enabled and cfg.mode == FakeFillMode.ADVANCED:
        seed = cfg.advanced_seed
    else:
        seed = random.randrange(2**32)
    _last_shuffle_seed = seed
    _engine = GameEngine(rng=random.Random(seed))
    _last_output = _engine.start()
    # Branch-tree ROOT = the truly EMPTY pre-setup state (before any deck /
    # first-turn / battlefield / mulligan choice). /branch/back to an empty path
    # restores this, so the setup decisions below are rewindable nodes IN FRONT
    # of the root rather than being collapsed away.
    _initial_state = _capture_state()
    _reset_branch_tree()
    _branch_states = []

    # Auto-resolve setup, but RECORD each choice as a branch node (flagged
    # `setup`) with its post-choice snapshot — routed through `_apply`, which
    # reports to the active step recorder. The live tip ends at the start of the
    # action turn, so a reset still LANDS there; you can simply rewind behind it.
    _setup_path: list[BranchStep] = []
    _setup_states: list[GameState] = []

    def _rec_setup(actor_val: str, action: str, label: str) -> None:
        _setup_path.append(
            {
                "actor": actor_val,
                "action": action,
                "label": label,
                "intent_only": False,
                "shortcut": None,
                "intent": None,
                "setup": True,
            }
        )
        _setup_states.append(_capture_state())

    _step_recorder = _rec_setup
    try:
        _last_output = _auto_fake_fill(_engine, _last_output)
    finally:
        _step_recorder = None
    _branch_path = _setup_path
    _branch_states = _setup_states
    _branch_nodes_visited = len(_setup_path)
    for i in range(1, len(_setup_path) + 1):
        _branch_visited.add(_branch_path_key(_setup_path[:i]))
    # The action-turn baseline (setup done, nothing played) — the clean root that
    # /branch/search generates "reach a state" examples from.
    _post_setup_state = _capture_state()

    if replay_moves:
        # Loaded saved game with an explicit move list: replay exactly those,
        # recording each as a branch node. Do NOT also auto fast-forward.
        _last_output = _replay_moves_as_branch(_last_output, replay_moves)
    elif (
        # ADVANCED: fast-forward the opening moves ONCE, recording each as a
        # branch node so the tree reflects the jumped-forward position (and the
        # moves are rewindable). EARLY mode lands at the action turn with an
        # empty path, as before.
        cfg.enabled
        and cfg.mode == FakeFillMode.ADVANCED
        and _last_output.required_action is not None
        and _last_output.required_action.name == RequiredStep.ACTION_TURN
    ):
        _last_output = _record_advanced_fast_forward(_last_output)
    return _last_output


def _replay_moves_as_branch(
    output: EngineOutput, moves: list[dict[str, Any]]
) -> EngineOutput:
    """Apply an explicit ``{actor, action}`` move list on top of the current
    baseline, recording each as a branch-tree node with a computed label (the
    same machinery the advanced fast-forward uses). Raises ValueError if a move
    is illegal in the replayed state (e.g. the saved game no longer matches the
    decks)."""
    global _step_recorder, _branch_path, _branch_states, _branch_nodes_visited
    path: list[BranchStep] = []
    states: list[GameState] = []

    def rec(actor_val: str, action: str, label: str, applied: list[dict[str, str]]) -> None:
        path.append(
            {
                "actor": actor_val,
                "action": action,
                "label": label,
                "intent_only": False,
                "shortcut": None,
                "intent": None,
                # The CONCRETE engine actions this node applied. Storing them
                # means re-saving a loaded game captures real actions again
                # (rather than the synthetic action), so save→load→save→load
                # stays stable.
                "chain": applied,
            }
        )
        states.append(_capture_state())

    try:
        for mv in moves:
            actor = RequiredTo(mv["actor"])
            action = mv["action"]
            label = mv.get("label") or action
            chain = mv.get("chain")
            applied: list[dict[str, str]] = []
            if chain:
                # Newer saves persist the real engine actions directly — apply
                # them verbatim (most robust; no re-derivation needed).
                for c in chain:
                    output = _engine.apply_action(
                        action=c["action"], actor=RequiredTo(c["actor"])
                    )
                    applied.append({"actor": c["actor"], "action": c["action"]})
            else:
                # Legacy save (no stored chain): the move may be a synthetic
                # intent/shortcut, so re-derive the concrete engine actions
                # against the current (seeded) replay state.
                for engine_action in _expand_synthetic_action(_engine, actor, action):
                    output = _engine.apply_action(action=engine_action, actor=actor)
                    applied.append({"actor": actor.value, "action": engine_action})
            # Record ONE branch node for the whole move (matching how it was
            # recorded when first played).
            rec(actor.value, action, label, applied)
    finally:
        _step_recorder = None

    # Append AFTER the setup nodes recorded at reset, so the path reads
    # setup → gameplay and rewinding walks back through both.
    _branch_path = [*_branch_path, *path]
    _branch_states = [*_branch_states, *states]
    _branch_nodes_visited += len(path)
    for i in range(1, len(_branch_path) + 1):
        _branch_visited.add(_branch_path_key(_branch_path[:i]))
    return output


def _record_advanced_fast_forward(output: EngineOutput) -> EngineOutput:
    """Run the advanced gameplay fast-forward ONCE, recording every move as a
    branch-tree node (with the post-move state captured) so the tree shows
    the jumped-forward position and each move can be rewound to."""
    global _step_recorder, _branch_path, _branch_states, _branch_nodes_visited
    path: list[BranchStep] = []
    states: list[GameState] = []

    def rec(actor_val: str, action: str, label: str) -> None:
        path.append(
            {
                "actor": actor_val,
                "action": action,
                "label": label,
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

    # Append AFTER the setup nodes recorded at reset, so the path reads
    # setup → gameplay and rewinding walks back through both.
    _branch_path = [*_branch_path, *path]
    _branch_states = [*_branch_states, *states]
    _branch_nodes_visited += len(path)
    for i in range(1, len(_branch_path) + 1):
        _branch_visited.add(_branch_path_key(_branch_path[:i]))
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


def _gear_attached_ref(gs: GameState, gear) -> str | None:
    """The host unit's CURRENT ``"controller:index"`` for a gear, resolved from
    its stable ``attached_uid`` — or None if unequipped / the host has left
    play. Keeps the UI's attachment display correct after units shift indices."""
    uid = getattr(gear, "attached_uid", None)
    if not uid:
        return None
    for controller, units in (("player_1", gs.player_1_units), ("player_2", gs.player_2_units)):
        for i, u in enumerate(units):
            if getattr(u, "uid", 0) == uid:
                return f"{controller}:{i}"
    return None


def _serialize_state(gs: GameState) -> dict[str, Any]:
    return {
        "counter": gs.counter,
        "action_log": [
            {"sequence": e.sequence, "actor": e.actor, "action": e.action}
            for e in gs.action_log
        ],
        "event_feed": [
            {
                "sequence": e.sequence,
                "kind": e.kind,
                "text": e.text,
                "code": e.code,
                "card": e.card,
            }
            for e in gs.event_feed
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
        # Each player's Legend — a single fixed card that sits beside their
        # rune base (it never enters the deck/hand). Sourced from the
        # selected deck; null until decks are chosen.
        "player_1_legend": gs.player_1_deck.legend if gs.player_1_deck else None,
        "player_2_legend": gs.player_2_deck.legend if gs.player_2_deck else None,
        # Chosen champion: the fixed Unit beside the board. `available` is True
        # until it's been played onto the board.
        "player_1_champion": gs.player_1_deck.chosen_champion if gs.player_1_deck else None,
        "player_2_champion": gs.player_2_deck.chosen_champion if gs.player_2_deck else None,
        "player_1_champion_available": (
            gs.player_1_deck is not None and not gs.player_1_champion_played
        ),
        "player_2_champion_available": (
            gs.player_2_deck is not None and not gs.player_2_champion_played
        ),
        "player_1_hand": list(gs.player_1_hand) if gs.player_1_hand is not None else None,
        "player_2_hand": list(gs.player_2_hand) if gs.player_2_hand is not None else None,
        "player_1_hand_costs": _serialize_hand_costs(gs.player_1_hand),
        "player_2_hand_costs": _serialize_hand_costs(gs.player_2_hand),
        "player_1_units": [
            {
                "card": u.card,
                "location": u.location,
                "exhausted": u.exhausted,
                "bonus_might": u.bonus_might,
                # Whether the unit carries a [Buff] (drives the buff overlay in
                # the UI and the +1 below).
                "buffed": u.buffed,
                # CURRENT Might shown on the board = printed + buffs + [Buff] +1
                # + Might granted by EFFECT-TEXT equipment attached to this unit.
                "effective_might": (card_might_of(u.card) or 0)
                + u.bonus_might
                + (1 if u.buffed else 0)
                + attached_might_bonus(gs.player_1_gears, f"player_1:{i}", gs.total_turn_number),
                "token": u.token,
                # Marked damage (rules 142/143.3); the unit dies at cleanup once
                # this is ≥ its effective Might. Surfaced so the UI can show it.
                "damage": u.damage,
            }
            for i, u in enumerate(gs.player_1_units)
        ],
        "player_2_units": [
            {
                "card": u.card,
                "location": u.location,
                "exhausted": u.exhausted,
                "bonus_might": u.bonus_might,
                "buffed": u.buffed,
                "effective_might": (card_might_of(u.card) or 0)
                + u.bonus_might
                + (1 if u.buffed else 0)
                + attached_might_bonus(gs.player_2_gears, f"player_2:{i}", gs.total_turn_number),
                "token": u.token,
                "damage": u.damage,
            }
            for i, u in enumerate(gs.player_2_units)
        ],
        "player_1_spells": [
            {
                "card": s.card,
                "targets": list(s.targets),
                "order": s.order,
                "repeat_count": len(s.repeat_targets),
            }
            for s in gs.player_1_spells
        ],
        "player_2_spells": [
            {
                "card": s.card,
                "targets": list(s.targets),
                "order": s.order,
                "repeat_count": len(s.repeat_targets),
            }
            for s in gs.player_2_spells
        ],
        "player_1_gears": [
            {
                "card": g.card,
                "location": g.location,
                "exhausted": g.exhausted,
                # Recomputed from the stable uid so it's the host's CURRENT
                # position (or None if the host has left play).
                "attached_to": _gear_attached_ref(gs, g),
                "token": g.token,
            }
            for g in gs.player_1_gears
        ],
        "player_2_gears": [
            {
                "card": g.card,
                "location": g.location,
                "exhausted": g.exhausted,
                "attached_to": _gear_attached_ref(gs, g),
                "token": g.token,
            }
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
        "pending_effect_choice": (
            None
            if gs.pending_effect_choice is None
            else {
                "actor": gs.pending_effect_choice.actor.value,
                "code": gs.pending_effect_choice.code,
                "source": gs.pending_effect_choice.source,
                "source_card": gs.pending_effect_choice.source_card,
                "options": list(gs.pending_effect_choice.options),
                "label": gs.pending_effect_choice.label,
            }
        ),
        "pending_spell_repeat": (
            None
            if gs.pending_spell_repeat is None
            else {
                "actor": gs.pending_spell_repeat.actor.value,
                "card": gs.pending_spell_repeat.card,
                "cost": {
                    "energy": int(gs.pending_spell_repeat.cost.get("energy", 0)),
                    "power": dict(gs.pending_spell_repeat.cost.get("power", {})),
                    "any_power": int(gs.pending_spell_repeat.cost.get("any_power", 0)),
                },
                # How many times the effect will have happened so far (base +
                # repeats already paid). Lets the UI show "Repeat (x2)?".
                "rounds": len(gs.pending_spell_repeat.rounds),
            }
        ),
        "pending_deflect": (
            None
            if getattr(gs, "pending_deflect", None) is None
            else {
                "actor": gs.pending_deflect.actor.value,
                # The target currently awaiting a pay-or-drop decision, plus how
                # many more decisions remain — lets the UI show "Pay 2 Power to
                # target <card>? (1 of N)".
                "card": gs.pending_deflect.queue[0][2] if gs.pending_deflect.queue else None,
                "stacks": gs.pending_deflect.queue[0][1] if gs.pending_deflect.queue else 0,
                "remaining": len(gs.pending_deflect.queue),
            }
        ),
        "pending_accelerate": (
            None
            if gs.pending_accelerate is None
            else {
                "actor": gs.pending_accelerate.actor.value,
                "card": gs.pending_accelerate.card,
                "unit_index": gs.pending_accelerate.unit_index,
                "cost": {
                    "energy": int(gs.pending_accelerate.cost.get("energy", 0)),
                    "power": dict(gs.pending_accelerate.cost.get("power", {})),
                    "any_power": int(gs.pending_accelerate.cost.get("any_power", 0)),
                },
            }
        ),
        "pending_ability_cost": (
            None
            if getattr(gs, "pending_ability_cost", None) is None
            else {
                "actor": gs.pending_ability_cost.actor.value,
                "card": gs.pending_ability_cost.source_card,
                "label": gs.pending_ability_cost.label,
                "costs": list(gs.pending_ability_cost.costs),
            }
        ),
        "pending_ability_payment": (
            None
            if getattr(gs, "pending_ability_payment", None) is None
            else {
                "actor": gs.pending_ability_payment.actor.value,
                "remaining": gs.pending_ability_payment.remaining,
                "label": gs.pending_ability_payment.label,
            }
        ),
        "pending_chain": (
            None
            if gs.pending_chain is None
            else {
                "priority": gs.pending_chain.priority.value,
                "consecutive_passes": gs.pending_chain.consecutive_passes,
                "items": [
                    {
                        "actor": it.actor.value,
                        "card": it.card,
                        "targets": list(it.targets),
                        "label": it.label,
                        # A triggered-ability item carries its effect codes; a
                        # cast-spell item leaves this null.
                        "effect": (
                            None
                            if it.effect is None
                            else {
                                "controller": it.effect.controller,
                                "source": it.effect.source,
                                "trigger": it.effect.trigger,
                                "event_kind": it.effect.event_kind,
                                "effects": list(it.effect.effects),
                                # Card the triggering event was about (e.g. the
                                # resolved spell for ON_PLAY_SPELL) — the UI
                                # underlines "spell" and hover-previews it.
                                "context_card": it.effect.context_card,
                            }
                        ),
                    }
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
                "focus": gs.pending_showdown.focus_holder.value,
                "focus_passes": gs.pending_showdown.focus_passes,
                "locked": gs.pending_showdown.locked,
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
        # The point total needed to win, and the winner once the game is decided
        # (null while it's still going).
        "victory_score": VICTORY_SCORE,
        "winner": gs.winner,
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
    # Equip intents (pre-costed chips for attaching board Equipment to a unit)
    # are appended after the hand-play intents — same chip shape, so the client
    # renders them with no changes.
    p1_intents = [
        serialize_play_intent(i)
        for i in (
            *compute_play_intents(_engine, RequiredTo.PLAYER_1),
            *compute_move_intents(_engine, RequiredTo.PLAYER_1),
            *compute_equip_intents(_engine, RequiredTo.PLAYER_1),
            *compute_quick_draw_intents(_engine, RequiredTo.PLAYER_1),
            *compute_repeat_intents(_engine, RequiredTo.PLAYER_1),
            *compute_accelerate_intents(_engine, RequiredTo.PLAYER_1),
        )
    ]
    p2_intents = [
        serialize_play_intent(i)
        for i in (
            *compute_play_intents(_engine, RequiredTo.PLAYER_2),
            *compute_move_intents(_engine, RequiredTo.PLAYER_2),
            *compute_equip_intents(_engine, RequiredTo.PLAYER_2),
            *compute_quick_draw_intents(_engine, RequiredTo.PLAYER_2),
            *compute_repeat_intents(_engine, RequiredTo.PLAYER_2),
            *compute_accelerate_intents(_engine, RequiredTo.PLAYER_2),
        )
    ]
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


# Setup steps that reset_engine auto-resolves (and records as rewindable nodes).
# When the engine is parked on one of these — i.e. the user/agent deliberately
# rewound BEHIND the setup nodes — get_snapshot must NOT re-resolve it, or every
# poll would snap the game forward again and the rewind wouldn't hold.
_SETUP_STEPS = frozenset(
    {
        RequiredStep.CHOOSE_DECK,
        RequiredStep.CHOOSE_FIRST_TURN,
        RequiredStep.CHOOSE_BATTLEFIELDS,
        RequiredStep.CHOOSE_MULLIGAN,
    }
)


def get_snapshot() -> dict[str, Any]:
    """Always run `start()` so ABCD auto-completion and any logic upgrades apply to every poll."""
    with _engine_lock:
        global _last_output
        _last_output = _engine.start()
        # Setup is resolved once in reset_engine (and recorded as branch nodes).
        # Skip the per-poll auto-fill while parked on a setup step so a rewind to
        # "before the mulligan/battlefield" stays put instead of re-resolving.
        ra = _last_output.required_action
        if ra is None or ra.name not in _SETUP_STEPS:
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
        # Identity of the CURRENT shuffle. Changes whenever the engine is
        # re-seeded (server restart, /reset, loading a different save). The
        # client records this when a save is loaded/created so it can detect
        # that the engine re-shuffled out from under it (e.g. the dev server
        # restarted on a code change) and refuse to overwrite the save with
        # an unrelated random game.
        "shuffle_seed": _last_shuffle_seed,
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


class BranchSearchBody(BaseModel):
    """Brute-force search for a move path to a target state, then replay it
    onto the live branch. ``predicate`` is the JSON match spec evaluated by
    ``search.evaluate_predicate`` (see that module for the grammar)."""

    predicate: dict[str, Any] = Field(..., description="goal predicate (JSON match spec)")
    max_depth: int = Field(default=14, ge=1, le=160, description="max plies to explore")
    node_budget: int = Field(default=20000, ge=1, le=500000, description="max nodes to expand")
    time_budget_s: float = Field(default=20.0, gt=0, le=120, description="wall-clock cap (seconds)")
    label: str = Field(default="AI search", description="label for the single branch node")
    forward_search: bool = Field(
        default=False,
        description=(
            "When True, search FORWARD from the current live position and append "
            "the found moves to the existing branch — decisions already made "
            "(deck choice, prior moves) are never revisited, and the result "
            "persists. When False (default), search from the clean root and, if a "
            "named card isn't in the loaded decks, change decks (sideboard swap or "
            "deck switch) to bring it into reach — i.e. it may go all the way back "
            "to the deck-choice decision."
        ),
    )


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


class SaveGameBody(BaseModel):
    """Save the current setup (+ branch-path moves) as a named saved game."""

    name: str = Field(..., min_length=1, description="display name; id is a slug of it")
    description: str | None = Field(default=None, description="optional note")


class DeckSaveBody(BaseModel):
    """Save a deck file to riftbound-engine/decks/{id}.txt. Overwrites if it exists."""

    id: str = Field(..., min_length=1, max_length=80)
    text: str = Field(..., min_length=1)


class ImplementEffectBody(BaseModel):
    """A data-driven effect spec the Implementation agent authors. Written to
    generated_effects.json and registered LIVE (no restart). Currently the
    ``might_delta`` kind: add/subtract Might from matching units with a floor."""

    code: str = Field(..., min_length=1, max_length=80)
    kind: str = Field("might_delta")
    amount: int | None = Field(None)  # required for might_delta/deal_damage/draw
    min_might: int | None = Field(None)
    target: str = Field("any")  # enemy | friendly | any
    location: str = Field("any")  # here | any
    select: str = Field("all")  # all | one
    # return_from_trash family
    filter: str | None = Field(None)  # champion | unit | spell | gear | any
    destination: str | None = Field(None)  # hand | champion_zone
    optional: bool | None = Field(None)  # "may" effect
    condition: str | None = Field(None)  # champion_zone_empty | null
    card: str | None = Field(None)
    note: str | None = Field(None)


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

    @app.get("/implemented-surface")
    def implemented_surface_get() -> dict[str, Any]:
        """The engine's LIVE implemented surface (effects/triggers/conditions/
        costs/passives + pattern families). The web Implementation tracker reads
        this instead of the generated engineImplemented.ts, so it can never go
        stale — a newly-registered effect shows up immediately."""
        from .implemented_surface import surface_data

        return surface_data()

    @app.post("/reroll-until")
    def reroll_until(body: BranchSearchBody) -> dict[str, Any]:
        """Re-DEAL the opening (reset with a fresh shuffle) until ``predicate``
        holds at the turn-1 opening — the legitimate "reset until I have X in
        hand". NO moves are played and NO cards are injected: it just keeps
        dealing fresh openings until the shuffle delivers the goal, then keeps
        that deal as the live branch. Bounded by attempts + a time budget."""
        import time as _time

        from .search import evaluate_predicate, state_view

        global _last_output
        max_attempts = 800
        time_budget_s = 20.0
        with _engine_lock:
            t0 = _time.time()
            found = False
            attempts = 0
            for attempts in range(1, max_attempts + 1):
                # Force a fresh shuffle every attempt (seed_override overrides the
                # mode's fixed/advanced seed), so each reset is a new deal.
                _last_output = reset_engine(seed_override=random.randrange(2**32))
                opening = _post_setup_state if _post_setup_state is not None else _engine.game_state
                if evaluate_predicate(body.predicate, state_view(opening)):
                    found = True
                    break
                if _time.time() - t0 > time_budget_s:
                    break
            return {"found": found, "attempts": attempts, "branch": _serialize_branch()}

    @app.get("/decks")
    def decks_list() -> dict[str, Any]:
        return {"decks": _serialize_decks()}

    @app.post("/create-test-deck")
    def create_test_deck_route(body: dict) -> dict[str, Any]:
        """A playable deck that RUNS a given card (to test it on the board).
        Reuses a real deck that already includes the card; only builds a NEW
        throwaway deck when none does. Never edits an existing deck."""
        from .deck_files import deck_for_card

        card = str(body.get("card") or "").strip()
        if not card:
            raise HTTPException(status_code=400, detail="card is required")
        try:
            deck_id, source = deck_for_card(card)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "deck_id": deck_id, "source": source, "card": card}

    @app.post("/implement-effect")
    def implement_effect(body: ImplementEffectBody) -> dict[str, Any]:
        """Author/replace a data-driven effect handler and register it LIVE.
        Lets the Implementation agent actually make an effect run (currently the
        ``might_delta`` family) without a developer or a restart."""
        from . import generated_effects

        if body.kind not in (
            "might_delta",
            "deal_damage",
            "draw",
            "opponent_discard",
            "return_from_trash",
            "spend_buff_draw",
        ):
            raise HTTPException(status_code=400, detail=f"unsupported kind {body.kind!r}")
        if body.kind in ("might_delta", "deal_damage", "draw", "spend_buff_draw") and body.amount is None:
            raise HTTPException(status_code=400, detail=f"amount is required for kind {body.kind!r}")
        if body.kind in ("might_delta", "deal_damage"):
            if body.target not in ("enemy", "friendly", "any"):
                raise HTTPException(status_code=400, detail="target must be enemy|friendly|any")
            if body.location not in ("here", "any"):
                raise HTTPException(status_code=400, detail="location must be here|any")
            if body.select not in ("all", "one"):
                raise HTTPException(status_code=400, detail="select must be all|one")
        if body.kind == "return_from_trash":
            if (body.destination or "hand") not in ("hand", "champion_zone"):
                raise HTTPException(
                    status_code=400, detail="destination must be hand|champion_zone"
                )
            if (body.filter or "any") not in ("any", "champion", "unit", "spell", "gear"):
                raise HTTPException(
                    status_code=400, detail="filter must be any|champion|unit|spell|gear"
                )
        spec = {
            k: v
            for k, v in body.model_dump().items()
            if v is not None
        }
        try:
            registered = generated_effects.upsert_spec(spec)
        except Exception as exc:  # bad spec — surface it, don't 500 silently
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        from . import effects as _effects

        return {
            "ok": True,
            "code": body.code,
            "implemented": _effects.is_implemented(body.code),
            "generated_codes": registered,
        }

    @app.get("/decks/{deck_id}/text")
    def decks_text(deck_id: str) -> dict[str, Any]:
        """Raw deck-file text for one deck, so the UI can show its full
        composition (every card line) and pre-fill an edit form."""
        try:
            path = deck_file_path(deck_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"id": deck_id, "text": path.read_text(encoding="utf-8")}

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
        global _last_output
        updates = body.model_dump(exclude_unset=True)
        prev = get_fake_fill_config()
        cfg = update_fake_fill_config(updates)

        # The fake-fill MODE (and, within advanced mode, the seed) defines how
        # the game is built FROM SCRATCH — early hands control back at the
        # action turn on a fresh shuffle; advanced uses a seeded shuffle and
        # fast-forwards to a contested battlefield. There's no way to convert a
        # live mid-game into the new mode's position without rebuilding it, so
        # toggling mode used to silently do nothing until a manual reset.
        # Apply it immediately by resetting here; other views pick up the new
        # state on their next poll.
        mode_changed = "mode" in updates and cfg.mode != prev.mode
        seed_changed = (
            "advanced_seed" in updates
            and cfg.mode == FakeFillMode.ADVANCED
            and cfg.advanced_seed != prev.advanced_seed
        )
        if mode_changed or seed_changed:
            with _engine_lock:
                _last_output = reset_engine()
        return _serialize_fake_fill(cfg)

    @app.get("/saved-games")
    def saved_games_list() -> dict[str, Any]:
        return {"games": list_saved_games()}

    @app.post("/saved-games")
    def saved_games_create(body: SaveGameBody) -> dict[str, Any]:
        """Capture the CURRENT setup + the live branch path as a new saved
        game. The setup mirrors the fake-fill config; the moves are the real
        (non-intent-only) engine actions recorded in the branch tree, so
        loading the game later replays straight back to this position."""
        with _engine_lock:
            setup, moves = _capture_current_save()
        game = add_saved_game(
            name=body.name,
            description=body.description or "",
            setup=setup,
            moves=moves,
        )
        return {"games": list_saved_games(), "created": game}

    @app.post("/saved-games/{game_id}/update")
    def saved_games_update(game_id: str) -> dict[str, Any]:
        """Overwrite an existing saved game with the CURRENT setup + branch
        path (the same capture create uses). Id/name/description are kept, so
        a quick-load slot can be re-saved in place after exploring further."""
        with _engine_lock:
            setup, moves = _capture_current_save()
        game = update_saved_game(game_id, setup=setup, moves=moves)
        if game is None:
            raise HTTPException(status_code=404, detail=f"no saved game '{game_id}'")
        return {"games": list_saved_games(), "updated": game}

    @app.post("/saved-games/{game_id}/load")
    def saved_games_load(game_id: str) -> dict[str, Any]:
        """Apply a saved game's setup, reset to its baseline, then replay its
        moves (or run the mode's fast-forward when it has none). Board, branch
        tree, and Control view all follow on their next poll."""
        game = get_saved_game(game_id)
        if game is None:
            raise HTTPException(status_code=404, detail=f"no saved game '{game_id}'")
        with _engine_lock:
            global _last_output
            # Apply the saved setup to the fake-fill config so reset_engine
            # builds the right decks / seed / mode. Force enabled so the setup
            # auto-resolves rather than stalling at choose_deck.
            update_fake_fill_config({**game.get("setup", {}), "enabled": True})
            moves = game.get("moves") or None
            # Replay onto the EXACT shuffle the game was saved on. Older saves
            # without a recorded seed fall back to the mode's default RNG.
            raw_seed = game.get("setup", {}).get("shuffle_seed")
            seed_override = raw_seed if isinstance(raw_seed, int) else None
            try:
                _last_output = reset_engine(replay_moves=moves, seed_override=seed_override)
            except (ValueError, KeyError) as e:
                # A failed replay leaves the engine mid-way through the move
                # list — a misleading half-loaded position. Rebuild a clean
                # baseline (same seed, no moves) before reporting the failure,
                # so the board at least shows a coherent fresh game.
                _last_output = reset_engine(seed_override=seed_override)
                raise HTTPException(
                    status_code=400,
                    detail=f"could not replay saved game '{game_id}': {e}",
                ) from e
            return _serialize_output(_last_output)

    @app.delete("/saved-games/{game_id}")
    def saved_games_delete(game_id: str) -> dict[str, Any]:
        if not delete_saved_game(game_id):
            raise HTTPException(status_code=404, detail=f"no saved game '{game_id}'")
        return {"games": list_saved_games()}

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
                    # Don't auto-resolve the REST of setup after a manual setup
                    # step: if the user rewound to (say) battlefield 1 and picks
                    # it, leave battlefield 2 / mulligans for them to step too,
                    # instead of fast-forwarding straight past them. Mirrors the
                    # get_snapshot gate. (In normal play this is a no-op, since
                    # the result isn't a setup step.)
                    ra = _last_output.required_action
                    if ra is None or ra.name not in _SETUP_STEPS:
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
                # Persist the REAL engine actions applied (the combo chain, or
                # the single {actor, action} for a plain move). `action` above
                # is often a synthetic UI string ("intent:0", "shortcut:…") that
                # the engine can't re-apply, so saved games replay from `chain`.
                "chain": (
                    [c.model_dump() for c in body.chain]
                    if body.chain
                    else ([] if body.intent_only else [{"actor": body.actor, "action": body.action}])
                ),
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

    @app.post("/branch/search")
    def branch_search(body: BranchSearchBody) -> dict[str, Any]:
        """Brute-force the game tree (no AI) for the SHORTEST legal move path
        whose resulting state satisfies ``predicate``, starting from a CLEAN
        game (the turn-1 root, nothing played), then REPLAY that path onto a
        freshly-reset branch so the tree/board land at the target.

        Searching from the clean root (not the current live position) is
        deliberate: this is a "generate an example reaching state X" tool, so
        each call starts from a fresh game. Searching from the drifted live
        state would compound prior searches until the game ran out of legal
        moves (explored=1, frontier=0). Returns the transcript and stats."""
        global _branch_path, _branch_states, _branch_visited, _branch_nodes_visited, _last_output, _initial_state, _post_setup_state
        with _engine_lock:
            forward = body.forward_search
            if forward:
                # FORWARD search: continue from the current live position and
                # only ever go forward — decisions already made (deck choice,
                # prior branch moves) are never revisited, so deck changes are
                # off. The found path appends to the existing branch.
                start = copy.deepcopy(_engine.game_state)
                result = bfs_search(
                    start,
                    body.predicate,
                    max_depth=body.max_depth,
                    node_budget=body.node_budget,
                    time_budget_s=body.time_budget_s,
                    verbose=True,
                    allow_deck_changes=False,
                )
            else:
                # Search from the clean root. If a named card isn't in the
                # loaded decks, change decks (sideboard swap / deck switch) to
                # bring it into reach — i.e. revisit the deck-choice decision.
                # Search from the POST-SETUP turn-1 baseline (decks chosen,
                # nothing played) — NOT the empty pre-setup floor — so "X in
                # hand" just stacks the opening hand instead of grinding out the
                # whole setup + draws.
                base = (
                    _post_setup_state
                    if _post_setup_state is not None
                    else _initial_state
                    if _initial_state is not None
                    else _engine.game_state
                )
                start, deck_changes = apply_deck_changes(copy.deepcopy(base), body.predicate)
                result = bfs_search(
                    start,
                    body.predicate,
                    max_depth=body.max_depth,
                    node_budget=body.node_budget,
                    time_budget_s=body.time_budget_s,
                    verbose=True,
                    allow_deck_changes=False,
                )
                result.deck_changes = deck_changes
            steps_out: list[dict[str, Any]] = []
            # Land on found even with ZERO moves: a deck change can satisfy the
            # goal at the (new) root (e.g. the wanted card is now in hand), so
            # we still need to restore + persist that root for the live game.
            if result.found:
                if not forward:
                    # Land the live engine on the (possibly deck-changed /
                    # hand-stacked) example baseline, and PERSIST it as the
                    # search baseline for later searches.
                    _restore_state(start)
                    _post_setup_state = copy.deepcopy(start)
                    # PRESERVE the recorded setup nodes (deck / first-turn /
                    # battlefield / mulligan) so you can still rewind to those
                    # decisions after landing. Keep only the leading setup
                    # prefix (drop any prior gameplay), then, if this example
                    # adjusted the deal (deck change / stacked opening hand),
                    # record ONE setup-flagged node capturing the example
                    # baseline so the live tip is rewindable too.
                    n_setup = 0
                    for s in _branch_path:
                        if s.get("setup"):
                            n_setup += 1
                        else:
                            break
                    if n_setup == 0:
                        # No setup nodes recorded (e.g. fake-fill disabled) —
                        # fall back to a clean single-root branch.
                        _initial_state = copy.deepcopy(start)
                        _reset_branch_tree()
                        _branch_states = []
                    else:
                        _branch_path = _branch_path[:n_setup]
                        _branch_states = _branch_states[:n_setup]
                        if result.deck_changes:
                            _branch_path = [
                                *_branch_path,
                                {
                                    "actor": "both",
                                    "action": "setup_adjust",
                                    "label": "; ".join(result.deck_changes),
                                    "intent_only": False,
                                    "shortcut": None,
                                    "intent": None,
                                    "setup": True,
                                },
                            ]
                            _branch_states = [*_branch_states, copy.deepcopy(start)]
                        _branch_nodes_visited = len(_branch_path)
                # In forward mode we leave the live engine where it is and just
                # append the found moves onto the current branch.
                # Apply the found path and record each move as its OWN real
                # branch node — exactly the nodes you'd get by clicking those
                # moves yourself. The search ran silently off to the side, so
                # you never watched it step; what lands on the tree is just the
                # finished sequence of REAL game moves, with the current
                # position at the end (the target). There is intentionally NO
                # "AI"/prompt node: the goal text is not a game action and must
                # never appear on the tree. Because every step is a genuine
                # node you can walk backward/forward through the actual states.
                for move in result.moves:
                    try:
                        for actor, action in move.steps:
                            _last_output = _engine.apply_action(action=action, actor=actor)
                        _last_output = _auto_fake_fill(_engine, _last_output)
                    except ValueError as e:
                        raise HTTPException(
                            status_code=500,
                            detail=f"replay of '{move.label}' failed: {e}",
                        ) from e
                    saved = _capture_state()
                    step: BranchStep = {
                        "actor": move.actor,
                        "action": move.label,
                        "label": move.label,
                        "intent_only": False,
                        "shortcut": None,
                        "intent": None,
                        "chain": [
                            {"actor": actor.value, "action": action}
                            for actor, action in move.steps
                        ],
                    }
                    _branch_path = [*_branch_path, step]
                    _branch_states = [*_branch_states, saved]
                    steps_out.append({"actor": move.actor, "label": move.label})
                for i in range(1, len(_branch_path) + 1):
                    _branch_visited.add(_branch_path_key(_branch_path[:i]))
                _branch_nodes_visited = len(_branch_path)
            return {
                "found": result.found,
                "depth": result.depth,
                "nodes_explored": result.nodes_explored,
                "reason": result.reason,
                "deck_changes": result.deck_changes,
                "steps": steps_out,
                "branch": _serialize_branch(),
            }

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
