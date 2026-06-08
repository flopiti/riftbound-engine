"""Load the card taxonomy (triggered abilities authored in the wizard) and
expose it keyed by the card *name* the engine plays with.

The wizard writes ``riftbound/data/card_taxonomy.json`` (a sibling project),
whose ``assignments`` map is keyed by card **ID**. The engine, however,
identifies cards by **name** (``PlayedUnit.card``). This module bridges the
two: it reads the CSV's ID↔Name columns to translate the taxonomy's
id-keyed assignments into a name-keyed lookup.

If the taxonomy file is absent (e.g. in a checkout without the front-end, or
in CI), every lookup simply returns no abilities — the engine runs exactly as
before, just with no triggers firing.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .csv_data import _ENGINE_ROOT, _csv_rows_raw, _header_index

#: Default taxonomy location (overridable via ``RIFTBOUND_TAXONOMY_PATH`` —
#: handy for tests and alternative checkouts).
_DEFAULT_TAXONOMY_PATH = _ENGINE_ROOT.parent / "riftbound" / "data" / "card_taxonomy.json"


def _taxonomy_path() -> Path:
    override = os.environ.get("RIFTBOUND_TAXONOMY_PATH")
    return Path(override) if override else _DEFAULT_TAXONOMY_PATH


@dataclass(frozen=True)
class Ability:
    """One triggered/continuous ability bundle, mirroring the wizard's shape.

    Tuples (not lists) so instances are hashable and safely shared/cached.
    """

    triggers: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    costs: tuple[str, ...] = ()
    active_effects: tuple[str, ...] = ()
    passive_effects: tuple[str, ...] = ()
    #: True ⇒ EFFECT text: only active while the card is attached to another
    #: unit (appends to the host's rules — e.g. equipment). False ⇒ RULE text:
    #: always active while the card is in play. The engine will use this to
    #: decide whether an equipment's ability applies to its equipped unit.
    effect_text: bool = False

    @property
    def is_empty(self) -> bool:
        return not (
            self.triggers
            or self.conditions
            or self.costs
            or self.active_effects
            or self.passive_effects
        )


def _ability_from_dict(d: dict) -> Ability:
    def _seq(key: str) -> tuple[str, ...]:
        val = d.get(key) or []
        return tuple(str(x) for x in val) if isinstance(val, list) else ()

    return Ability(
        triggers=_seq("triggers"),
        conditions=_seq("conditions"),
        costs=_seq("costs"),
        active_effects=_seq("activeEffects"),
        passive_effects=_seq("passiveEffects"),
        effect_text=bool(d.get("effectText")),
    )


@lru_cache(maxsize=1)
def _id_to_name() -> dict[str, str]:
    """Map CSV card ID → card Name (using the same canonicalized rows the rest
    of the engine reads, so dedup/backfill stays consistent)."""
    rows = _csv_rows_raw()
    if len(rows) < 2:
        return {}
    header = rows[0]
    i_id = _header_index(header, "ID")
    i_name = _header_index(header, "Name")
    if i_id is None or i_name is None:
        return {}
    out: dict[str, str] = {}
    for parts in rows[1:]:
        if len(parts) <= max(i_id, i_name):
            continue
        cid = parts[i_id].strip()
        name = parts[i_name].strip()
        if cid and name and cid not in out:
            out[cid] = name
    return out


@lru_cache(maxsize=1)
def _name_to_abilities() -> dict[str, tuple[Ability, ...]]:
    """Lowercased card name → its non-empty abilities, from the taxonomy file."""
    path = _taxonomy_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    assignments = raw.get("assignments")
    if not isinstance(assignments, dict):
        return {}
    id_to_name = _id_to_name()
    out: dict[str, tuple[Ability, ...]] = {}
    for cid, payload in assignments.items():
        name = id_to_name.get(str(cid).strip())
        if not name:
            continue
        ability_dicts = (payload or {}).get("abilities") if isinstance(payload, dict) else None
        if not isinstance(ability_dicts, list):
            continue
        abilities = tuple(
            a for a in (_ability_from_dict(d) for d in ability_dicts if isinstance(d, dict))
            if not a.is_empty
        )
        if abilities:
            out[name.strip().lower()] = abilities
    return out


def triggered_abilities_for(name: str) -> tuple[Ability, ...]:
    """Abilities tagged on the card ``name`` (case-insensitive), or ``()``.

    Uses the same name-resolution fallback as :func:`csv_data.card_type_of`
    so a deck's "Miss Fortune, Bounty Hunter" matches the CSV row
    "Bounty Hunter".
    """
    if not name:
        return ()
    index = _name_to_abilities()
    direct = index.get(name.strip().lower())
    if direct is not None:
        return direct
    sep = name.find(", ")
    if sep >= 0:
        return index.get(name[sep + 2 :].strip().lower(), ())
    return ()


#: An EFFECT-TEXT passive that buffs/debuffs the host unit's Might while the
#: equipment is attached, e.g. "UNIT_ATTACHED_+2M" → +2, "UNIT_ATTACHED_-1M" → -1.
_ATTACH_MIGHT_RE = re.compile(r"UNIT_ATTACHED_([+-]?\d+)M\b")


def _gear_conditions_met(ability: Ability, gear, current_turn: int | None) -> bool:
    """Whether a continuous equipment ability's conditions currently hold for
    ``gear``. An ability with no conditions always applies. A condition we
    can't evaluate (not implemented) is treated as NOT met, so we never apply a
    continuous effect whose gate we can't verify.

    Implemented conditions:
      * ``ATTACHED_THIS_TURN`` — the gear was (re-)attached on the current turn.
    """
    for cond in ability.conditions:
        if cond == "ATTACHED_THIS_TURN":
            if current_turn is None or getattr(gear, "attached_on_turn", None) != current_turn:
                return False
        else:
            return False  # unknown condition → can't verify → don't apply
    return True


def attached_might_bonus(gears, host_uid: int | None, current_turn: int | None = None) -> int:
    """Sum the Might that EFFECT-TEXT passive abilities on equipment attached to
    the unit whose stable uid is ``host_uid`` grant that unit.

    ``gears`` is that unit's controller's ``PlayedGear`` list (a gear can only
    be equipped onto a unit its owner controls). Matching by uid — not by a
    positional ref — means the buff follows the right unit even after others
    leave play and shift indices. Only ``effect_text`` abilities count — a
    rule-text ability would apply on its own, not via attachment — and only
    when the ability's conditions currently hold (see ``_gear_conditions_met``),
    so e.g. Brutalizer's conditional +2 Might applies only the turn it's
    attached. ``current_turn`` is ``GameState.total_turn_number``."""
    if not host_uid:
        return 0
    total = 0
    for g in gears or []:
        if getattr(g, "attached_uid", None) != host_uid:
            continue
        for ability in triggered_abilities_for(g.card):
            if not ability.effect_text:
                continue
            if not _gear_conditions_met(ability, g, current_turn):
                continue
            for code in ability.passive_effects:
                m = _ATTACH_MIGHT_RE.search(code or "")
                if m:
                    total += int(m.group(1))
    return total


def reset_caches() -> None:
    """Clear memoized lookups (used by tests that swap the taxonomy file)."""
    _id_to_name.cache_clear()
    _name_to_abilities.cache_clear()
