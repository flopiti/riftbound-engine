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

from .csv_data import card_energy_of, card_might_of, card_power_of, card_type_of

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
    # Controller scope relative to the CASTER: None ⇒ either side counts
    # ("ANY UNIT"); "friendly" ⇒ only the caster's units; "enemy" ⇒ only the
    # opponent's. Resolving friendly/enemy needs to know who is casting, so
    # ``unit_matches`` takes the caster's controller string.
    side: str | None = None
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
    # If True this is a MOVE phrase: after the unit is picked the caster also
    # picks a destination location. Doesn't change which units MATCH (any
    # matching unit can be moved); it only signals the extra destination pick.
    move: bool = False

    def unit_matches(self, unit: UnitView, caster: str | None = None) -> bool:
        # Controller scope. ``caster`` is the casting player's controller
        # string ("player_1"/"player_2"). When it's unknown (None) the side
        # filter can't be resolved, so it's skipped rather than guessed.
        if self.side is not None and caster is not None:
            if self.side == "friendly" and unit.controller != caster:
                return False
            if self.side == "enemy" and unit.controller == caster:
                return False
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
class BattlefieldRequirement:
    """A parsed ``BATTLEFIELD`` phrase — pick one battlefield.

    ``where_friendly`` ⇒ ``BATTLEFIELD[WHERE_FRIENDLY_UNITS]``: only a
    battlefield where the caster has a unit may be chosen.
    """

    where_friendly: bool = False


@dataclass
class GearRequirement:
    """A parsed ``GEAR`` phrase — pick ``min_count`` gear(s) on the board.

    Gears carry no side qualifier in the card data (any player's gear may be
    targeted). Bare ``GEAR`` is one gear; ``GEAR (0-1)`` is optional (min 0 ⇒
    trivially satisfiable, no forced pick).
    """

    min_count: int = 1
    max_count: int | None = 1


@dataclass
class TrashRequirement:
    """A parsed ``... TRASH ...`` phrase — pick ``min_count`` card(s) from a
    trash (discard) zone.

    ``side`` scopes the zone relative to the caster (friendly = caster's trash,
    enemy = opponent's, None = either). ``unit_only`` ⇒ the phrase said "TRASH
    UNIT" (only Unit-type trash cards qualify). ``energy_max`` ⇒ a ``[<= NE]``
    filter on the card's Energy cost.
    """

    min_count: int = 1
    max_count: int | None = 1
    side: str | None = None
    unit_only: bool = False
    energy_max: int | None = None

    def matches(self, card_name: str) -> bool:
        if self.unit_only and card_type_of(card_name) != "Unit":
            return False
        if self.energy_max is not None:
            e = card_energy_of(card_name)
            if e is None or e > self.energy_max:
                return False
        return True


@dataclass
class SpellRequirement:
    """A parsed ``... SPELL ...`` phrase — pick a spell on the chain (the
    priority stack), e.g. for a counterspell.

    ``side`` scopes relative to the caster (enemy = opponent's spell, None =
    any). ``energy_max`` / ``power_max`` come from a ``(<= NE AND <= NP)``
    filter on the TARGET spell's printed cost.
    """

    min_count: int = 1
    max_count: int | None = 1
    side: str | None = None
    energy_max: int | None = None
    power_max: int | None = None

    def matches(self, item_actor: str, item_card: str, caster: str | None) -> bool:
        if self.side == "enemy" and (caster is None or item_actor == caster):
            return False
        if self.side == "friendly" and (caster is None or item_actor != caster):
            return False
        if self.energy_max is not None:
            e = card_energy_of(item_card)
            if e is None or e > self.energy_max:
                return False
        if self.power_max is not None:
            p = card_power_of(item_card) or 0
            if p > self.power_max:
                return False
        return True


@dataclass
class LocationRequirement:
    """A parsed ``LOCATION`` phrase — pick one location (base / either
    battlefield). No filters appear in the card data, so it's a plain pick."""

    pass


@dataclass
class Phrase:
    """One requirement phrase. Either a handled unit / battlefield / gear /
    trash / spell / location requirement or unknown.

    ``unit`` is set for the unit-selector family (ANY / FRIENDLY / ENEMY UNIT,
    incl. MOVE). ``battlefield`` is set for BATTLEFIELD phrases, ``gear`` for
    GEAR, ``trash`` for trash-zone, ``spell`` for chain-spell, ``location`` for
    LOCATION phrases. ``unknown`` means a selector we don't handle yet
    (ABILITY, …) — those default to satisfiable so their cards keep working.
    """

    raw: str
    unit: UnitRequirement | None = None
    battlefield: BattlefieldRequirement | None = None
    gear: GearRequirement | None = None
    trash: TrashRequirement | None = None
    spell: SpellRequirement | None = None
    location: LocationRequirement | None = None
    unknown: bool = False


# --------------------------------------------------------------------------
# Phrase parsing
# --------------------------------------------------------------------------
_COUNT_RE = re.compile(r"^\s*(n|\d+(?:\s*-\s*\d+)?)\s*(?:\[(.*)\])?\s*$", re.IGNORECASE)
_MIGHT_LE_RE = re.compile(r"<=\s*(\d+)\s*M", re.IGNORECASE)
_MIGHT_GE_RE = re.compile(r">=\s*(\d+)\s*M", re.IGNORECASE)
_SUM_LE_RE = re.compile(r"SUM\s*<=\s*(\d+)\s*M", re.IGNORECASE)
_ENERGY_LE_RE = re.compile(r"<=\s*(\d+)\s*E", re.IGNORECASE)
_POWER_LE_RE = re.compile(r"<=\s*(\d+)\s*P", re.IGNORECASE)


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

    # MOVE phrases ("MOVE FRIENDLY UNIT (1)") are unit selectors PLUS a
    # destination pick: the unit-selection half is identical to the plain
    # selector, so strip the MOVE prefix and parse the remainder as one, with
    # the ``move`` flag set. The body (`parse_body` below) handles the rest.
    move = False
    body = text
    body_upper = upper
    if body_upper.startswith("MOVE"):
        # MOVE + TRASH (a trash-zone move) still needs a ref namespace we don't
        # model — leave it ungated.
        if "TRASH" in body_upper:
            return Phrase(raw=text, unknown=True)
        move = True
        body = text[len("MOVE"):].strip()
        body_upper = body.upper()

    # TRASH-zone targeting: pick card(s) from a trash pile. "TRASH UNIT" limits
    # to Unit-type cards; a "(1[<= 2E])" filter caps the card's Energy cost.
    # MOVE-from-trash was already short-circuited above (needs a destination).
    if "TRASH" in body_upper:
        if body_upper.startswith("FRIENDLY"):
            t_side: str | None = "friendly"
        elif body_upper.startswith("ENEMY"):
            t_side = "enemy"
        else:
            t_side = None
        unit_only = "TRASH UNIT" in body_upper
        t_paren = re.search(r"\(([^)]*)\)", body)
        if t_paren is None:
            return Phrase(
                raw=text,
                trash=TrashRequirement(min_count=1, max_count=1, side=t_side, unit_only=unit_only),
            )
        tm = _COUNT_RE.match(t_paren.group(1))
        if tm is None:
            return Phrase(raw=text, unknown=True)
        lo, hi = _parse_count(tm.group(1))
        e_le = _ENERGY_LE_RE.search(tm.group(2) or "")
        energy_max = int(e_le.group(1)) if e_le else None
        return Phrase(
            raw=text,
            trash=TrashRequirement(
                min_count=lo, max_count=hi, side=t_side, unit_only=unit_only, energy_max=energy_max
            ),
        )

    # BATTLEFIELD: pick one battlefield. ``[WHERE_FRIENDLY_UNITS]`` restricts
    # to a battlefield where the caster has a unit. (MOVE doesn't apply to a
    # battlefield pick, so a stray MOVE prefix is just ignored here.)
    if body_upper.startswith("BATTLEFIELD"):
        where_friendly = "WHERE_FRIENDLY_UNITS" in body_upper
        return Phrase(raw=text, battlefield=BattlefieldRequirement(where_friendly=where_friendly))

    # GEAR: pick gear(s) on the board (any player's). Bare "GEAR" defaults to
    # one; an explicit count in parens ("GEAR (0-1)") overrides it.
    if body_upper.startswith("GEAR"):
        gear_paren = re.search(r"\(([^)]*)\)", body)
        if gear_paren is None:
            return Phrase(raw=text, gear=GearRequirement(min_count=1, max_count=1))
        cm = _COUNT_RE.match(gear_paren.group(1))
        if cm is None:
            return Phrase(raw=text, unknown=True)
        lo, hi = _parse_count(cm.group(1))
        return Phrase(raw=text, gear=GearRequirement(min_count=lo, max_count=hi))

    # SPELL: pick a spell on the chain (counterspell-style). ABILITY isn't
    # modelled (no ability stack), so a phrase mentioning ABILITY stays
    # deferred. Cost filters "(<= NE AND <= NP)" cap the TARGET spell's cost.
    if "SPELL" in body_upper and "ABILITY" not in body_upper:
        if body_upper.startswith("FRIENDLY"):
            s_side: str | None = "friendly"
        elif body_upper.startswith("ENEMY"):
            s_side = "enemy"
        else:
            s_side = None
        e_le = _ENERGY_LE_RE.search(body)
        p_le = _POWER_LE_RE.search(body)
        return Phrase(
            raw=text,
            spell=SpellRequirement(
                min_count=1,
                max_count=1,
                side=s_side,
                energy_max=int(e_le.group(1)) if e_le else None,
                power_max=int(p_le.group(1)) if p_le else None,
            ),
        )

    # LOCATION: pick one location (base or either battlefield).
    if body_upper.startswith("LOCATION"):
        return Phrase(raw=text, location=LocationRequirement())

    # Unit selectors we evaluate: ANY / FRIENDLY / ENEMY UNIT. ``side`` scopes
    # the controller relative to the caster (None = either side counts).
    # Everything else (GEAR, SPELL, BATTLEFIELD, …) is left ungated so those
    # cards keep working until they're wired up.
    if body_upper.startswith("ANY UNIT"):
        side: str | None = None
    elif body_upper.startswith("FRIENDLY UNIT"):
        side = "friendly"
    elif body_upper.startswith("ENEMY UNIT"):
        side = "enemy"
    else:
        return Phrase(raw=text, unknown=True)

    # Count + optional inner (per-unit) bracket live in the first (...).
    paren = re.search(r"\(([^)]*)\)", body)
    if paren is None:
        # Malformed — be lenient and don't block the card.
        return Phrase(raw=text, unknown=True)

    m = _COUNT_RE.match(paren.group(1))
    if m is None:
        return Phrase(raw=text, unknown=True)
    min_count, max_count = _parse_count(m.group(1))
    req = UnitRequirement(min_count=min_count, max_count=max_count, side=side, move=move)
    _apply_filter_tokens(req, (m.group(2) or "").strip(), is_group=False)

    # Any bracket AFTER the parentheses is a group-level constraint.
    after = body[paren.end():]
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
def _unit_phrase_satisfiable(
    req: UnitRequirement, units: Iterable[UnitView], caster: str | None = None
) -> bool:
    if req.impossible:
        return False
    # Minimum of 0 (e.g. "n" or "0-3") is trivially satisfiable ⇒ always offer.
    if req.min_count <= 0:
        return True

    matching = [u for u in units if req.unit_matches(u, caster)]
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


def _battlefield_phrase_satisfiable(
    req: BattlefieldRequirement, units: Iterable[UnitView], caster: str | None = None
) -> bool:
    # Plain BATTLEFIELD: the two battlefield slots always exist in a live game,
    # so it's always satisfiable. WHERE_FRIENDLY_UNITS needs a battlefield where
    # the caster has at least one unit.
    if not req.where_friendly:
        return True
    if caster is None:
        return True  # can't resolve scope → don't block the card
    return any(u.controller == caster and u.at_battlefield for u in units)


def matching_trash_refs(
    req: TrashRequirement,
    trash_p1: list[str],
    trash_p2: list[str],
    caster: str | None = None,
) -> list[TargetRef]:
    """(controller, index) refs for trash cards matching ``req`` in the
    caster-scoped zone(s). Shared by satisfiability and the engine's option
    enumeration so they agree on what's pickable."""
    sides: list[tuple[str, list[str]]] = []
    if req.side == "friendly":
        sides = [("player_1", trash_p1)] if caster == "player_1" else [("player_2", trash_p2)]
        if caster is None:
            sides = [("player_1", trash_p1), ("player_2", trash_p2)]
    elif req.side == "enemy":
        if caster == "player_1":
            sides = [("player_2", trash_p2)]
        elif caster == "player_2":
            sides = [("player_1", trash_p1)]
        else:
            sides = [("player_1", trash_p1), ("player_2", trash_p2)]
    else:
        sides = [("player_1", trash_p1), ("player_2", trash_p2)]
    out: list[TargetRef] = []
    for controller, pile in sides:
        for i, name in enumerate(pile):
            if req.matches(name):
                out.append((controller, i))
    return out


def matching_spell_refs(
    req: SpellRequirement,
    chain_items: list[tuple[str, str]],
    caster: str | None = None,
) -> list[int]:
    """Indices of chain spells (``chain_items`` = (actor, card) pairs, index 0 =
    top of chain) that match ``req`` for ``caster``. Shared by satisfiability
    and the engine's option enumeration."""
    return [
        i for i, (actor, card) in enumerate(chain_items) if req.matches(actor, card, caster)
    ]


def _phrase_satisfiable(
    phrase: Phrase,
    units: list[UnitView],
    caster: str | None = None,
    *,
    gear_count: int = 0,
    trash_p1: list[str] | None = None,
    trash_p2: list[str] | None = None,
    chain_items: list[tuple[str, str]] | None = None,
) -> bool:
    if phrase.unknown:
        return True  # selector not handled yet → don't block the card
    if phrase.unit is not None:
        return _unit_phrase_satisfiable(phrase.unit, units, caster)
    if phrase.battlefield is not None:
        return _battlefield_phrase_satisfiable(phrase.battlefield, units, caster)
    if phrase.gear is not None:
        # Min 0 (e.g. "GEAR (0-1)") is trivially satisfiable; otherwise need
        # enough gears on the board (either player's).
        return phrase.gear.min_count <= 0 or gear_count >= phrase.gear.min_count
    if phrase.trash is not None:
        if phrase.trash.min_count <= 0:
            return True
        refs = matching_trash_refs(phrase.trash, trash_p1 or [], trash_p2 or [], caster)
        return len(refs) >= phrase.trash.min_count
    if phrase.spell is not None:
        if phrase.spell.min_count <= 0:
            return True
        refs = matching_spell_refs(phrase.spell, chain_items or [], caster)
        return len(refs) >= phrase.spell.min_count
    if phrase.location is not None:
        return True  # base + two battlefields always exist in a live game
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


def tree_satisfiable(
    tree: RequirementTree,
    units: list[UnitView],
    caster: str | None = None,
    *,
    gear_count: int = 0,
    trash_p1: list[str] | None = None,
    trash_p2: list[str] | None = None,
    chain_items: list[tuple[str, str]] | None = None,
) -> bool:
    if not tree.groups:
        return True
    group_values = [
        _fold(
            [
                _phrase_satisfiable(
                    p,
                    units,
                    caster,
                    gear_count=gear_count,
                    trash_p1=trash_p1,
                    trash_p2=trash_p2,
                    chain_items=chain_items,
                )
                for p in g.phrases
            ],
            g.connectors,
        )
        for g in tree.groups
    ]
    return _fold(group_values, tree.connectors)


def _norm_caster(caster: "str | None") -> str | None:
    """Accept a ``RequiredTo`` enum member or a controller string and return
    the plain controller string ("player_1"/"player_2"), or None."""
    if caster is None:
        return None
    value = getattr(caster, "value", caster)
    return value if value in ("player_1", "player_2") else None


def requirement_satisfiable(
    raw: str | None, state: "GameState", caster: "str | None" = None
) -> bool:
    """Whether ``raw``'s requirement has at least one valid target set now.

    ``caster`` is the casting player (a ``RequiredTo`` or controller string);
    it's needed to resolve FRIENDLY / ENEMY selectors. Empty/blank
    requirements are always satisfiable (no choice needed).
    """
    if not raw or not raw.strip():
        return True
    caster = _norm_caster(caster)
    tree = parse_tree(raw)
    gear_count = len(getattr(state, "player_1_gears", None) or []) + len(
        getattr(state, "player_2_gears", None) or []
    )
    trash_p1 = list(getattr(state, "player_1_trash", None) or [])
    trash_p2 = list(getattr(state, "player_2_trash", None) or [])
    chain = getattr(state, "pending_chain", None)
    chain_items = (
        [(getattr(it.actor, "value", it.actor), it.card) for it in chain.items]
        if chain is not None
        else []
    )
    if not tree_satisfiable(
        tree,
        board_units(state),
        caster,
        gear_count=gear_count,
        trash_p1=trash_p1,
        trash_p2=trash_p2,
        chain_items=chain_items,
    ):
        return False
    # A pure-AND requirement that forces more than one pick additionally needs
    # a DISTINCT unit for each pick — tree_satisfiable only checks each phrase
    # in isolation, so confirm a full disjoint assignment exists before the
    # card is offered (otherwise the caster could soft-lock mid-selection).
    plan = spell_target_plan(raw)
    if len(plan) > 1:
        return plan_feasible(plan, state, caster)
    return True


def spell_playable(state: "GameState", card: str, caster: "str | None" = None) -> bool:
    """True if ``card``'s Spell Choice Requirement can be met on the board.

    Convenience wrapper used by the engine's action generator: looks up the
    card's raw requirement from the CSV and evaluates it. ``caster`` is the
    casting player, needed for FRIENDLY / ENEMY selectors. Cards with no
    requirement are always playable (subject to the engine's cost gates,
    which are checked separately).
    """
    from .csv_data import card_spell_requirement_of

    return requirement_satisfiable(card_spell_requirement_of(card), state, caster)


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


def _selectable_phrase_req(phrase: Phrase) -> UnitRequirement | None:
    """The UnitRequirement for a phrase that forces an explicit pick, or None.

    A phrase forces a pick when it's a handled ANY-UNIT requirement with a
    minimum of at least 1 and isn't impossible. Unknown selectors (GEAR,
    FRIENDLY UNIT, …), min-0 phrases (``n`` / ``0-N``), and EQUIPMENT-style
    impossible phrases never force a pick.
    """
    if phrase.unit is None or phrase.unknown or phrase.unit.impossible:
        return None
    if phrase.unit.min_count < 1:
        return None
    return phrase.unit


def selectable_unit_requirement(raw: str | None) -> UnitRequirement | None:
    """Return the single ANY-UNIT requirement that needs an explicit pick.

    Fires only for a requirement that parses to EXACTLY one group with one
    unit phrase whose minimum is >= 1 (so a real choice is forced) and isn't
    impossible. Anything else — blank, min-0 (``n``/``0-N``), an unknown
    selector, or a multi-phrase/multi-group tree — returns ``None``.

    For multi-phrase ANY-UNIT requirements use :func:`spell_target_plan`,
    which returns one requirement per pick the caster must make.
    """
    if not raw or not raw.strip():
        return None
    tree = parse_tree(raw)
    if len(tree.groups) != 1 or len(tree.groups[0].phrases) != 1:
        return None
    return _selectable_phrase_req(tree.groups[0].phrases[0])


def spell_target_plan(raw: str | None) -> list[UnitRequirement]:
    """Ordered list of the ANY-UNIT picks a spell forces the caster to make.

    Each entry is a :class:`UnitRequirement` the caster must satisfy with a
    distinct set of units, in order. The plan is non-empty only for a *pure
    conjunction* (every connector — within and between groups — is ``AND``)
    that contains at least one pick-forcing ANY-UNIT phrase; phrases that
    don't force a pick (unknown selectors like GEAR, min-0 phrases, EQUIPMENT)
    are simply omitted from the plan.

    Anything involving an OR connector returns ``[]`` — the caster would have
    to choose *which* branch to satisfy, which the choice system doesn't model
    yet, so those spells commit immediately the way they always have.

    Single-phrase requirements yield a one-entry plan, matching
    :func:`selectable_unit_requirement`.
    """
    if not raw or not raw.strip():
        return []
    tree = parse_tree(raw)
    if any(c == "OR" for c in tree.connectors):
        return []
    plan: list[UnitRequirement] = []
    for group in tree.groups:
        if any(c == "OR" for c in group.connectors):
            return []
        for phrase in group.phrases:
            req = _selectable_phrase_req(phrase)
            if req is not None:
                plan.append(req)
    return plan


def battlefield_picks(raw: str | None) -> list[BattlefieldRequirement]:
    """Ordered BATTLEFIELD requirements a spell forces the caster to pick.

    One entry per BATTLEFIELD phrase, in tree order. Like the unit plan, this
    is only populated for a pure conjunction (no OR), so OR'd requirements
    commit immediately as they always have.
    """
    if not raw or not raw.strip():
        return []
    tree = parse_tree(raw)
    if any(c == "OR" for c in tree.connectors):
        return []
    out: list[BattlefieldRequirement] = []
    for group in tree.groups:
        if any(c == "OR" for c in group.connectors):
            return []
        for phrase in group.phrases:
            if phrase.battlefield is not None:
                out.append(phrase.battlefield)
    return out


def gear_picks(raw: str | None) -> list[GearRequirement]:
    """Ordered GEAR requirements a spell forces the caster to pick (min >= 1).

    Optional gear phrases (``GEAR (0-1)``, min 0) and OR'd requirements force
    no pick, mirroring the unit plan.
    """
    if not raw or not raw.strip():
        return []
    tree = parse_tree(raw)
    if any(c == "OR" for c in tree.connectors):
        return []
    out: list[GearRequirement] = []
    for group in tree.groups:
        if any(c == "OR" for c in group.connectors):
            return []
        for phrase in group.phrases:
            if phrase.gear is not None and phrase.gear.min_count >= 1:
                out.append(phrase.gear)
    return out


def trash_picks(raw: str | None) -> list[TrashRequirement]:
    """Ordered TRASH requirements a spell forces the caster to pick (min >= 1).

    Optional (min 0) and OR'd requirements force no pick, mirroring the others.
    """
    if not raw or not raw.strip():
        return []
    tree = parse_tree(raw)
    if any(c == "OR" for c in tree.connectors):
        return []
    out: list[TrashRequirement] = []
    for group in tree.groups:
        if any(c == "OR" for c in group.connectors):
            return []
        for phrase in group.phrases:
            if phrase.trash is not None and phrase.trash.min_count >= 1:
                out.append(phrase.trash)
    return out


def spell_picks(raw: str | None) -> list[SpellRequirement]:
    """Ordered SPELL requirements a spell forces the caster to pick (min >= 1).

    OR'd requirements force no pick (the caster would choose a branch, which the
    choice system doesn't model), mirroring the other pick helpers.
    """
    if not raw or not raw.strip():
        return []
    tree = parse_tree(raw)
    if any(c == "OR" for c in tree.connectors):
        return []
    out: list[SpellRequirement] = []
    for group in tree.groups:
        if any(c == "OR" for c in group.connectors):
            return []
        for phrase in group.phrases:
            if phrase.spell is not None and phrase.spell.min_count >= 1:
                out.append(phrase.spell)
    return out


def location_picks(raw: str | None) -> list[LocationRequirement]:
    """Ordered LOCATION requirements a spell forces the caster to pick.

    One per LOCATION phrase, in tree order; OR'd requirements force no pick.
    """
    if not raw or not raw.strip():
        return []
    tree = parse_tree(raw)
    if any(c == "OR" for c in tree.connectors):
        return []
    out: list[LocationRequirement] = []
    for group in tree.groups:
        if any(c == "OR" for c in group.connectors):
            return []
        for phrase in group.phrases:
            if phrase.location is not None:
                out.append(phrase.location)
    return out


def moved_unit_refs(raw: str | None, chosen: list[list[str]]) -> list[TargetRef]:
    """The (controller, index) refs the caster has picked for MOVE phrases,
    in pick order. Each one still needs a destination location chosen for it.

    ``chosen`` is ``PendingSpellChoice.chosen`` — one inner list of wire tokens
    per resolved phrase, aligned with ``spell_target_plan(raw)``.
    """
    plan = spell_target_plan(raw)
    out: list[TargetRef] = []
    for i, picks in enumerate(chosen):
        if i < len(plan) and plan[i].move:
            out.extend(token_to_ref(tok) for tok in picks)
    return out


def plan_feasible(
    plan: list[UnitRequirement],
    state: "GameState",
    caster: "str | None" = None,
    used: "frozenset[TargetRef] | set[TargetRef]" = frozenset(),
) -> bool:
    """Whether every requirement in ``plan`` can be satisfied with DISTINCT
    units simultaneously (no unit reused across picks).

    Backtracking assignment: try each valid target set for the first
    requirement, mark its units used, recurse on the rest. ``caster`` resolves
    FRIENDLY / ENEMY scopes; ``used`` seeds the refs already committed
    (mid-selection). Boards are tiny so the search is cheap;
    ``enumerate_unit_target_sets``'s cap bounds each level.
    """
    if not plan:
        return True
    head, rest = plan[0], plan[1:]
    used = set(used)
    for combo in enumerate_unit_target_sets(head, state, caster=caster, exclude=used):
        if plan_feasible(rest, state, caster, used | set(combo)):
            return True
    return False


def _group_ok(req: UnitRequirement, combo: tuple[UnitView, ...]) -> bool:
    if req.same_location and len({u.location for u in combo}) > 1:
        return False
    if req.sum_might_max is not None and sum(u.might for u in combo) > req.sum_might_max:
        return False
    return True


def enumerate_unit_target_sets(
    req: UnitRequirement,
    state: "GameState",
    *,
    caster: "str | None" = None,
    cap: int = 256,
    exclude: "frozenset[TargetRef] | set[TargetRef]" = frozenset(),
) -> list[tuple[TargetRef, ...]]:
    """All valid target sets for ``req`` on the current board.

    Each result is a tuple of ``(controller, index)`` refs. Set sizes range
    over ``[min_count, max_count]`` (``max_count`` None ⇒ up to every
    matching unit). ``caster`` resolves FRIENDLY / ENEMY scopes; per-unit
    filters are applied first; group constraints (SAME_LOC / SUM) are checked
    per candidate set. Capped at ``cap`` sets.

    ``exclude`` is a set of refs already committed to earlier phrases of a
    multi-phrase requirement — they're filtered out so the same unit can't
    satisfy two different picks of the same spell.
    """
    import itertools

    caster = _norm_caster(caster)
    matching = [
        u
        for u in board_units(state)
        if req.unit_matches(u, caster) and (u.controller, u.index) not in exclude
    ]
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
    req: UnitRequirement,
    state: "GameState",
    refs: list[TargetRef],
    *,
    caster: "str | None" = None,
    exclude: "frozenset[TargetRef] | set[TargetRef]" = frozenset(),
) -> bool:
    """Validate a concrete chosen set of refs against ``req``.

    ``caster`` resolves FRIENDLY / ENEMY scopes. ``exclude`` holds refs
    already committed to earlier phrases of the same multi-phrase requirement;
    reusing one of them is rejected.
    """
    caster = _norm_caster(caster)
    units = board_units(state)
    by_ref = {(u.controller, u.index): u for u in units}
    if len(set(refs)) != len(refs):
        return False  # no duplicate targets
    if any(r in exclude for r in refs):
        return False  # already used by an earlier phrase
    if not (req.min_count <= len(refs) and (req.max_count is None or len(refs) <= req.max_count)):
        return False
    chosen: list[UnitView] = []
    for r in refs:
        u = by_ref.get(r)
        if u is None or not req.unit_matches(u, caster):
            return False
        chosen.append(u)
    return _group_ok(req, tuple(chosen))
