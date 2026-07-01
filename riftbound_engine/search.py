"""Brute-force game-tree search to reach a target state — no AI in the loop.

Given a current ``GameState`` and a machine-checkable *predicate*, this walks
the engine's legal moves breadth-first (forking the engine via deepcopy at
every node) and returns the SHORTEST move sequence whose resulting state
satisfies the predicate, within a depth + node budget.

It reuses the exact same legal moves the Branch UI offers — raw engine
``options`` plus ``compute_shortcuts`` (rune-payment + play), expanded to the
atomic ``apply_action`` calls — so any path it finds is a real, legal game.

The predicate is a small JSON match language evaluated against ``state_view``
(see ``evaluate_predicate``), so callers (including an LLM that compiles a
natural-language goal once) never have to understand engine internals.
"""

from __future__ import annotations

import copy
import heapq
import sys
import time
from dataclasses import dataclass, field


def _log(msg: str) -> None:
    """Print to stderr so it shows in the uvicorn engine terminal (always
    flushed, regardless of logging config)."""
    print(f"[search] {msg}", file=sys.stderr, flush=True)

from .deck_files import deck_data_for_id, deck_ids
from .engine import Deck, GameEngine, GameState, Rune, RequiredTo
from .action_label import label_for_action
from .shortcuts import (
    compute_equip_intents,
    compute_move_intents,
    compute_play_intents,
    compute_quick_draw_intents,
    compute_repeat_intents,
    execution_steps,
)

# A single atomic engine call: (actor, action-string).
Step = tuple[RequiredTo, str]


@dataclass
class Move:
    """One legal move = a human label + the atomic engine steps it expands to
    (a card play is several: rune exhausts + the play + any follow-up)."""

    label: str
    steps: list[Step]
    actor: str  # primary actor (for the branch step record)


@dataclass
class SearchResult:
    found: bool
    moves: list[Move] = field(default_factory=list)
    nodes_explored: int = 0
    depth: int = 0
    reason: str = ""
    #: Human-readable deck edits the search made to bring a named card into
    #: reach (sideboard swap or deck switch). Empty when nothing was changed.
    deck_changes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# State view + predicate evaluation
# --------------------------------------------------------------------------- #
def _ctrl(v) -> str | None:
    return v.value if v is not None else None


def state_view(gs: GameState) -> dict:
    """Flatten the bits of a GameState a goal predicate can match on. Field
    names here are the predicate vocabulary."""

    def units(arr):
        return [
            {
                "card": u.card,
                "location": u.location,
                "exhausted": bool(u.exhausted),
                "bonus_might": getattr(u, "bonus_might", 0),
            }
            for u in (arr or [])
        ]

    return {
        "started": gs.started,
        "total_turn_number": gs.total_turn_number,
        "current_player": gs.current_player.value,
        "player_1_score": gs.player_1_score,
        "player_2_score": gs.player_2_score,
        "battlefield_1": gs.battlefield_1,
        "battlefield_2": gs.battlefield_2,
        "battlefield_1_controller": _ctrl(gs.battlefield_1_controller),
        "battlefield_2_controller": _ctrl(gs.battlefield_2_controller),
        "player_1_units": units(gs.player_1_units),
        "player_2_units": units(gs.player_2_units),
        "player_1_hand_count": len(gs.player_1_hand or []),
        "player_2_hand_count": len(gs.player_2_hand or []),
        "player_1_hand": list(gs.player_1_hand or []),
        "player_2_hand": list(gs.player_2_hand or []),
        # Library order (draw pile, top first) — lets the heuristic know how
        # many end-turns/draws away a needed card is, so the search will
        # deliberately end turns to draw toward it.
        "player_1_library": list(gs.player_1_library or []),
        "player_2_library": list(gs.player_2_library or []),
        "player_1_spells": [s.card for s in gs.player_1_spells],
        "player_2_spells": [s.card for s in gs.player_2_spells],
        "player_1_trash": list(gs.player_1_trash),
        "player_2_trash": list(gs.player_2_trash),
    }


#: Forgiving aliases for field names a goal-compiler is likely to invent, so a
#: near-miss ("turn") still resolves instead of silently evaluating to None
#: (which would make the clause un-satisfiable and the whole search fail).
_FIELD_ALIASES = {
    "turn": "total_turn_number",
    "turn_number": "total_turn_number",
    "total_turns": "total_turn_number",
    "active_player": "current_player",
    "p1_score": "player_1_score",
    "p2_score": "player_2_score",
    "bf1_controller": "battlefield_1_controller",
    "bf2_controller": "battlefield_2_controller",
}


def _get_path(view: dict, path: str):
    parts = path.split(".")
    if parts:
        parts[0] = _FIELD_ALIASES.get(parts[0], parts[0])
    cur = view
    for part in parts:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _cmp(op: str, left, right) -> bool:
    try:
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == "in":
            return left in right
        if op == "contains":
            return right in (left or [])
    except TypeError:
        return False
    return False


def _norm_name(s: str) -> str:
    """Normalize a card name for tolerant matching: lowercase and drop all
    punctuation (commas, apostrophes, hyphens, etc.) so "Irelia Fervent"
    matches the actual card "Irelia, Fervent". Collapses runs of whitespace."""
    out = []
    for ch in str(s).lower():
        out.append(ch if ch.isalnum() else " ")
    return " ".join("".join(out).split())


# A units clause's ``at`` can name a NON-board zone — a card-name list rather
# than in-play units. These map to the matching ``state_view`` list field.
_NONBOARD_ZONE: dict[str, str] = {
    "hand": "hand",
    "library": "library",
    "deck": "library",
    "trash": "trash",
    "discard": "trash",
}


def _zone_sides(controller) -> list[str]:
    return (
        [controller]
        if controller in ("player_1", "player_2")
        else ["player_1", "player_2"]
    )


def _match_zone_cards(view: dict, controller, zone: str, spec: dict) -> bool:
    """Count cards (by name) in a non-board zone list — ``{side}_hand`` /
    ``_library`` / ``_trash`` — and check the count against min/max. Cards in
    these zones aren't ``PlayedUnit``s with a board location, so they need
    name-list matching rather than the in-play unit scan."""
    name = spec.get("name")
    count = 0
    for s in _zone_sides(controller):
        for card in view.get(f"{s}_{zone}", []) or []:
            if name is not None and _norm_name(name) not in _norm_name(card):
                continue
            count += 1
    lo = spec.get("min", 1)
    hi = spec.get("max")
    return count >= lo and (hi is None or count <= hi)


def _match_units(view: dict, spec: dict) -> bool:
    """Count units matching {controller, at, name, exhausted} and check the
    count against min (default 1) / max (default None = no upper bound)."""
    # No controller specified ⇒ EITHER player (search both pools). Only narrow
    # to one side when the clause explicitly names player_1 / player_2.
    controller = spec.get("controller")
    # A non-board ``at`` (hand / library / trash) matches against that zone's
    # card-name list instead of in-play units.
    _zone = _NONBOARD_ZONE.get(spec.get("at")) if isinstance(spec.get("at"), str) else None
    if _zone is not None:
        return _match_zone_cards(view, controller, _zone, spec)
    key = f"{controller}_units" if controller in ("player_1", "player_2") else None
    pools = (
        [view.get("player_1_units", []), view.get("player_2_units", [])]
        if key is None
        else [view.get(key, [])]
    )
    at = spec.get("at")
    # "at" may be a single location, a list of locations, or the shorthand
    # "battlefield"/"any_battlefield" meaning either battlefield (so "a poro
    # at a battlefield" is one clause instead of an OR the compiler must nest).
    if at in ("battlefield", "any_battlefield"):
        allowed_at = {"battlefield_1", "battlefield_2"}
    elif isinstance(at, list):
        allowed_at = set(at)
    elif at is not None:
        allowed_at = {at}
    else:
        allowed_at = None
    name = spec.get("name")
    exhausted = spec.get("exhausted")
    count = 0
    for pool in pools:
        for u in pool:
            if allowed_at is not None and u.get("location") not in allowed_at:
                continue
            if name is not None and _norm_name(name) not in _norm_name(u.get("card", "")):
                continue
            if exhausted is not None and bool(u.get("exhausted")) != bool(exhausted):
                continue
            count += 1
    lo = spec.get("min", 1)
    hi = spec.get("max")
    return count >= lo and (hi is None or count <= hi)


def evaluate_predicate(pred: dict, view: dict) -> bool:
    """Evaluate a JSON predicate against a ``state_view``.

    Grammar (any node):
      {"all": [pred, ...]}                    — AND (also the empty-dict = True)
      {"any": [pred, ...]}                    — OR
      {"not": pred}
      {"field": "<dotpath>", "op": "==|!=|>|>=|<|<=|in|contains", "value": X}
      {"units": {"controller","at","name","exhausted","min","max"}}
    Unknown shapes evaluate to False (fail safe)."""
    if not isinstance(pred, dict):
        return False
    if not pred:  # empty {} ⇒ no constraints ⇒ True
        return True
    if "all" in pred:
        return all(evaluate_predicate(p, view) for p in pred["all"])
    if "any" in pred:
        return any(evaluate_predicate(p, view) for p in pred["any"])
    if "not" in pred:
        return not evaluate_predicate(pred["not"], view)
    if "units" in pred:
        return _match_units(view, pred["units"] or {})
    if "field" in pred and "op" in pred:
        return _cmp(pred["op"], _get_path(view, pred["field"]), pred.get("value"))
    return False


def goal_distance(pred: dict, view: dict) -> float:
    """Heuristic: how FAR a state is from satisfying ``pred`` (0 ⇒ satisfied).
    Drives the best-first search toward states that fulfil more of the goal.

      all ⇒ sum of children      any ⇒ min of children
      units leaf ⇒ 0 if already in play, 0.5 if a matching card is in the
        relevant HAND (one play away — guides the search to advance that
        player's turn then play it), else 1.
      other leaf / not ⇒ 0 if satisfied else 1
    """
    if not isinstance(pred, dict) or not pred:
        return 0.0
    if "all" in pred:
        return sum(goal_distance(p, view) for p in pred["all"])
    if "any" in pred:
        ds = [goal_distance(p, view) for p in pred["any"]]
        return min(ds) if ds else 0.0
    if "units" in pred:
        return _units_distance(pred, view)
    # A turn-number goal gets a gradient = turns remaining, so the search
    # deliberately ends turns toward the target instead of treating every
    # unsatisfied state as equidistant (flat 1.0), which made "reach turn N"
    # exhaust the budget. Only applies to total_turn_number with >, >=, ==.
    if "field" in pred and "op" in pred:
        field = _FIELD_ALIASES.get(pred.get("field"), pred.get("field"))
        if field == "total_turn_number" and pred.get("op") in (">", ">=", "=="):
            try:
                cur = int(_get_path(view, pred["field"]) or 0)
                target = int(pred.get("value"))
            except (TypeError, ValueError):
                return 0.0 if evaluate_predicate(pred, view) else 1.0
            if pred["op"] == ">":
                target += 1
            return float(max(0, target - cur))
    return 0.0 if evaluate_predicate(pred, view) else 1.0


def _units_distance(pred: dict, view: dict) -> float:
    """Distance for a units clause — the gradient that makes 'end turn until
    you draw it, then play it' fall out naturally:
        0.0  fully satisfied (right card, right place/count)
        0.5  a matching card is already IN PLAY (just needs positioning/another)
        1.0  in the relevant HAND (one play away)
        1.5+ in the LIBRARY, +0.05 per card from the top (so drawing toward it
             strictly lowers the distance ⇒ the search ends turns to reach it)
        5.0  not in the deck at all (effectively unreachable)
    """
    if evaluate_predicate(pred, view):
        return 0.0
    spec = pred.get("units") or {}
    name = _norm_name(spec.get("name") or "")
    controller = spec.get("controller")
    sides = [controller] if controller in ("player_1", "player_2") else ["player_1", "player_2"]

    def matches(card: object) -> bool:
        return (not name) or name in _norm_name(card)

    # Non-board zone goal (e.g. "X in hand"): the gradient is the OPPOSITE of a
    # board goal — keeping/drawing the card into the zone is the win, not
    # playing it out. For a HAND goal, draw it from the library (ending turns
    # lowers the distance); a card already IN PLAY would need a bounce, so it's
    # FAR (this also stops the search from "helpfully" playing a card the goal
    # wants kept in hand).
    zone = _NONBOARD_ZONE.get(spec.get("at")) if isinstance(spec.get("at"), str) else None
    if zone == "hand":
        best = None
        for s in sides:
            for i, card in enumerate(view.get(f"{s}_library", []) or []):
                if matches(card):
                    best = i if best is None else min(best, i)
                    break
        if best is not None:
            return 1.0 + best * 0.05
        for s in sides:
            if any(matches(u.get("card")) for u in view.get(f"{s}_units", [])):
                return 3.0
        return 5.0
    if zone in ("library", "trash"):
        return 2.0

    for s in sides:  # already in play somewhere?
        if any(matches(u.get("card")) for u in view.get(f"{s}_units", [])):
            return 0.5
    for s in sides:  # in hand (one play away)?
        if any(matches(c) for c in view.get(f"{s}_hand", [])):
            return 1.0
    best = None  # how deep in the library?
    for s in sides:
        for i, card in enumerate(view.get(f"{s}_library", [])):
            if matches(card):
                best = i if best is None else min(best, i)
                break
    return 1.5 + best * 0.05 if best is not None else 5.0


# --------------------------------------------------------------------------- #
# Deck changes — bring a NAMED card into reach when the loaded decks can't.
#
# A goal like "play Disarming Rake" is unreachable if the card isn't in the
# library or on the board (goal_distance pins at 5.0 and the search exhausts).
# When deck changes are allowed, we make each named card reachable by, in order
# of least disruption:
#   1. nothing — it's already in that side's hand/library,
#   2. SIDEBOARD swap — the card sits in the CURRENT deck's sideboard, so swap
#      it into the main deck (preserving the chosen deck), or
#   3. DECK SWITCH — no loaded deck on that side has it, so switch that side to
#      a deck that lists it (preferring one with it in the MAIN deck, else
#      sideboarding it in from the switched-to deck).
# The swapped-in card is placed on TOP of that side's library so the example is
# a short, real line (the player just draws it), not a 38-turn deck-dig.
# --------------------------------------------------------------------------- #
#: Opening-hand size used when a deck change rebuilds a side's opening (matches
#: the engine's mulligan draw count, so the rebuilt hand looks like a real one).
from .engine import MULLIGAN_DRAW_COUNT as _OPENING_HAND


def _named_targets(pred: dict) -> list[tuple[str, str]]:
    """(name_fragment, side) for every units clause that NAMES a card. ``side``
    is the clause's controller, or ``"player_1"`` when unscoped (the requester).
    De-duplicated, preserving order."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for spec in predicate_unit_specs(pred):
        name = spec.get("name")
        if not name:
            continue
        ctrl = spec.get("controller")
        side = ctrl if ctrl in ("player_1", "player_2") else "player_1"
        key = (_norm_name(name), side)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _find_named(cards, frag: str) -> str | None:
    """The first actual card name in ``cards`` matching name-fragment ``frag``."""
    for c in cards or []:
        if frag in _norm_name(c):
            return c
    return None


def _swap_into_main(main: list[str], card: str) -> tuple[str, ...]:
    """A 39-card main deck containing ``card``: drop one copy of the most-
    duplicated existing card (so a singleton tech card isn't lost) and add
    ``card``. Keeps the deck-size and ≤3-copies invariants."""
    new = list(main)
    from collections import Counter

    counts = Counter(new)
    # Prefer removing a card we have multiple of; fall back to the last card.
    drop = max(new, key=lambda c: (counts[c], new.index(c))) if new else None
    if drop is not None:
        new.remove(drop)
    new.append(card)
    return tuple(new)


def _deck_object(deck_id: str, cards: tuple[str, ...]) -> Deck:
    """Build a Deck for ``deck_id`` but with its main deck replaced by ``cards``
    (battlefields / champion / legend / runes taken from the deck file)."""
    dd = deck_data_for_id(deck_id)
    return Deck(
        battlefields=list(dd["battlefields"]),
        chosen_champion=str(dd["chosen_champion"]),
        legend=str(dd["legend"]),
        cards=cards,
        runes=tuple(Rune(domain=str(r["domain"])) for r in dd["runes"]),
    )


def _provider_for(frag: str, current_deck_id: str | None) -> tuple[str, tuple[str, ...], str] | None:
    """Find a deck that can supply a card matching ``frag``. Returns
    ``(deck_id, main_cards_including_it, how)`` where ``how`` is "sideboard" or
    "main", or ``None`` if no loaded deck lists it.

    Preference order: the CURRENT deck's sideboard (keep the chosen deck) →
    another deck with it in its MAIN → another deck with it in its sideboard."""
    # 1. current deck's sideboard
    if current_deck_id:
        try:
            dd = deck_data_for_id(current_deck_id)
        except Exception:
            dd = None
        if dd is not None:
            hit = _find_named(dd.get("sideboard") or [], frag)
            if hit and _find_named(dd.get("cards") or [], frag) is None:
                return current_deck_id, _swap_into_main(list(dd["cards"]), hit), "sideboard"
    # 2. another deck with it in MAIN, then 3. another deck's sideboard
    sideboard_fallback: tuple[str, tuple[str, ...], str] | None = None
    for did in deck_ids():
        if did == current_deck_id:
            continue
        try:
            dd = deck_data_for_id(did)
        except Exception:
            continue
        main_hit = _find_named(dd.get("cards") or [], frag)
        if main_hit:
            return did, tuple(dd["cards"]), "main"
        if sideboard_fallback is None:
            side_hit = _find_named(dd.get("sideboard") or [], frag)
            if side_hit:
                sideboard_fallback = (did, _swap_into_main(list(dd["cards"]), side_hit), "sideboard")
    return sideboard_fallback


def apply_deck_changes(start: GameState, pred: dict) -> tuple[GameState, list[str]]:
    """Return a COPY of ``start`` in which each of the predicate's named cards is
    REACHABLE in its target side's deck (hand or library), plus human-readable
    change descriptions.

    This NEVER injects a card into the opening hand. A card already in the deck
    is left exactly where the shuffle put it — it's reached through the engine
    (drawn, played, or by re-dealing the opening, see ``/reroll-until``). Only a
    card NO loaded deck contains is brought into the deck via a sideboard swap or
    deck switch, so it can then be reached legitimately."""
    state = copy.deepcopy(start)
    changes: list[str] = []
    seen: set[tuple[str, str]] = set()
    for spec in predicate_unit_specs(pred):
        name = spec.get("name")
        if not name:
            continue
        frag = _norm_name(name)
        ctrl = spec.get("controller")
        side = ctrl if ctrl in ("player_1", "player_2") else "player_1"
        if (frag, side) in seen:
            continue
        seen.add((frag, side))
        hand = getattr(state, f"{side}_hand") or []
        lib = getattr(state, f"{side}_library") or []
        if _find_named(hand, frag) or _find_named(lib, frag):
            continue  # already in this deck — reachable WITHOUT injecting it.
        current_id = getattr(state, f"{side}_deck_id", None)
        provider = _provider_for(frag, current_id)
        if provider is None:
            continue  # no loaded deck lists it — search will report not-found
        deck_id, cards, how = provider
        actual = _find_named(cards, frag) or frag
        # Rebuild that side's opening from the new deck with the wanted card IN
        # HAND, so it's immediately usable: a "card in hand" goal is satisfied at
        # once and a board goal is just a play away — independent of the live
        # position's turn order, runes, or draw timing (which is what made a
        # "draw it over several turns" approach fail from a drifted root).
        rest = list(cards)
        rest.remove(actual)
        opening = rest[:_OPENING_HAND]
        setattr(state, f"{side}_hand", [actual, *opening])
        setattr(state, f"{side}_library", rest[_OPENING_HAND:])
        setattr(state, f"{side}_deck", _deck_object(deck_id, cards))
        setattr(state, f"{side}_deck_id", deck_id)
        if deck_id == current_id:
            changes.append(f"{side}: sideboarded in {actual}")
        elif how == "sideboard":
            changes.append(f"{side}: switched deck to {deck_id}, sideboarded in {actual}")
        else:
            changes.append(f"{side}: switched deck to {deck_id} (has {actual})")
    return state, changes


# --------------------------------------------------------------------------- #
# Move enumeration + search
# --------------------------------------------------------------------------- #
def predicate_card_names(pred: dict) -> set[str]:
    """All lowercased card-name fragments mentioned in ``units`` clauses of a
    predicate. Used to prune the search to relevant card plays."""
    names: set[str] = set()
    if not isinstance(pred, dict):
        return names
    if "units" in pred:
        n = (pred["units"] or {}).get("name")
        if n:
            names.add(_norm_name(n))
    for key in ("all", "any"):
        for child in pred.get(key, []) or []:
            names |= predicate_card_names(child)
    if "not" in pred:
        names |= predicate_card_names(pred["not"])
    return names


def predicate_unit_specs(pred: dict) -> list[dict]:
    """Every ``units`` clause spec mentioned anywhere in ``pred``."""
    specs: list[dict] = []
    if not isinstance(pred, dict):
        return specs
    if "units" in pred and isinstance(pred["units"], dict):
        specs.append(pred["units"])
    for key in ("all", "any"):
        for child in pred.get(key, []) or []:
            specs += predicate_unit_specs(child)
    if "not" in pred:
        specs += predicate_unit_specs(pred["not"])
    return specs


_BOARD_AT = {None, "battlefield", "any_battlefield", "base", "battlefield_1", "battlefield_2"}


def open_play_sides(pred: dict) -> set[str]:
    """Controllers whose units must be PLAYED/MOVED to satisfy a *nameless*,
    board-targeting units clause (e.g. "2 of the opponent's units at a
    battlefield"). Those plays/moves involve cards the predicate doesn't name,
    so name-pruning would make the goal unreachable — we keep them instead.

    A nameless board clause with no controller opens BOTH sides."""
    sides: set[str] = set()
    for spec in predicate_unit_specs(pred):
        if spec.get("name"):
            continue
        at = spec.get("at")
        is_board = at in _BOARD_AT or (
            isinstance(at, list) and any(x in _BOARD_AT for x in at)
        )
        if not is_board:
            continue
        c = spec.get("controller")
        if c in ("player_1", "player_2"):
            sides.add(c)
        else:
            sides.update({"player_1", "player_2"})
    return sides


def enumerate_moves(
    engine: GameEngine,
    relevant_names: set[str] | None = None,
    open_sides: set[str] | None = None,
) -> list[Move]:
    """Every legal move from ``engine``'s current state, as atomic steps —
    the SAME move set the Branch UI offers:

      * raw ``options`` (already real engine verbs: end_turn, pass_priority,
        choose_location, pass_showdown, choose_spell_targets, …), plus
      * every play INTENT — play a card, MOVE a board unit (base↔battlefield,
        which is how units reach battlefields / start showdowns), EQUIP gear,
        and [Repeat] — expanded to atomic actions via ``execution_steps``.

    Only the first rune-payment combo per intent is kept (any legal payment
    reaches the same board) to keep the branching factor sane."""
    out: list[Move] = []
    output = engine.start()
    for actor in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
        opts = (
            output.player_1_options
            if actor == RequiredTo.PLAYER_1
            else output.player_2_options
        )
        for a in opts:
            # Human-readable label (same translator the UI uses), computed from
            # the PRE-action state — otherwise raw options would land on the
            # branch tree as e.g. "player_1:play:choose_location:base".
            out.append(
                Move(label=label_for_action(engine.game_state, actor, a), steps=[(actor, a)], actor=actor.value)
            )
        seen: set[tuple[str, int]] = set()
        # These are the SAME intents the Branch UI offers — including MOVE
        # (unit → battlefield). A move opens a SHOWDOWN, but we do NOT branch on
        # the showdown sub-tree (that combinatorial explosion is what used to
        # hang the search); instead ``_apply_move`` resolves the showdown inline
        # by passing, exactly like clicking through it in the UI. So a move is a
        # single atomic step here, and turn/ready constraints are handled
        # naturally by the search exploring end_turn moves.
        limited = bool(relevant_names) or bool(open_sides)
        if limited:
            intents = (
                *compute_play_intents(engine, actor),
                *compute_move_intents(engine, actor),
            )
        else:
            intents = (
                *compute_play_intents(engine, actor),
                *compute_move_intents(engine, actor),
                *compute_equip_intents(engine, actor),
                *compute_quick_draw_intents(engine, actor),
                *compute_repeat_intents(engine, actor),
            )
        for intent in intents:
            if not intent.combos:
                continue
            # Prune to the moves that can actually progress the goal:
            #   * a play of a NAMED card (any side), and
            #   * any UNIT play / MOVE by a side that a nameless board-units
            #     clause needs populated (its units aren't named, but they must
            #     reach a battlefield to satisfy "N of X's units at a BF").
            # Raw options (end_turn / choose_location / pass) are always kept
            # above, so turn advancement and showdown resolution still flow.
            if limited:
                cname = _norm_name(intent.card_name or "")
                named_hit = bool(relevant_names) and any(rn in cname for rn in relevant_names)
                side_hit = (
                    bool(open_sides)
                    and intent.actor.value in open_sides
                    and intent.play_action in ("play_unit", "play_champion", "move")
                )
                if not (named_hit or side_hit):
                    continue
            key = (intent.play_action, intent.card_index)
            if key in seen:
                continue
            seen.add(key)
            steps = list(execution_steps(intent.combos[0]))
            out.append(
                Move(label=intent.card_name or intent.play_action, steps=steps, actor=actor.value)
            )
    return out


def _signature(gs: GameState):
    """Hashable summary for dedup. Includes turn + rune counts so resource
    accrual across end-turns is NOT collapsed (otherwise the search can't
    'wait' for a play to become affordable)."""

    def us(arr):
        return tuple(sorted((u.card, u.location) for u in (arr or [])))

    def rc(runes):
        ready = sum(1 for r in (runes or []) if not r.exhausted)
        return (ready, len(runes or []) - ready)

    return (
        gs.total_turn_number,
        gs.current_player.value,
        gs.player_1_score,
        gs.player_2_score,
        us(gs.player_1_units),
        us(gs.player_2_units),
        _ctrl(gs.battlefield_1_controller),
        _ctrl(gs.battlefield_2_controller),
        rc(gs.player_1_runes),
        rc(gs.player_2_runes),
        len(gs.player_1_hand or []),
        len(gs.player_2_hand or []),
        gs.pending_chain is not None and len(gs.pending_chain.items),
        gs.pending_play is not None,
        gs.pending_spell_choice is not None,
        gs.pending_showdown is not None,
        gs.pending_combat is not None,
        getattr(gs, "pending_effect_choice", None) is not None,
        getattr(gs, "pending_spell_repeat", None) is not None,
        getattr(gs, "pending_deflect", None) is not None,
        getattr(gs, "pending_accelerate", None) is not None,
        getattr(gs, "pending_ability_cost", None) is not None,
        getattr(gs, "pending_ability_payment", None) is not None,
    )


def _resolve_showdown(eng: GameEngine, limit: int = 24) -> list[Step]:
    """Drive an OPEN showdown to resolution by passing focus — the same thing a
    player does by clicking "pass" in the Branch UI. We never muster or cast in
    the showdown (that's what would explode the search); we just pass until it
    closes (the moving unit conquers an uncontested battlefield). Returns the
    pass-steps applied, in order, so the caller can append them to the move's
    recorded step list and have replay reproduce the exact same state."""
    steps: list[Step] = []
    for _ in range(limit):
        out = eng.start()
        if eng._game_state.pending_showdown is None:
            break
        if out.player_1_options:
            actor = RequiredTo.PLAYER_1
        elif out.player_2_options:
            actor = RequiredTo.PLAYER_2
        else:
            break
        if "play:pass_showdown" not in (
            out.player_1_options if actor == RequiredTo.PLAYER_1 else out.player_2_options
        ):
            break
        eng.apply_action(action="play:pass_showdown", actor=actor)
        steps.append((actor, "play:pass_showdown"))
    return steps


def _apply_move(state: GameState, move: Move) -> GameState | None:
    """Apply ``move`` to a FORK of ``state`` and return the resulting state,
    or ``None`` if any step is rejected (shouldn't happen for enumerated
    moves, but guards against edge cases).

    If the move opens a showdown (a unit moved onto a battlefield), it is
    resolved INLINE by passing — the showdown is collapsed into this one move
    rather than explored as its own search sub-tree. The pass-steps are folded
    back into ``move.steps`` so the recorded path replays to the same state."""
    eng = GameEngine(game_state=copy.deepcopy(state))
    eng.start()
    try:
        for actor, action in move.steps:
            eng.apply_action(action=action, actor=actor)
        extra = _resolve_showdown(eng)
    except Exception:
        return None
    if extra:
        move.steps = [*move.steps, *extra]
    return eng.game_state


def _path_str(path: list[Move]) -> str:
    return " > ".join(m.label for m in path) if path else "(start)"


def bfs_search(
    start: GameState,
    predicate: dict,
    *,
    max_depth: int = 14,
    node_budget: int = 20000,
    time_budget_s: float = 20.0,
    verbose: bool = False,
    allow_deck_changes: bool = False,
) -> SearchResult:
    """Breadth-first search for the shortest move path whose resulting state
    satisfies ``predicate``. Returns the first (shortest) hit, or not-found
    once the depth / node budget is exhausted.

    When ``allow_deck_changes`` is set, a named card the loaded decks can't
    reach is brought into play first via a sideboard swap or deck switch (see
    ``apply_deck_changes``); the edits made are reported on ``SearchResult``.

    When ``verbose`` is set, every node expanded and every new path enqueued
    is logged to stderr (the engine terminal), plus a final summary."""
    deck_changes: list[str] = []
    if allow_deck_changes:
        start, deck_changes = apply_deck_changes(start, predicate)
        if verbose and deck_changes:
            _log(f"deck changes: {deck_changes}")

    def _result(**kw) -> SearchResult:
        return SearchResult(deck_changes=deck_changes, **kw)

    start_view = state_view(start)
    start_dist = goal_distance(predicate, start_view)
    if verbose:
        _log(
            f"START predicate={predicate} distance={start_dist} "
            f"max_depth={max_depth} node_budget={node_budget}"
        )
    if evaluate_predicate(predicate, start_view):
        if verbose:
            _log("already satisfied at start")
        return _result(found=True, moves=[], nodes_explored=0, depth=0, reason="already satisfied")

    # Best-first (greedy) search: a priority queue ordered by goal_distance,
    # then by shallower depth, so the most-promising states (closest to the
    # goal) are expanded first. This beelines toward the named cards/conditions
    # instead of exploring every permutation breadth-first.
    relevant = predicate_card_names(predicate)
    open_sides = open_play_sides(predicate)
    if verbose and relevant:
        _log(f"pruning plays to cards matching: {sorted(relevant)}")
    if verbose and open_sides:
        _log(f"also allowing any unit play/move for: {sorted(open_sides)}")
    seen = {_signature(start)}
    counter = 0  # unique tiebreaker so heap never compares GameState/list
    heap: list[tuple[int, int, int, GameState, list[Move]]] = [
        (start_dist, 0, counter, start, [])
    ]
    nodes = 0
    enqueued = 1
    best = start_dist  # closest distance seen so far
    cur_depth = 0
    PROGRESS_EVERY = 500
    t0 = time.time()

    def _progress(tag: str) -> None:
        _log(
            f"{tag} explored={nodes} frontier={len(heap)} states_seen={len(seen)} "
            f"closest={best}/{start_dist} depth={cur_depth}/{max_depth} t={time.time() - t0:.1f}s"
        )

    while heap:
        dist, depth, _c, state, path = heapq.heappop(heap)
        nodes += 1
        cur_depth = depth
        if dist < best:
            best = dist
            if verbose:
                _progress(f"closer (distance {best:.2f}) —")
        elif verbose and nodes % PROGRESS_EVERY == 0:
            _progress("...")
        if nodes > node_budget:
            if verbose:
                _progress("STOP node budget exhausted —")
            return _result(found=False, nodes_explored=nodes, reason="node budget exhausted")
        if time.time() - t0 > time_budget_s:
            if verbose:
                _progress("STOP time budget exhausted —")
            return _result(found=False, nodes_explored=nodes, reason="time budget exhausted")
        if depth >= max_depth:
            continue
        fork = GameEngine(game_state=copy.deepcopy(state))
        for move in enumerate_moves(fork, relevant, open_sides):
            # Time check INSIDE the move loop too — a single node can have many
            # moves (showdown/combat states), each an expensive fork, so the
            # per-node check alone can't bound wall-clock.
            if time.time() - t0 > time_budget_s:
                if verbose:
                    _progress("STOP time budget exhausted (mid-node) —")
                return _result(found=False, nodes_explored=nodes, reason="time budget exhausted")
            ns = _apply_move(state, move)
            if ns is None:
                continue
            nv = state_view(ns)
            if evaluate_predicate(predicate, nv):
                if verbose:
                    _log(
                        f"FOUND at depth {depth + 1} after exploring {nodes} states "
                        f"(of {enqueued} seen): {_path_str([*path, move])}"
                    )
                return _result(
                    found=True, moves=[*path, move], nodes_explored=nodes, depth=depth + 1
                )
            sg = _signature(ns)
            if sg not in seen:
                seen.add(sg)
                enqueued += 1
                counter += 1
                heapq.heappush(
                    heap, (goal_distance(predicate, nv), depth + 1, counter, ns, [*path, move])
                )
    if verbose:
        _progress("STOP exhausted reachable states (not found) —")
    return _result(found=False, nodes_explored=nodes, reason="exhausted reachable states")
