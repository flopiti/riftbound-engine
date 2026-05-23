# Deck lists

Each deck is a plain-text file in this folder. The filename (without `.txt`) is the **deck id** used in the engine (e.g. `choose_deck:ezreal_prodigal_explorer`).

## Format

- One section per block, with a header line ending in `:`.
- Card lines: `quantity name` (quantity 1–3).
- Rune lines: `quantity Domain Rune` (e.g. `7 Chaos Rune`).
- Lines starting with `#` are comments.
- Empty lines are ignored.

Required sections: `Legend`, `Champion`, `MainDeck`, `Battlefields`, `Runes`.  
Optional: `Sideboard` (parsed and stored; not used by the game engine yet).

### Example

```
Legend:
1 Prodigal Explorer

Champion:
1 Ezreal, Prodigy

MainDeck:
3 Gust
...

Battlefields:
1 Abandoned Hall
1 Marai Spire
1 Targon's Peak

Runes:
7 Chaos Rune
5 Mind Rune

Sideboard:
1 Rebuke
```

Card names are resolved against `riftbound_cards.csv`. You can use the full CSV name or, for legends, the short suffix after a comma (e.g. `Prodigal Explorer` for `Ezreal, Prodigal Explorer`).

## Rules enforced on load

- Exactly 1 legend and 1 champion.
- Main deck expands to **39** cards (max 3 copies per card).
- **3** battlefields.
- Runes sum to **12**.
