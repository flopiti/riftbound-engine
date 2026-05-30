"""Parse and evaluate the per-card "Spell Choice Requirement" codes.

The ``Spell Choice Requirement`` column of ``riftbound_cards.csv`` encodes the
UNIT/GEAR/etc. choices a player is REQUIRED to be able to make in order to
*play* a Spell. Before the engine offers a ``play:play_spell:<i>`` option it
must confirm the player has at least one VALID set of targets for the card's
requirement — paying the Energy/Power cost is necessary but no longer
sufficient.

This module is the first slice of that system. It implements:

  * the pipe-run delimiter grammar authored by the Spell Wizard
    (``|`` = AND within a group, ``||`` = OR within a group,
    ``|||`` = AND between groups, ``||||`` = OR between groups), mirroring
    ``riftbound/src/SpellWizardApp.tsx`` so values round-trip identically;
  * parsing + satisfiability for the ``ANY UNIT`` family of phrases
    (the codes we're starting with):

        ANY UNIT (0-3)[SAME_LOC]      ANY UNIT (1[BASE])
        ANY UNIT (1-2)                ANY UNIT (1[BF <= 2M])
        ANY UNIT (1)                  ANY UNIT (1[BF])
        ANY UNIT (1) EQUIPMENT (1)    ANY UNIT (1[EXHAUSTED])
          [SAME_CONT]                 ANY UNIT (2)
        ANY UNIT (1[<=3M BF])         ANY UNIT (n)[BF]
        ANY UNIT (1[ATTACKING])       ANY UNIT (n)[SUM <= 4M]

Grammar of a single ANY UNIT phrase
------------------------------------
``ANY UNIT (<count>[<per-unit filter>]) [<group filter>]``

* ``<count>`` is ``1`` / ``2`` (exact), ``1-2`` / ``0-3`` (range), or ``n``
  (any number). ``n`` and any range whose minimum is 0 are trivially
  satisfiable: per product decision, those cards are ALWAYS offered.
* The bracket *inside* the parentheses constrains EACH selected unit.
* The bracket *outside* the parentheses constrains the selected set AS A
  WHOLE (e.g. ``SAME_LOC``, ``SUM <= 4M``). Per-unit-style tokens that
  appear outside (``BF``) are applied to every unit.

Design decisions (confirmed with the engine owner):
  * ``ATTACKING`` — there is no persistent attacking flag, so a unit counts
    as attacking only when it belongs to the *initiator* of an active
    showdown and sits at the contested battlefield.
  * ``EQUIPMENT`` — the engine has no equipment model yet, so any phrase
    that references EQUIPMENT is treated as NEVER satisfiable (the card is
    not offered).
  * Minimum count of 0 (``n`` / ``0-N``) ⇒ always offered.
  * Selectors we don't handle yet (FRIENDLY UNIT, ENEMY UNIT, MOVE, GEAR,
    SPELL, BATTLEFIELD, …) default to SATISFIABLE so the 100+ cards that
    use them keep their current behaviour until they're wired up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from .csv_data import card_might_of

if TYPE_CHECKING:  # avoid an import cycle at module load
    from .engine import GameState


# --------------------------------------------------------------------------
# Board snapshot used by the evaluator (decoupled from engine dataclasses)
# --------------------------------------------------------------------------
_BATTLEFIELDS = ("battlefield_1", "battlefield_2")


@dataclass(frozen=True)
class UnitView:
    """A flat, evaluator-friendly view of one unit on the board."""

    controller: str  # "player_1" / "player_2"
    index: int  # position in its controller's units list (player_X_units)
    card: str
    location: str  # "base" / "battlefield_1" / "battlefield_2"
    exhausted: bool
    might: int
    attacking: bool

    @property
    def at_battlefield(self) -> bool:
        return self.location in _BATTLEFIELDS

    @property
    def at_base(self) -> bool:
        return self.location == "base"


def _attacking_keys(state: "GameState") -> set[tuple[str, int]]:
    """(controller, index) pairs for units that currently count as attacking.

    A unit is attacking only while a showdown it initiated is open and it
    sits at the contested battlefield. During the normal action turn no
    showdown is open, so this is empty — which is exactly why
    ``ANY UNIT (1[ATTACKING])`` spells aren't offered as plain plays.
    """
    showdown = getattr(state, "pending_showdown", None)
    if showdown is None:
        return set()
    initiator = getattr(showdown.initiator, "value", showdown.initiator)
    bf = showdown.battlefield
    units = state.player_1_units if initiator == "player_1" else state.player_2_units
    return {
        (initiator, i) for i, u in enumerate(units) if u.location == bf
    }


def board_units(state: "GameState") -> list[UnitView]:
    """Flatten both players' units into ``UnitView`` rows for evaluation."""
    attacking = _attacking_keys(state)
    views: list[UnitView] = []
    for controller, units in (
        ("player_1", state.player_1_units),
        ("player_2", state.player_2_units),
    ):
        for i, u in enumerate(units):
            might = card_might_of(u.card)
            views.append(
                UnitView(
                    controller=controller,
                    index=i,
                    card=u.card,
                    location=u.location,
                    exhausted=bool(u.exhausted),
                    might=0 if might is None else might,
                    attacking=(controller, i) in attacking,
                )
            )
    return views


# --------------------------------------------------------------------------
# Parsed phrase model
# --------------------------------------------------------------------------
@dataclass
class UnitRequirement:
    """A parsed ``ANY UNIT`` (or related unit-selector) phrase."""

    min_count: int
    max_count: int | None  # None ⇒ unbounded ("n")
    # Per-unit predicates (every selected unit must satisfy all of these):
    require_battlefield: bool = False
    require_base: bool = False
    require_exhausted: bool = False
    require_attacking: bool = False
    might_max: int | None = None
    might_min: int | None = None
    # Group predicates over the selected set as a whole:
    same_location: bool = False
    sum_might_max: int | None = None
    # If True the phrase can never be satisfied (e.g. references EQUIPMENT,
    # which the engine doesn't model yet).
    impossible: bool = False

    def unit_matches(self, unit: UnitView) -> bool:
        if self.require_battlefield and not unit.at_battlefield:
            return False
        if self.require_base and not unit.at_base:
            return False
        if self.require_exhausted and not unit.exhausted:
            return False
        if self.require_attacking and not unit.attacking:
            return False
        if self.might_max is not None and unit.might > self.might_max:
            return False
        if self.might_min is not None and unit.might < self.might_min:
            return False
        return True


@dataclass
class Phrase:
    """One requirement phrase. Either a handled unit requirement or unknown.

    ``unit`` is set for the ANY-UNIT family we evaluate. ``unknown`` means a
    selector we don't handle yet (FRIENDLY UNIT, GEAR, …) — those default to
    satisfiable so their cards keep working until wired up.
    """

    raw: str
    unit: UnitRequirement | None = None
    unknown: bool = False


# --------------------------------------------------------------------------
# Phrase parsing
# --------------------------------------------------------------------------
_COUNT_RE = re.compile(r"^\s*(n|\d+(?:\s*-\s*\d+)?)\s*(?:\[(.*)\])?\s*$", re.IGNORECASE)
_MIGHT_LE_RE = re.compile(r"<=\s*(\d+)\s*M", re.IGNORECASE)
_MIGHT_GE_RE = re.compile(r">=\s*(\d+)\s*M", re.IGNORECASE)
_SUM_LE_RE = re.compile(r"SUM\s*<=\s*(\d+)\s*M", re.IGNORECASE)


def _parse_count(token: str) -> tuple[int, int | None]:
    """('1'|'2'|'1-2'|'0-3'|'n') → (min_count, max_count|None)."""
    token = token.strip().lower()
    if token == "n":
        return 0, None
    if "-" in token:
        lo, hi = (p.strip() for p in token.split("-", 1))
        return int(lo), int(hi)
    value = int(token)
    return value, value


def _apply_filter_tokens(req: UnitRequirement, text: str, *, is_group: bool) -> None:
    """Fill predicate fields from a bracket's contents.

    ``is_group`` distinguishes the bracket *outside* the parentheses (set-
    level constraints like SAME_LOC / SUM) from the one *inside* (per-unit).
    Tokens are matched case-insensitively and order-independently so both
    ``[<=3M BF]`` and ``[BF <= 2M]`` parse correctly.
    """
    if not text:
        return
    upper = text.upper()

    if is_group:
        if "SAME_LOC" in upper:
            req.same_location = True
        sum_m = _SUM_LE_RE.search(text)
        if sum_m:
            req.sum_might_max = int(sum_m.group(1))
            # A SUM constraint consumes the "<= N M" — don't also read it as
            # a per-unit might bound below.
            return

    if "BF" in upper:
        req.require_battlefield = True
    if "BASE" in upper:
        req.require_base = True
    if "EXHAUSTED" in upper:
        req.require_exhausted = True
    if "ATTACKING" in upper:
        req.require_attacking = True
    le = _MIGHT_LE_RE.search(text)
    if le:
        req.might_max = int(le.group(1))
    ge = _MIGHT_GE_RE.search(text)
    if ge:
        req.might_min = int(ge.group(1))


def parse_phrase(raw: str) -> Phrase:
    """Parse a single requirement phrase into a :class:`Phrase`."""
    text = raw.strip()
    upper = text.upper()

    # EQUIPMENT isn't modelled yet → never satisfiable, regardless of what
    # else the phrase mentions (e.g. "ANY UNIT (1) EQUIPMENT (1) [SAME_CONT]").
    if "EQUIPMENT" in upper:
        return Phrase(raw=text, unit=UnitRequirement(min_count=1, max_count=1, impossible=True))

    # We only handle the ANY UNIT family in this slice. Everything else is
    # left ungated (satisfiable) so existing cards keep working.
    if not upper.startswith("ANY UNIT"):
        return Phrase(raw=text, unknown=True)

    # Count + optional inner (per-unit) bracket live in the first (...).
    paren = re.search(r"\(([^)]*)\)", text)
    if paren is None:
        # Malformed — be lenient and don't block the card.
        return Phrase(raw=text, unknown=True)

    m = _COUNT_RE.match(paren.group(1))
    if m is None:
        return Phrase(raw=text, unknown=True)
    min_count, max_count = _parse_count(m.group(1))
    req = UnitRequirement(min_count=min_count, max_count=max_count)
    _apply_filter_tokens(req, (m.group(2) or "").strip(), is_group=False)

    # Any bracket AFTER the parentheses is a group-level constraint.
    after = text[paren.end():]
    for grp in re.findall(r"\[(.*?)\]", after):
        _apply_filter_tokens(req, grp.strip(), is_group=True)

    return Phrase(raw=text, unit=req)


# --------------------------------------------------------------------------
# Tree parsing (pipe-run grammar — mirrors SpellWizardApp.tsx)
# --------------------------------------------------------------------------
@dataclass
class Group:
    phrases: list[Phrase] = field(default_factory=list)
    connectors: list[str] = field(default_factory=list)  # "AND"/"OR", len == max(0, len(phrases)-1)


@dataclass
class RequirementTree:
    groups: list[Group] = field(default_factory=list)
    connectors: list[str] = field(default_factory=list)  # between groups


_PIPE_SPLIT_RE = re.compile(r"(\|+)")


def parse_tree(raw: str | None) -> RequirementTree:
    """Parse the raw CSV value into groups of phrases with AND/OR connectors.

    Run length of pipes: 1 = AND within group, 2 = OR within group,
    3 = AND between groups, 4 = OR between groups. Older values (runs of
    1–2 only) parse as a single group — the historical flat list.
    """
    tree = RequirementTree()
    if not raw:
        return tree
    tokens = _PIPE_SPLIT_RE.split(raw)

    cur_phrases: list[Phrase] = []
    cur_connectors: list[str] = []
    pending_inner = "AND"
    pending_top = "AND"

    def flush() -> None:
        if not cur_phrases:
            return
        if tree.groups:
            tree.connectors.append(pending_top)
        tree.groups.append(Group(phrases=list(cur_phrases), connectors=list(cur_connectors)))
        cur_phrases.clear()
        cur_connectors.clear()

    for i, tok in enumerate(tokens):
        if i % 2 == 1:  # a run of pipes
            run = len(tok)
            if run >= 3:
                flush()
                pending_top = "OR" if run >= 4 else "AND"
            else:
                pending_inner = "OR" if run >= 2 else "AND"
            continue
        phrase_text = tok.strip()
        if not phrase_text:
            continue
        if cur_phrases:
            cur_connectors.append(pending_inner)
        cur_phrases.append(parse_phrase(phrase_text))
        pending_inner = "AND"
    flush()
    return tree


# --------------------------------------------------------------------------
# Satisfiability
# --------------------------------------------------------------------------
def _unit_phrase_satisfiable(req: UnitRequirement, units: Iterable[UnitView]) -> bool:
    if req.impossible:
        return False
    # Minimum of 0 (e.g. "n" or "0-3") is trivially satisfiable ⇒ always offer.
    if req.min_count <= 0:
        return True

    matching = [u for u in units if req.unit_matches(u)]
    if len(matching) < req.min_count:
        return False

    if req.same_location:
        # Need ``min_count`` matching units sharing a single location.
        by_loc: dict[str, int] = {}
        for u in matching:
            by_loc[u.location] = by_loc.get(u.location, 0) + 1
        if max(by_loc.values(), default=0) < req.min_count:
            return False

    if req.sum_might_max is not None:
        # Pick the ``min_count`` lowest-might matches; if even those exceed
        # the sum cap the phrase can't be satisfied.
        cheapest = sorted(u.might for u in matching)[: req.min_count]
        if sum(cheapest) > req.sum_might_max:
            return False

    return True


def _phrase_satisfiable(phrase: Phrase, units: list[UnitView]) -> bool:
    if phrase.unknown:
        return True  # selector not handled yet → don't block the card
    if phrase.unit is not None:
        return _unit_phrase_satisfiable(phrase.unit, units)
    return True


def _fold(values: list[bool], connectors: list[str]) -> bool:
    """Left-to-right fold of booleans with per-gap AND/OR connectors."""
    if not values:
        return True
    acc = values[0]
    for i, conn in enumerate(connectors):
        nxt = values[i + 1]
        acc = (acc or nxt) if conn == "OR" else (acc and nxt)
    return acc


def tree_satisfiable(tree: RequirementTree, units: list[UnitView]) -> bool:
    if not tree.groups:
        return True
    group_values = [
        _fold([_phrase_satisfiable(p, units) for p in g.phrases], g.connectors)
        for g in tree.groups
    ]
    return _fold(group_values, tree.connectors)


def requirement_satisfiable(raw: str | None, state: "GameState") -> bool:
    """Whether ``raw``'s requirement has at least one valid target set now.

    Empty/blank requirements are always satisfiable (no choice needed).
    """
    if not raw or not raw.strip():
        return True
    tree = parse_tree(raw)
    return tree_satisfiable(tree, board_units(state))


def spell_playable(state: "GameState", card: str) -> bool:
    """True if ``card``'s Spell Choice Requirement can be met on the board.

    Convenience wrapper used by the engine's action generator: looks up the
    card's raw requirement from the CSV and evaluates it. Cards with no
    requirement are always playable (subject to the engine's cost gates,
    which are checked separately).
    """
    from .csv_data import card_spell_requirement_of

    return requirement_satisfiable(card_spell_requirement_of(card), state)


# --------------------------------------------------------------------------
# Target selection (phase 2): turn a requirement into the concrete choices
# --------------------------------------------------------------------------
# A "target ref" is the pair (controller, index) identifying one unit in its
# controller's units list. The wire form is "p1-0" / "p2-1".
TargetRef = tuple[str, int]

_REF_SHORT = {"player_1": "p1", "player_2": "p2"}
_REF_LONG = {"p1": "player_1", "p2": "player_2"}


def ref_to_token(ref: TargetRef) -> str:
    return f"{_REF_SHORT[ref[0]]}-{ref[1]}"


def token_to_ref(token: str) -> TargetRef:
    short, _, idx = token.strip().partition("-")
    if short not in _REF_LONG or not idx.isdigit():
        raise ValueError(f"bad target ref {token!r}; expected e.g. 'p1-0'")
    return (_REF_LONG[short], int(idx))


def selectable_unit_requirement(raw: str | None) -> UnitRequirement | None:
    """Return the single ANY-UNIT requirement that needs an explicit pick.

    This is the first slice of the choice system: it only fires for a
    requirement that parses to EXACTLY one group with one unit phrase whose
    minimum is >= 1 (so a real choice is forced) and isn't impossible.
    Anything else — blank, min-0 (``n``/``0-N``), an unknown selector, or a
    multi-phrase/multi-group tree — returns ``None``, and the engine commits
    the spell immediately the way it always has.
    """
    if not raw or not raw.strip():
        return None
    tree = parse_tree(raw)
    if len(tree.groups) != 1 or len(tree.groups[0].phrases) != 1:
        return None
    phrase = tree.groups[0].phrases[0]
    if phrase.unit is None or phrase.unknown or phrase.unit.impossible:
        return None
    if phrase.unit.min_count < 1:
        return None
    return phrase.unit


def _group_ok(req: UnitRequirement, combo: tuple[UnitView, ...]) -> bool:
    if req.same_location and len({u.location for u in combo}) > 1:
        return False
    if req.sum_might_max is not None and sum(u.might for u in combo) > req.sum_might_max:
        return False
    return True


def enumerate_unit_target_sets(
    req: UnitRequirement, state: "GameState", *, cap: int = 256
) -> list[tuple[TargetRef, ...]]:
    """All valid target sets for ``req`` on the current board.

    Each result is a tuple of ``(controller, index)`` refs. Set sizes range
    over ``[min_count, max_count]`` (``max_count`` None ⇒ up to every
    matching unit). Per-unit filters are applied first; group constraints
    (SAME_LOC / SUM) are checked per candidate set. Capped at ``cap`` sets.
    """
    import itertools

    matching = [u for u in board_units(state) if req.unit_matches(u)]
    if len(matching) < req.min_count:
        return []
    hi = len(matching) if req.max_count is None else min(req.max_count, len(matching))
    lo = max(req.min_count, 1)  # the choice step never offers an empty pick

    out: list[tuple[TargetRef, ...]] = []
    for size in range(lo, hi + 1):
        for combo in itertools.combinations(matching, size):
            if not _group_ok(req, combo):
                continue
            out.append(tuple((u.controller, u.index) for u in combo))
            if len(out) >= cap:
                return out
    return out


def target_set_satisfies(
    req: UnitRequirement, state: "GameState", refs: list[TargetRef]
) -> bool:
    """Validate a concrete chosen set of refs against ``req``."""
    units = board_units(state)
    by_ref = {(u.controller, u.index): u for u in units}
    if len(set(refs)) != len(refs):
        return False  # no duplicate targets
    if not (req.min_count <= len(refs) and (req.max_count is None or len(refs) <= req.max_count)):
        return False
    chosen: list[UnitView] = []
    for r in refs:
        u = by_ref.get(r)
        if u is None or not req.unit_matches(u):
            return False
        chosen.append(u)
    return _group_ok(req, tuple(chosen))
