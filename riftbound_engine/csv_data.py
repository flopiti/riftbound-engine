"""Load card data from `riftbound_cards.csv` next to the engine package root."""

from __future__ import annotations

import csv
import random
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
_CSV_PATH = _ENGINE_ROOT / "riftbound_cards.csv"

MAIN_DECK_CARD_TYPES = frozenset({"Unit", "Spell", "Gear"})
SKIP_RARITIES = frozenset({"Showcase"})
MAIN_DECK_SIZE = 39
RUNE_DECK_SIZE = 12
MAX_COPIES_PER_CARD = 3


@dataclass(frozen=True)
class CsvCard:
    name: str
    card_type: str
    domain: str
    rarity: str


# An "ID-suffix" alternate art: a base id like 'ogn-079' printed again as
# 'ogn-079a' (different art, Showcase rarity). The base group is captured so
# we can confirm the base row actually exists before dropping the alt.
_ALT_ID_RE = re.compile(r"^([a-z]+-\d+)[a-z]$")


def _backfill_row(target: list[str], source: list[str]) -> None:
    """Copy a value from ``source`` into any *empty* cell of ``target``.

    Never overwrites a value the target already has, so a complete base row is
    never corrupted by a less-reliable alt's data — it only gains values the
    base happened to leave blank. This mirrors the engine's historic
    first-non-blank-per-field lookup behaviour."""
    for j, val in enumerate(source):
        if j < len(target) and not target[j].strip() and val.strip():
            target[j] = val


def _canonicalize_rows(rows: list[list[str]]) -> list[list[str]]:
    """Collapse alternate-art rows so only base cards remain.

    ``riftbound_cards.csv`` ships three flavours of alternate art, removed in
    two passes:

    1. **By id** —
       * *suffix alts*: an id like ``ogn-079a`` whose base ``ogn-079`` also
         appears (always Showcase, e.g. "Diana, Lunari").
       * *duplicate-id alts*: the *same* id printed twice with different art
         (e.g. the ``ogn-299``+ Legends), differing only by Image URL.
    2. **By name** — a card reprinted under a *different* id with the same
       name, almost always a higher-numbered Showcase variant (e.g.
       "Seal of Rage" ogn-040 + sfd-222). The Showcase reprints are frequently
       incomplete scrapes (truncated ability text, missing stats), so the base
       (non-Showcase, else first-seen) printing is authoritative.

    In both passes we keep the base row and drop the alts, backfilling any empty
    cell of the kept row from a dropped one so no scalar data is lost. This is
    the single chokepoint every engine lookup flows through, so the whole engine
    operates on base cards only.
    """
    if len(rows) < 2:
        return rows
    header = rows[0]
    try:
        i_id = header.index("ID")
    except ValueError:
        return rows  # no ID column → nothing to dedupe on
    i_name = header.index("Name") if "Name" in header else None
    i_rarity = header.index("Rarity") if "Rarity" in header else None
    all_ids = {
        parts[i_id].strip() for parts in rows[1:] if len(parts) > i_id and parts[i_id].strip()
    }

    # --- Pass 1: collapse by id (suffix alts + duplicate ids) ---
    kept: dict[str, list[str]] = {}
    order: list[str] = []
    passthrough: list[list[str]] = []  # rows without an id (kept as-is)
    for parts in rows[1:]:
        sid = parts[i_id].strip() if len(parts) > i_id else ""
        if not sid:
            passthrough.append(list(parts))
            continue
        m = _ALT_ID_RE.match(sid)
        if m and m.group(1) in all_ids:
            base = kept.get(m.group(1))
            if base is not None:
                _backfill_row(base, parts)
            continue  # suffix alt of an existing base → drop
        if sid in kept:
            _backfill_row(kept[sid], parts)
            continue  # duplicate-id alt → drop
        kept[sid] = list(parts)
        order.append(sid)
    id_rows = [kept[s] for s in order]

    # --- Pass 2: collapse by name (cross-id reprints / Showcase variants) ---
    if i_name is not None:
        def _is_showcase(r: list[str]) -> bool:
            return i_rarity is not None and len(r) > i_rarity and r[i_rarity].strip() == "Showcase"

        base_for: dict[str, list[str]] = {}
        for r in id_rows:
            name = r[i_name].strip() if len(r) > i_name else ""
            if not name:
                continue  # unnamed rows aren't deduped; emitted verbatim below
            cur = base_for.get(name)
            if cur is None:
                base_for[name] = r
            elif _is_showcase(cur) and not _is_showcase(r):
                # A non-Showcase printing trumps a Showcase one already seen.
                _backfill_row(r, cur)
                base_for[name] = r  # replace the chosen base in place
            else:
                _backfill_row(cur, r)  # keep existing base → drop this reprint
        # Emit one row per name in first-seen order; unnamed rows are kept
        # verbatim in their original order.
        emitted: set[str] = set()
        id_rows = []
        for r in (kept[s] for s in order):
            name = r[i_name].strip() if len(r) > i_name else ""
            if not name:
                id_rows.append(r)
                continue
            chosen = base_for[name]
            if name not in emitted:
                emitted.add(name)
                id_rows.append(chosen)

    return [header] + id_rows + passthrough


@lru_cache(maxsize=1)
def _csv_rows_raw() -> list[list[str]]:
    if not _CSV_PATH.is_file():
        return []
    with _CSV_PATH.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    return _canonicalize_rows(rows)


def _header_index(header: list[str], name: str) -> int | None:
    try:
        return header.index(name)
    except ValueError:
        return None


@lru_cache(maxsize=1)
def csv_cards() -> tuple[CsvCard, ...]:
    rows = _csv_rows_raw()
    if len(rows) < 2:
        return ()
    header = rows[0]
    i_name = _header_index(header, "Name")
    i_type = _header_index(header, "Card Type")
    i_domain = _header_index(header, "Domain")
    i_rarity = _header_index(header, "Rarity")
    if i_name is None or i_type is None or i_domain is None or i_rarity is None:
        return ()
    out: list[CsvCard] = []
    for parts in rows[1:]:
        if len(parts) <= max(i_name, i_type, i_domain, i_rarity):
            continue
        name = parts[i_name].strip()
        card_type = parts[i_type].strip()
        if not name or not card_type:
            continue
        out.append(
            CsvCard(
                name=name,
                card_type=card_type,
                domain=parts[i_domain].strip(),
                rarity=parts[i_rarity].strip(),
            )
        )
    return tuple(out)


def parse_domains(domain_field: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in domain_field.split(",") if part.strip())


def card_matches_domains(domain_field: str, allowed: frozenset[str]) -> bool:
    domains = parse_domains(domain_field)
    return bool(domains) and any(domain in allowed for domain in domains)


@lru_cache(maxsize=1)
def _csv_card_type_index() -> dict[str, str]:
    """Lowercased card name → CSV `Card Type` (e.g. 'Unit', 'Spell', 'Gear', 'Battlefield', 'Rune', 'Legend')."""
    out: dict[str, str] = {}
    for card in csv_cards():
        key = card.name.strip().lower()
        if key and key not in out:
            out[key] = card.card_type
    return out


def card_type_of(name: str) -> str | None:
    """Look up the Card Type for a card by name (case-insensitive).

    Mirrors :func:`riftbound_engine.deck_files.resolve_card_name` so the engine
    matches whatever form the deck file used: tries the full name first, then
    the suffix after the first ', ' (handles "Miss Fortune, Bounty Hunter" →
    CSV name "Bounty Hunter"). Returns ``None`` if the card isn't in the CSV.
    """
    if not name:
        return None
    index = _csv_card_type_index()
    direct = index.get(name.strip().lower())
    if direct is not None:
        return direct
    sep = name.find(", ")
    if sep >= 0:
        return index.get(name[sep + 2 :].strip().lower())
    return None


@lru_cache(maxsize=1)
def _csv_card_energy_index() -> dict[str, int]:
    """Lowercased card name → CSV `Energy` cost as an integer.

    Names whose ``Energy`` cell is blank or non-numeric are skipped; non-playable
    types like Battlefield/Rune/Legend usually have no Energy. Use
    :func:`card_energy_of` to look up by name with the same fallback rules as
    :func:`card_type_of`.
    """
    rows = _csv_rows_raw()
    if len(rows) < 2:
        return {}
    header = rows[0]
    i_name = _header_index(header, "Name")
    i_energy = _header_index(header, "Energy")
    if i_name is None or i_energy is None:
        return {}
    out: dict[str, int] = {}
    for parts in rows[1:]:
        if len(parts) <= max(i_name, i_energy):
            continue
        name = parts[i_name].strip().lower()
        energy_raw = parts[i_energy].strip()
        if not name or not energy_raw:
            continue
        try:
            value = int(energy_raw)
        except ValueError:
            continue
        if name not in out:
            out[name] = value
    return out


def card_energy_of(name: str) -> int | None:
    """Look up the Energy (cost) for a card by name (case-insensitive).

    Returns ``None`` if the card isn't in the CSV or has no numeric Energy
    (e.g. Battlefield rows). Uses the same name-resolution fallback as
    :func:`card_type_of` so "Miss Fortune, Bounty Hunter" matches the CSV
    row "Bounty Hunter".
    """
    if not name:
        return None
    index = _csv_card_energy_index()
    direct = index.get(name.strip().lower())
    if direct is not None:
        return direct
    sep = name.find(", ")
    if sep >= 0:
        return index.get(name[sep + 2 :].strip().lower())
    return None


@lru_cache(maxsize=1)
def _csv_card_power_index() -> dict[str, int]:
    """Lowercased card name → CSV ``Power`` cost as an integer.

    The Power column is the number of domain-Power pips the card requires
    in addition to Energy. Cards with a blank or non-numeric Power cell are
    skipped (they have no Power requirement).
    """
    rows = _csv_rows_raw()
    if len(rows) < 2:
        return {}
    header = rows[0]
    i_name = _header_index(header, "Name")
    i_power = _header_index(header, "Power")
    if i_name is None or i_power is None:
        return {}
    out: dict[str, int] = {}
    for parts in rows[1:]:
        if len(parts) <= max(i_name, i_power):
            continue
        name = parts[i_name].strip().lower()
        power_raw = parts[i_power].strip()
        if not name or not power_raw:
            continue
        try:
            value = int(power_raw)
        except ValueError:
            continue
        if name not in out:
            out[name] = value
    return out


@lru_cache(maxsize=1)
def _csv_card_might_index() -> dict[str, int]:
    """Lowercased card name → CSV ``Might`` as an integer.

    Might is the "combat strength" of a Unit — the damage it both deals
    and can absorb in a showdown. Non-Unit rows (Spell, Battlefield, …)
    have a blank Might and are skipped. We don't accept partial / decimal
    might — any non-integer Might row is treated as missing.
    """
    rows = _csv_rows_raw()
    if len(rows) < 2:
        return {}
    header = rows[0]
    i_name = _header_index(header, "Name")
    i_might = _header_index(header, "Might")
    if i_name is None or i_might is None:
        return {}
    out: dict[str, int] = {}
    for parts in rows[1:]:
        if len(parts) <= max(i_name, i_might):
            continue
        name = parts[i_name].strip().lower()
        might_raw = parts[i_might].strip()
        if not name or not might_raw:
            continue
        try:
            value = int(might_raw)
        except ValueError:
            continue
        if name not in out:
            out[name] = value
    return out


def card_might_of(name: str) -> int | None:
    """Look up the Might (combat strength) of a unit by name (case-insensitive).

    Returns ``None`` if the card isn't in the CSV or has no numeric Might
    (e.g. Spell / Battlefield / Rune rows). Uses the same name-resolution
    fallback as :func:`card_type_of` so "Garen, Rugged" matches the CSV
    row "Garen, Rugged" or its bare-name form if any.
    """
    if not name:
        return None
    index = _csv_card_might_index()
    direct = index.get(name.strip().lower())
    if direct is not None:
        return direct
    sep = name.find(", ")
    if sep >= 0:
        return index.get(name[sep + 2 :].strip().lower())
    return None


def card_power_of(name: str) -> int | None:
    """Look up the Power (cost) for a card by name (case-insensitive).

    Returns ``None`` if the card isn't in the CSV or has no numeric Power
    (most cards have no Power requirement — only Energy). Uses the same
    name-resolution fallback as :func:`card_type_of`.
    """
    if not name:
        return None
    index = _csv_card_power_index()
    direct = index.get(name.strip().lower())
    if direct is not None:
        return direct
    sep = name.find(", ")
    if sep >= 0:
        return index.get(name[sep + 2 :].strip().lower())
    return None


@lru_cache(maxsize=1)
def _csv_card_requirement_index() -> dict[str, str]:
    """Lowercased card name → raw CSV ``Spell Choice Requirement`` value.

    The ``Spell Choice Requirement`` column (the last column of
    ``riftbound_cards.csv``) encodes, for a Spell, the UNIT/GEAR/etc.
    choices a player is REQUIRED to make in order to play that card. The
    raw string uses the pipe-run grammar authored by the Spell Wizard
    (``|`` AND-within-group, ``||`` OR-within-group, ``|||`` AND-between-
    groups, ``||||`` OR-between-groups). Blank cells (no requirement) are
    skipped. See :mod:`riftbound_engine.requirements` for parsing/eval.
    """
    rows = _csv_rows_raw()
    if len(rows) < 2:
        return {}
    header = rows[0]
    i_name = _header_index(header, "Name")
    i_req = _header_index(header, "Spell Choice Requirement")
    if i_name is None or i_req is None:
        return {}
    out: dict[str, str] = {}
    for parts in rows[1:]:
        if len(parts) <= max(i_name, i_req):
            continue
        name = parts[i_name].strip().lower()
        req_raw = parts[i_req].strip()
        if not name or not req_raw:
            continue
        if name not in out:
            out[name] = req_raw
    return out


def card_spell_requirement_of(name: str) -> str | None:
    """Look up the raw ``Spell Choice Requirement`` string for a card by name.

    Returns ``None`` if the card isn't in the CSV or has a blank
    requirement cell. Uses the same name-resolution fallback as
    :func:`card_type_of` so "Miss Fortune, Bounty Hunter" matches the CSV
    row "Bounty Hunter".
    """
    if not name:
        return None
    index = _csv_card_requirement_index()
    direct = index.get(name.strip().lower())
    if direct is not None:
        return direct
    sep = name.find(", ")
    if sep >= 0:
        return index.get(name[sep + 2 :].strip().lower())
    return None


@lru_cache(maxsize=1)
def _csv_card_ability_index() -> dict[str, str]:
    """Lowercased card name → raw CSV ``Ability`` text."""
    rows = _csv_rows_raw()
    if len(rows) < 2:
        return {}
    header = rows[0]
    i_name = _header_index(header, "Name")
    i_ability = _header_index(header, "Ability")
    if i_name is None or i_ability is None:
        return {}
    out: dict[str, str] = {}
    for parts in rows[1:]:
        if len(parts) <= max(i_name, i_ability):
            continue
        name = parts[i_name].strip().lower()
        if name and name not in out:
            out[name] = parts[i_ability]
    return out


def card_ability_of(name: str) -> str:
    """Raw ``Ability`` text for a card by name (case-insensitive), or ''."""
    if not name:
        return ""
    index = _csv_card_ability_index()
    direct = index.get(name.strip().lower())
    if direct is None:
        sep = name.find(", ")
        if sep >= 0:
            direct = index.get(name[sep + 2 :].strip().lower())
    return direct or ""


def card_is_reaction(name: str) -> bool:
    """Whether the card carries the ``[Reaction]`` keyword (can be played in
    response to a spell on the chain). Read straight from the ability text so
    it doesn't depend on the derived Keywords column."""
    return "[reaction]" in card_ability_of(name).lower()


#: "[Accelerate] (You may pay 1 energy and 1 fury rune as an additional cost to
#: have me enter ready.)" — capture the energy amount, the power amount, and the
#: rune DOMAIN of the extra cost.
_ACCELERATE_RE = re.compile(
    r"\[accelerate\][^()]*\(\s*you may pay\s+(\d+)\s+energy\s+and\s+(\d+)\s+(\w+)\s+rune",
    re.IGNORECASE,
)


#: "[Shield]" / "[Shield 2]" keyword (bare = 1). Matches the printed keyword.
_SHIELD_KW_RE = re.compile(r"\[Shield(?:\s+(\d+))?\]", re.IGNORECASE)


def card_printed_shield(name: str) -> int:
    """The unit's OWN printed [Shield] amount (+Might while it defends), or 0.

    Bare ``[Shield]`` is 1, ``[Shield N]`` is N. Skips a ``[Shield]`` that the
    card GRANTS to OTHER units ("…have [Shield]" / "give … [Shield]") — that's a
    separate granted-keyword effect, not this card's own keyword."""
    ab = card_ability_of(name)
    for m in _SHIELD_KW_RE.finditer(ab):
        pre = ab[max(0, m.start() - 14) : m.start()].lower()
        if any(w in pre for w in ("have", "give", "gain")):
            continue
        return int(m.group(1) or 1)
    return 0


def shield_amount_in_text(text: str) -> int:
    """First ``[Shield N]`` amount anywhere in ``text`` (bare = 1), else 1.
    Used to size a GRANTED [Shield] from the granting card's reminder text
    (e.g. Fortified Position's "It gains [Shield 2] this turn")."""
    m = _SHIELD_KW_RE.search(text or "")
    return int(m.group(1)) if (m and m.group(1)) else 1


def card_has_accelerate(name: str) -> bool:
    """Whether the card carries the ``[Accelerate]`` keyword — pay an extra
    cost as you play it to have it enter READY instead of exhausted."""
    return "[accelerate]" in card_ability_of(name).lower()


def card_accelerate_cost(name: str) -> tuple[int, int, str] | None:
    """The Accelerate ADDITIONAL cost as ``(energy, power, domain)``, or None if
    the card has no parseable Accelerate clause. ``domain`` is Title-cased to
    match :func:`card_domains_of` (e.g. ``"Fury"``)."""
    m = _ACCELERATE_RE.search(card_ability_of(name))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), m.group(3).capitalize())


def card_is_action(name: str) -> bool:
    """Whether the card carries the ``[Action]`` keyword — playable on your
    own turn OR during a showdown. Read straight from the ability text so it
    doesn't depend on the derived Keywords column."""
    return "[action]" in card_ability_of(name).lower()


def card_playable_in_showdown(name: str) -> bool:
    """A spell can be played during a showdown iff it's an [Action] or a
    [Reaction] — those are the only timing keywords that permit it."""
    return card_is_action(name) or card_is_reaction(name)


@lru_cache(maxsize=1)
def _csv_card_tags_index() -> dict[str, str]:
    """Lowercased card name → raw CSV ``Tags`` field (may be 'Ornn, Equipment')."""
    rows = _csv_rows_raw()
    if len(rows) < 2:
        return {}
    header = rows[0]
    i_name = _header_index(header, "Name")
    i_tags = _header_index(header, "Tags")
    if i_name is None or i_tags is None:
        return {}
    out: dict[str, str] = {}
    for parts in rows[1:]:
        if len(parts) <= max(i_name, i_tags):
            continue
        name = parts[i_name].strip().lower()
        if name and name not in out:
            out[name] = parts[i_tags]
    return out


def card_tags_of(name: str) -> tuple[str, ...]:
    """Parsed list of Tags for a card (case-insensitive lookup); ``()`` if none."""
    if not name:
        return ()
    index = _csv_card_tags_index()
    raw = index.get(name.strip().lower())
    if raw is None:
        sep = name.find(", ")
        if sep >= 0:
            raw = index.get(name[sep + 2 :].strip().lower())
    if not raw:
        return ()
    return tuple(t.strip() for t in raw.split(",") if t.strip())


def card_is_equipment(name: str) -> bool:
    """A Gear-type card tagged ``Equipment`` (it carries the [Equip] keyword)."""
    return card_type_of(name) == "Gear" and any(
        t.lower() == "equipment" for t in card_tags_of(name)
    )


def card_has_quick_draw(name: str) -> bool:
    """Whether an Equipment carries ``[Quick-Draw]`` — it gains [Reaction]
    timing AND, when played this way, attaches to a unit you control for FREE
    (you pay only the card cost, not the [Equip] cost). Read from the ability
    text. (Jax, Unmatched granting Quick-Draw to all your equipment is a
    separate keyword-grant passive, not modelled here.)"""
    return card_is_equipment(name) and "[quick-draw]" in card_ability_of(name).lower()


_RUNE_DOMAINS = ("fury", "calm", "mind", "body", "chaos", "order")
_EQUIP_ENERGY_RE = re.compile(r"(\d+)\s*energy", re.IGNORECASE)
_EQUIP_DOMAIN_RE = re.compile(
    r"(\d+)\s*(fury|calm|mind|body|chaos|order)\s*rune", re.IGNORECASE
)
_EQUIP_ANY_RE = re.compile(r"(\d+)\s*runes?\s*of\s*any\s*type", re.IGNORECASE)


def card_equip_cost(name: str) -> dict[str, object] | None:
    """Parse the ``[Equip]`` cost for an Equipment gear, or ``None`` if the card
    isn't an equipment / has no parseable [Equip] cost.

    Returns ``{"energy": int, "power": {domain: int}, "any_power": int}`` where
    ``power`` is per-domain rune cost and ``any_power`` is a "rune of any type"
    count payable from any domain. Only the standard energy + rune portion is
    parsed; exotic clauses (Spend XP, Recycle, Kill a unit, …) are ignored.
    """
    if not card_is_equipment(name):
        return None
    ability = card_ability_of(name)
    low = ability.lower()
    marker = low.find("[equip]")
    if marker < 0:
        return None
    # Cost phrase = text after [Equip] up to the reminder "(" or sentence "."
    rest = ability[marker + len("[equip]") :]
    cut = len(rest)
    for ch in ("(", "."):
        idx = rest.find(ch)
        if idx >= 0:
            cut = min(cut, idx)
    phrase = rest[:cut]

    energy = sum(int(m.group(1)) for m in _EQUIP_ENERGY_RE.finditer(phrase))
    power: dict[str, int] = {}
    for m in _EQUIP_DOMAIN_RE.finditer(phrase):
        dom = m.group(2).capitalize()
        power[dom] = power.get(dom, 0) + int(m.group(1))
    any_power = sum(int(m.group(1)) for m in _EQUIP_ANY_RE.finditer(phrase))
    return {"energy": energy, "power": power, "any_power": any_power}


# --------------------------------------------------------------------------- #
# [Repeat] keyword — a spell may be cast, then (per its Repeat cost) paid for
# AGAIN to repeat its effect. The cost lives in the structured CSV column
# "Repeat Cost" as a compact DSL: space-separated tokens, each
#   <n>E         → n Energy
#   <n><Domain>  → n Power of that rune domain (Fury/Calm/Mind/Body/Chaos/Order)
#   <n>ANY       → n Power payable from any domain ("rune of any type")
# e.g. "2E 1Fury", "1E 1ANY", "1Chaos". Blank ⇒ no (supported) Repeat cost.
# --------------------------------------------------------------------------- #
_REPEAT_TOKEN_RE = re.compile(
    r"(\d+)\s*(E|ANY|FURY|CALM|MIND|BODY|CHAOS|ORDER)\b", re.IGNORECASE
)


@lru_cache(maxsize=1)
def _csv_repeat_cost_index() -> dict[str, str]:
    """Lowercased card name → raw CSV ``Repeat Cost`` field (compact DSL)."""
    rows = _csv_rows_raw()
    if len(rows) < 2:
        return {}
    header = rows[0]
    i_name = _header_index(header, "Name")
    i_rep = _header_index(header, "Repeat Cost")
    if i_name is None or i_rep is None:
        return {}
    out: dict[str, str] = {}
    for parts in rows[1:]:
        if len(parts) <= max(i_name, i_rep):
            continue
        name = parts[i_name].strip().lower()
        if name and name not in out:
            out[name] = parts[i_rep]
    return out


def card_repeat_cost(name: str) -> dict[str, object] | None:
    """Parse a card's [Repeat] cost from the structured ``Repeat Cost`` column.

    Returns the same shape as :func:`card_equip_cost` —
    ``{"energy": int, "power": {domain: int}, "any_power": int}`` — so the
    engine's existing ``can_afford_equip_cost`` / ``_deduct_equip_cost`` can
    charge it. Returns ``None`` when the card has no (supported) Repeat cost.
    """
    if not name:
        return None
    index = _csv_repeat_cost_index()
    raw = index.get(name.strip().lower())
    if raw is None:
        sep = name.find(", ")
        if sep >= 0:
            raw = index.get(name[sep + 2 :].strip().lower())
    if not raw or not raw.strip():
        return None
    energy = 0
    power: dict[str, int] = {}
    any_power = 0
    for m in _REPEAT_TOKEN_RE.finditer(raw):
        n = int(m.group(1))
        kind = m.group(2).upper()
        if kind == "E":
            energy += n
        elif kind == "ANY":
            any_power += n
        else:
            dom = kind.capitalize()
            power[dom] = power.get(dom, 0) + n
    if energy == 0 and not power and any_power == 0:
        return None
    return {"energy": energy, "power": power, "any_power": any_power}


def card_has_repeat(name: str) -> bool:
    """Whether the card has a (supported) [Repeat] cost authored."""
    return card_repeat_cost(name) is not None


@lru_cache(maxsize=1)
def _csv_card_domain_index() -> dict[str, str]:
    """Lowercased card name → raw CSV ``Domain`` field (may be 'Fury, Chaos')."""
    out: dict[str, str] = {}
    for card in csv_cards():
        key = card.name.strip().lower()
        if key and key not in out:
            out[key] = card.domain
    return out


def card_domains_of(name: str) -> tuple[str, ...]:
    """Parsed list of domains the card belongs to (case-insensitive lookup).

    Returns ``()`` if the card isn't in the CSV or has no Domain field.
    Uses the same name-resolution fallback as :func:`card_type_of` so
    "Miss Fortune, Bounty Hunter" matches the CSV row "Bounty Hunter".
    """
    if not name:
        return ()
    index = _csv_card_domain_index()
    raw = index.get(name.strip().lower())
    if raw is None:
        sep = name.find(", ")
        if sep >= 0:
            raw = index.get(name[sep + 2 :].strip().lower())
    if raw is None:
        return ()
    return parse_domains(raw)


@lru_cache(maxsize=1)
def battlefield_names_unique() -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for card in csv_cards():
        if card.card_type != "Battlefield":
            continue
        if card.name not in seen:
            seen.add(card.name)
            out.append(card.name)
    return tuple(out)


def pick_three_battlefields(seed: int) -> tuple[str, str, str]:
    names = list(battlefield_names_unique())
    if len(names) < 3:
        raise ValueError(f"need at least 3 Battlefield rows in {_CSV_PATH}")
    rng = random.Random(seed)
    picked = rng.sample(names, 3)
    return (picked[0], picked[1], picked[2])


def default_fallback_battlefields() -> tuple[str, str, str]:
    """Used when CSV is missing or invalid."""
    return ("Battlefield A", "Battlefield B", "Battlefield C")


def deck_battlefields_for_key(deck_key: str) -> tuple[str, str, str]:
    """Stable per-deck triple from CSV (distinct seeds per archetype)."""
    seeds = {"ember_vanguard": 41_001, "tide_wardens": 41_002}
    seed = seeds.get(deck_key, hash(deck_key) % (2**31))
    try:
        return pick_three_battlefields(seed)
    except ValueError:
        return default_fallback_battlefields()


def _prefer_card(existing: CsvCard | None, candidate: CsvCard) -> CsvCard:
    if existing is None:
        return candidate
    if existing.rarity in SKIP_RARITIES and candidate.rarity not in SKIP_RARITIES:
        return candidate
    return existing


def _main_deck_pool(domains: frozenset[str]) -> list[str]:
    by_name: dict[str, CsvCard] = {}
    for card in csv_cards():
        if card.card_type not in MAIN_DECK_CARD_TYPES:
            continue
        if not card_matches_domains(card.domain, domains):
            continue
        by_name[card.name] = _prefer_card(by_name.get(card.name), card)
    return sorted(by_name.keys())


def build_main_deck_cards(domains: frozenset[str], seed: int, size: int = MAIN_DECK_SIZE) -> tuple[str, ...]:
    pool = _main_deck_pool(domains)
    if not pool:
        raise ValueError(f"no main-deck cards for domains {sorted(domains)} in {_CSV_PATH}")
    rng = random.Random(seed)
    shuffled = list(pool)
    rng.shuffle(shuffled)
    cards: list[str] = []
    counts: Counter[str] = Counter()
    pool_index = 0
    guard = 0
    while len(cards) < size:
        guard += 1
        if guard > size * len(pool) * 2:
            raise ValueError(f"could not build {size}-card deck from pool of {len(pool)} names")
        name = shuffled[pool_index % len(shuffled)]
        pool_index += 1
        if counts[name] >= MAX_COPIES_PER_CARD:
            continue
        cards.append(name)
        counts[name] += 1
    return tuple(cards)


def _pick_named_card(
    card_type: str,
    domains: frozenset[str],
    seed: int,
    *,
    pool_filter: Callable[[CsvCard], bool] | None = None,
) -> str:
    matches: list[CsvCard] = []
    for card in csv_cards():
        if card.card_type != card_type:
            continue
        if card.rarity in SKIP_RARITIES:
            continue
        if not card_matches_domains(card.domain, domains):
            continue
        if pool_filter is not None and not pool_filter(card):
            continue
        matches.append(card)
    if not matches:
        raise ValueError(f"no {card_type} for domains {sorted(domains)} in {_CSV_PATH}")
    rng = random.Random(seed)
    return rng.choice(matches).name


def pick_legend(domains: frozenset[str], seed: int) -> str:
    return _pick_named_card("Legend", domains, seed)


def pick_chosen_champion(domains: frozenset[str], seed: int) -> str:
    def is_unit(card: CsvCard) -> bool:
        return card.card_type == "Unit"

    return _pick_named_card("Unit", domains, seed, pool_filter=is_unit)


def build_runes(domain_counts: list[tuple[str, int]], seed: int) -> list[dict[str, str]]:
    total = sum(count for _, count in domain_counts)
    if total != RUNE_DECK_SIZE:
        raise ValueError(f"rune counts must sum to {RUNE_DECK_SIZE}, got {total}")
    rng = random.Random(seed)
    runes: list[dict[str, str]] = []
    for domain, count in domain_counts:
        runes.extend({"domain": domain} for _ in range(count))
    rng.shuffle(runes)
    return runes


@dataclass(frozen=True)
class CsvDeckProfile:
    chosen_champion: str
    legend: str
    cards: tuple[str, ...]
    runes: list[dict[str, str]]


def build_csv_deck_profile(
    deck_key: str,
    *,
    domains: frozenset[str],
    rune_counts: list[tuple[str, int]],
    seed: int,
) -> CsvDeckProfile:
    return CsvDeckProfile(
        chosen_champion=pick_chosen_champion(domains, seed + 11),
        legend=pick_legend(domains, seed + 17),
        cards=build_main_deck_cards(domains, seed + 23),
        runes=build_runes(rune_counts, seed + 29),
    )
