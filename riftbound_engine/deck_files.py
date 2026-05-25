"""Load deck lists from ``riftbound-engine/decks/*.txt`` files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .csv_data import csv_cards

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
DECKS_DIR = _ENGINE_ROOT / "decks"

_SECTION_ALIASES: dict[str, str] = {
    # canonical
    "legend": "legend",
    "champion": "champion",
    "maindeck": "main_deck",
    "main deck": "main_deck",
    "battlefields": "battlefields",
    "runes": "runes",
    "sideboard": "sideboard",
    # synonyms commonly produced by deck builders / pasted from other tools
    "legends": "legend",
    "champions": "champion",
    "main": "main_deck",
    "deck": "main_deck",
    "battlefield": "battlefields",
    "rune pool": "runes",
    "runepool": "runes",
    "rune": "runes",
    "side": "sideboard",
    "side board": "sideboard",
}

_LINE_RE = re.compile(r"^(\d+)\s+(.+)$")


@dataclass(frozen=True)
class ParsedDeckFile:
    deck_id: str
    legend: str
    champion: str
    main_deck: tuple[str, ...]
    battlefields: tuple[str, ...]
    runes: list[tuple[str, int]]
    sideboard: tuple[str, ...]


def _build_name_index(
    allowed_types: frozenset[str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    candidates = [
        card
        for card in csv_cards()
        if allowed_types is None or card.card_type in allowed_types
    ]
    exact: dict[str, str] = {}
    for card in sorted(candidates, key=lambda c: (c.rarity == "Showcase", c.name)):
        key = card.name.strip()
        if key and key not in exact:
            exact[key] = key
    lower = {name.lower(): name for name in exact.values()}
    return exact, lower


_csv_name_cache: dict[frozenset[str] | None, tuple[dict[str, str], dict[str, str]]] = {}


def _name_maps(allowed_types: frozenset[str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    key = allowed_types
    if key not in _csv_name_cache:
        _csv_name_cache[key] = _build_name_index(allowed_types)
    return _csv_name_cache[key]


def resolve_card_name(raw: str, *, allowed_types: frozenset[str] | None = None) -> str:
    name = raw.strip()
    if not name:
        raise ValueError("empty card name")
    exact, lower = _name_maps(allowed_types)
    if name in exact:
        return exact[name]
    hit = lower.get(name.lower())
    if hit:
        return hit
    if ", " in name:
        suffix = name.split(", ", 1)[-1].strip()
        if suffix in exact:
            return exact[suffix]
        hit = lower.get(suffix.lower())
        if hit:
            return hit
    allowed = f" (types: {', '.join(sorted(allowed_types))})" if allowed_types else ""
    raise ValueError(f"unknown card name '{raw}'{allowed}")


def _normalize_section(header: str) -> str | None:
    key = header.strip().rstrip(":").lower()
    return _SECTION_ALIASES.get(key)


def _expand_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        match = _LINE_RE.match(stripped)
        if match:
            count = int(match.group(1))
            card_name = match.group(2).strip()
            if count < 1 or count > 3:
                raise ValueError(f"invalid copy count {count} for '{card_name}' (must be 1–3)")
            out.extend([card_name] * count)
        else:
            out.append(stripped)
    return out


def parse_deck_text(text: str, *, deck_id: str = "deck") -> ParsedDeckFile:
    sections: dict[str, list[str]] = {}
    current: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(":"):
            current = _normalize_section(line)
            if current is None:
                raise ValueError(f"unknown section header '{line}' in deck '{deck_id}'")
            sections.setdefault(current, [])
            continue
        if current is None:
            raise ValueError(f"deck '{deck_id}': content before any section: {line!r}")
        sections[current].append(line)

    for required in ("legend", "champion", "main_deck", "battlefields", "runes"):
        if required not in sections:
            raise ValueError(f"deck '{deck_id}' missing required section '{required}'")

    legend_lines = _expand_lines(sections["legend"])
    champion_lines = _expand_lines(sections["champion"])
    if len(legend_lines) != 1:
        raise ValueError(f"deck '{deck_id}' must have exactly 1 legend, got {len(legend_lines)}")
    if len(champion_lines) != 1:
        raise ValueError(f"deck '{deck_id}' must have exactly 1 champion, got {len(champion_lines)}")

    main_raw = _expand_lines(sections["main_deck"])
    bf_raw = _expand_lines(sections["battlefields"])
    rune_lines = sections["runes"]
    sideboard_raw = _expand_lines(sections.get("sideboard", []))

    legend = resolve_card_name(legend_lines[0], allowed_types=frozenset({"Legend"}))
    champion = resolve_card_name(champion_lines[0], allowed_types=frozenset({"Unit"}))
    main_deck = tuple(resolve_card_name(n) for n in main_raw)
    battlefields = tuple(
        resolve_card_name(n, allowed_types=frozenset({"Battlefield"})) for n in bf_raw
    )

    runes: list[tuple[str, int]] = []
    for line in rune_lines:
        match = _LINE_RE.match(line.strip())
        if not match:
            raise ValueError(f"invalid rune line '{line}' (expected e.g. '7 Chaos Rune')")
        count = int(match.group(1))
        rune_label = match.group(2).strip()
        domain = rune_label.removesuffix(" Rune").removesuffix(" rune").strip()
        if not domain:
            raise ValueError(f"invalid rune line '{line}'")
        resolve_card_name(f"{domain} Rune", allowed_types=frozenset({"Rune"}))
        runes.append((domain, count))

    sideboard = tuple(resolve_card_name(n) for n in sideboard_raw)

    return ParsedDeckFile(
        deck_id=deck_id,
        legend=legend,
        champion=champion,
        main_deck=main_deck,
        battlefields=battlefields,
        runes=runes,
        sideboard=sideboard,
    )


def list_deck_ids() -> tuple[str, ...]:
    if not DECKS_DIR.is_dir():
        return ()
    return tuple(
        sorted(path.stem for path in DECKS_DIR.glob("*.txt") if path.is_file())
    )


def deck_file_path(deck_id: str) -> Path:
    path = DECKS_DIR / f"{deck_id}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"deck file not found: {path}")
    return path


def load_deck_file(deck_id: str) -> ParsedDeckFile:
    path = deck_file_path(deck_id)
    text = path.read_text(encoding="utf-8")
    return parse_deck_text(text, deck_id=deck_id)


def runes_to_engine_list(runes: list[tuple[str, int]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for domain, count in runes:
        out.extend({"domain": domain} for _ in range(count))
    return out


def deck_data_for_id(deck_id: str) -> dict[str, object]:
    parsed = load_deck_file(deck_id)
    return {
        "valid": True,
        "battlefields": list(parsed.battlefields),
        "chosen_champion": parsed.champion,
        "legend": parsed.legend,
        "cards": list(parsed.main_deck),
        "runes": runes_to_engine_list(parsed.runes),
        "sideboard": list(parsed.sideboard),
    }


def deck_ids() -> tuple[str, ...]:
    return list_deck_ids()
