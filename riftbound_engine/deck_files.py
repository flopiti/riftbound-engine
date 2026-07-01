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
    candidates = [name]
    if ", " in name:
        candidates.append(name.split(", ", 1)[-1].strip())
    for cand in candidates:
        if cand in exact:
            return exact[cand]
        hit = lower.get(cand.lower())
        if hit:
            return hit
        # Some legends are listed only as their starter-deck printing, named
        # "<title> - Starter" (e.g. "Dark Child - Starter" for "Annie, Dark
        # Child"). Match those too, returning the real CSV name.
        starter = cand + " - Starter"
        if starter in exact:
            return exact[starter]
        hit = lower.get(starter.lower())
        if hit:
            return hit
    # Punctuation-insensitive fallback: match ignoring commas/spaces/case, so
    # "Ahri Inquisitive" resolves to "Ahri, Inquisitive". Cheap and forgiving of
    # an agent (or user) dropping the comma.
    def _squash(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", s.lower())

    squashed = _squash(name)
    if squashed:
        for real in exact.values():
            if _squash(real) == squashed:
                return real
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


def _swap_card_into_maindeck(text: str, card: str) -> str:
    """Add 1 copy of ``card`` to the MainDeck and remove 1 copy of some OTHER
    main-deck card, so the count stays valid. Returns the new deck text."""
    lines = text.splitlines()
    out: list[str] = []
    in_main = False
    removed = False
    for line in lines:
        s = line.strip()
        is_header = s.endswith(":")
        if is_header:
            in_main = _normalize_section(s[:-1]) == "main_deck"
            out.append(line)
            if in_main:
                out.append(f"1 {card}")  # the card under test, at the top
            continue
        if in_main and s and not removed:
            m = re.match(r"^(\d+)\s+(.+)$", s)
            if m and m.group(2).strip().lower() != card.lower():
                n = int(m.group(1))
                if n > 1:
                    out.append(f"{n - 1} {m.group(2).strip()}")
                removed = True  # if n == 1, drop the line entirely
                continue
        out.append(line)
    return "\n".join(out) + "\n"


def _swap_card_into_battlefields(text: str, card: str) -> str:
    """Put ``card`` into the BATTLEFIELDS section, replacing ONE existing
    battlefield so the count stays at DECK_BATTLEFIELD_COUNT. If the card is
    already a battlefield in this deck, the text is returned unchanged. Returns
    the new deck text."""
    lines = text.splitlines()
    out: list[str] = []
    in_bf = False
    already = any(
        re.match(r"^\d+\s+(.+)$", ln.strip())
        and re.match(r"^\d+\s+(.+)$", ln.strip()).group(1).strip().lower() == card.lower()
        for ln in lines
    )
    replaced = False
    for line in lines:
        s = line.strip()
        if s.endswith(":"):
            in_bf = _normalize_section(s[:-1]) == "battlefields"
            out.append(line)
            if in_bf and not already:
                out.append(f"1 {card}")  # the battlefield under test, listed first
            continue
        if in_bf and s and not already and not replaced:
            m = re.match(r"^(\d+)\s+(.+)$", s)
            if m and m.group(2).strip().lower() != card.lower():
                # drop one existing battlefield to keep the 3-battlefield count
                replaced = True
                continue
        out.append(line)
    return "\n".join(out) + "\n"


def deck_for_card(card_name: str) -> tuple[str, str]:
    """A playable deck id that RUNS ``card_name``, plus how it was obtained:
    ``("<id>", "existing")`` when a real deck already includes the card (reused
    as-is), or ``("test_<slug>", "created")`` when none does and a throwaway
    test deck had to be built. NEVER modifies an existing deck."""
    card = resolve_card_name(card_name)
    # Prefer a real deck that already runs the card (main deck or champion) —
    # no point building a throwaway when one exists.
    for did in list_deck_ids():
        if did.startswith("test_"):
            continue
        try:
            pf = load_deck_file(did)
        except Exception:
            continue
        champ = pf.champion[0] if isinstance(pf.champion, list) else pf.champion
        # A Battlefield card "runs" in a deck when it's one of that deck's
        # battlefields (placed at setup — it is NEVER played from the main
        # deck); a unit/spell/gear runs when it's in the main deck or champion.
        if card in pf.main_deck or card == champ or card in pf.battlefields:
            return did, "existing"
    return create_test_deck(card), "created"


def create_test_deck(card_name: str) -> str:
    """Build a NEW throwaway deck (``decks/test_<slug>.txt``) that runs
    ``card_name``, derived from a domain-compatible existing deck by swapping the
    card in. NEVER modifies an existing deck. Returns the new deck id."""
    from .csv_data import card_domains_of

    card = resolve_card_name(card_name)
    doms = {d for d in card_domains_of(card) if d and d != "Colorless"}

    base_id = None
    for did in list_deck_ids():
        if did.startswith("test_"):
            continue
        try:
            pf = load_deck_file(did)
        except Exception:
            continue
        legend = pf.legend[0] if isinstance(pf.legend, list) else pf.legend
        legend_doms = set(card_domains_of(str(legend)))
        if not doms or doms <= legend_doms:
            base_id = did
            break
    if base_id is None:
        non_test = [d for d in list_deck_ids() if not d.startswith("test_")]
        if not non_test:
            raise ValueError("no base deck available to derive a test deck from")
        base_id = non_test[0]

    from .csv_data import card_type_of

    text = deck_file_path(base_id).read_text(encoding="utf-8")
    # A Battlefield card belongs in the BATTLEFIELDS section (placed at setup),
    # not the main deck — putting it in the main deck would never let it appear
    # on the board. Route by card type.
    if (card_type_of(card) or "").lower() == "battlefield":
        new_text = _swap_card_into_battlefields(text, card)
    else:
        new_text = _swap_card_into_maindeck(text, card)
    slug = re.sub(r"[^a-z0-9]+", "_", card.lower()).strip("_") or "card"
    test_id = f"test_{slug}"
    header = (
        f"# TEST DECK (throwaway) — {card} added for implementation testing.\n"
        f"# Derived from {base_id}. NOT a real/persistent deck; safe to delete.\n"
    )
    (DECKS_DIR / f"{test_id}.txt").write_text(header + new_text, encoding="utf-8")
    return test_id


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
