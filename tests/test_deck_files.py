import unittest
from pathlib import Path

from riftbound_engine.deck_files import (
    DECKS_DIR,
    deck_data_for_id,
    list_deck_ids,
    load_deck_file,
    parse_deck_text,
    resolve_card_name,
)
from riftbound_engine.engine import build_deck_from_id


class DeckFilesTests(unittest.TestCase):
    def test_decks_dir_has_at_least_two_txt_files(self) -> None:
        self.assertGreaterEqual(len(list_deck_ids()), 2)

    def test_resolve_legend_suffix_after_comma(self) -> None:
        name = resolve_card_name("Ezreal, Prodigal Explorer", allowed_types=frozenset({"Legend"}))
        self.assertEqual(name, "Prodigal Explorer")

    def test_ezreal_deck_loads_with_expected_shape(self) -> None:
        deck = build_deck_from_id("ezreal_prodigal_explorer")
        self.assertEqual(len(deck.cards), 39)
        self.assertEqual(len(deck.runes), 12)
        self.assertEqual(len(deck.battlefields), 3)
        domains = [r.domain for r in deck.runes]
        self.assertEqual(domains.count("Chaos"), 7)
        self.assertEqual(domains.count("Mind"), 5)

    def test_parse_minimal_sections(self) -> None:
        text = Path(DECKS_DIR / "ember_vanguard.txt").read_text(encoding="utf-8")
        parsed = parse_deck_text(text, deck_id="ember_vanguard")
        self.assertEqual(len(parsed.main_deck), 39)
        data = deck_data_for_id("ember_vanguard")
        self.assertEqual(len(data["runes"]), 12)
        self.assertTrue(all(r["domain"] == "Fury" for r in data["runes"]))

    def test_tide_wardens_runes(self) -> None:
        deck = build_deck_from_id("tide_wardens")
        domains = [r.domain for r in deck.runes]
        self.assertEqual(domains.count("Fury"), 6)
        self.assertEqual(domains.count("Body"), 6)

    def test_sideboard_parsed_but_not_in_engine_deck(self) -> None:
        parsed = load_deck_file("ezreal_prodigal_explorer")
        self.assertGreater(len(parsed.sideboard), 0)
        deck = build_deck_from_id("ezreal_prodigal_explorer")
        self.assertEqual(len(deck.cards), 39)


if __name__ == "__main__":
    unittest.main()
