"""Load card data from `riftbound_cards.csv` next to the engine package root."""

from __future__ import annotations

import csv
import random
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


@lru_cache(maxsize=1)
def _csv_rows_raw() -> list[list[str]]:
    if not _CSV_PATH.is_file():
        return []
    with _CSV_PATH.open(encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


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
