# Implemented Rules

A running list of Riftbound rules the engine actually enforces (or fakes well
enough to drive the UI). Add new entries to the top of the list as they ship.

## 1. The turn loop (ABCD runs automatically)

Every time `engine.start()` is polled, if the active player hasn't finished
ABCD it runs the full A→B→C→D sequence in one shot (`_complete_abcd_for_current_player`)
and then re-emits options. **ABCD is never surfaced as user actions** — the
client just sees the action-turn options once setup for the turn is done.

- **A — Awake.** Every exhausted rune in the active player's pool flips
  back to ready, and every unit owned by the active player readies as well
  (clearing summoning sickness from units played the previous turn, and
  resetting any mid-match exhaustion on older units).
- **B — (placeholder).** No effect yet; just gates the order so C can't
  fire before B.
- **C — Channel.** Draws runes from the active player's rune library into
  their rune pool. The match-wide schedule is **2 on the 1st channel, 3 on
  the 2nd, 2 forever after** (`global_channel_count` tracks this across
  both players). If the library has fewer than the scheduled count, all
  remaining are drawn; the step still completes.
- **D — Draw.** One card from the active player's main library into hand.

When the active player ends their turn via `play:end_turn`, the engine
calls `_advance_turn()`: swaps `current_player`, bumps the turn counters,
clears **both players' Energy pools**, and resets the ABCD flags so the
next `start()` runs the new player's ABCD automatically.

## 2. The action turn (after ABCD)

Once ABCD is done, the active player is in the action-turn phase and the
engine emits options in this order:

1. **Exhaust runes to produce Energy** — `play:exhaust_rune:<i>` per
   unique ready-rune *domain* (same-domain runes are functionally
   identical, so the menu collapses to one option per domain, pointing at
   that domain's leftmost ready rune). Each exhaust flips that rune's
   `exhausted` flag to `True` and adds **1 Energy** to the active player's
   pool. The handler still accepts any valid ready-rune index, so tests
   can target specific indices.
2. **Play a Unit you can afford** — `play:play_unit:<hand_index>` for each
   Unit-typed card in hand whose Energy cost is ≤ the player's current
   Energy pool. Non-Unit cards are not offered.
3. **End the turn** — `play:end_turn`.

The inactive player's options stay empty throughout.

### Energy

- Energy is a per-turn pool, stored on `player_1_energy` /
  `player_2_energy`. It is produced by exhausting runes (1 per rune) and
  consumed by `play:play_unit` (deducted up front by the card's Energy
  cost from `riftbound_cards.csv`).
- **Energy does NOT persist across turns.** `_advance_turn` clears both
  pools when the turn ends; unspent Energy is lost.
- The cost gate is `card Energy ≤ player Energy` — checked at option
  emission *and* re-checked in the handler (last line of defense against
  clients that bypass options).

## 3. Playing a unit

Playing a Unit is two steps: pick the card (which deducts Energy), then
pick the location.

### Step A — start the play

- Wire format: `play:play_unit:<hand_index>` (0-based).
- Cost gate: rejected if `card Energy > player Energy pool`.
- Effect: the card's Energy cost is deducted from the active player's
  pool, the card is removed from hand, and it is parked in `pending_play`
  on the game state. It is not yet in `units`.
- While `pending_play` is set, the active player's only available options
  are the controlled locations (Step B). `play:exhaust_rune:*`,
  `play:end_turn`, and further `play:play_unit:*` are suppressed.

### Step B — commit to a location

- Wire format: `play:choose_location:<location>` where `<location>` is one
  of `base`, `battlefield_1`, `battlefield_2`.
- Effect: the pending card is appended to `player_X_units` as
  `{card, location, exhausted: true}` (summoning sickness — see §4).
  `pending_play` is cleared. The player can then exhaust more runes,
  start another play, or `play:end_turn`.

### Step B constraint — control

A unit may only be played in a location the active player **controls**.

- Each player always controls their own `base`.
- A battlefield is "controlled" by whichever player matches
  `battlefield_1_controller` / `battlefield_2_controller` on the game state.
  Both start as `None` (uncontrolled), so initially neither player can play
  units on either battlefield — `base` is the only legal target.
- No mechanism for gaining control of a battlefield is implemented yet;
  once one is added, those fields will be set by it.

## 4. Summoning sickness

Newly played units enter the battlefield (or base) exhausted. They
remain exhausted through the opponent's turn — the opponent's Awake does
not touch our units — and ready on the owner's next Awake (ABCD step A).

## 5. Surface (UI rendering)

- The played card renders at its chosen zone — own base, or battlefield
  1/2 — in play order, with a brief base → destination animation on
  first appearance.
- Exhausted runes render rotated 90° ("tapped") and slightly dimmed.
- The control dashboard's Player 1 / Player 2 panels show the current
  exhausted-rune count (e.g. `3 of 5`) and the current Energy pool
  (clears at turn end).
- Options surface (active player's `player_X_options`):
  - **Default (action turn, no `pending_play`):** `play:exhaust_rune:<i>`
    per unique ready-rune domain, then `play:play_unit:<i>` per
    affordable Unit, then `play:end_turn`.
  - **With `pending_play` set:** only `play:choose_location:<loc>` for
    locations the active player controls. At match start that's just
    `play:choose_location:base`.
- The inactive player's options stay empty throughout.
