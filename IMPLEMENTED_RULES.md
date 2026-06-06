# Implemented Rules

A running list of Riftbound rules the engine actually enforces (or fakes well
enough to drive the UI). Add new entries to the top of the list as they ship.

## Trigger / event system

Cards' triggered abilities (authored in the front-end taxonomy wizard,
`riftbound/data/card_taxonomy.json`) now fire during play.

### Pieces

- `triggers.py` — `GameEvent` (an immutable "what just happened" record),
  the event-kind vocabulary (`ON_PLAY_UNIT`, `ON_PLAY_SPELL`, `ON_DEATH`,
  `ON_CONQUER`, `TURN_START`, `ON_CHANNEL`, `ON_DRAW`), and
  `TRIGGER_EVENT_MAP` mapping each wizard trigger code to an event + a scope
  (`SELF` / `FRIENDLY` / `ENEMY` / `ANY` / `HERE`). `trigger_matches` is the
  pure predicate deciding whether a card's trigger responds to an event.
- `abilities.py` — loads the taxonomy and bridges its id-keyed assignments to
  the card *names* the engine plays with. Absent file ⇒ no abilities (engine
  runs exactly as before). Path overridable via `RIFTBOUND_TAXONOMY_PATH`.
- `effects.py` — a `@register_effect("CODE")` registry mirroring the
  action-turn registry. Implemented so far: `DRAW_1`, `SCORE_1_POINT`,
  `CHANNEL_1_RUNE`, `CHANNEL_1_RUNE_EXHAUSTED`,
  `EACH_PLAYER_CHANNEL_1_RUNE_EXHAUSTED`,
  `PUT_TOP_2_CARDS_OF_MAIN_DECK_INTO_TRASH`, `OPPONENT_DISCARD_1`,
  `DISCARD_1_DRAW_1`, `GIVE_ME_+1`, `GIVE_ME_+2M`, `ADDITIONAL_2M`.
  Unregistered codes resolve as a recorded no-op (logged, never an error) so
  the chain always drains; the Implementation Planner tab tracks the rest.

### Flow

`GameEngine._emit(event)` is called at each state transition (unit enters
play, spell **resolution** — by design a spell gets its reaction window
alone, and "when you play a spell" triggers only hit the chain after it
resolves — unit dies in combat, battlefield conquered/held, turn start,
channel, draw). It scans cards in play *at that moment* for matching
triggered abilities and queues them. `_drain_triggers()` then pushes each onto
the **chain** as a `ChainItem` carrying a `TriggeredEffect` (APNAP order —
the active player's go on first, so they resolve last under LIFO). The single
drain point sits in `start()` right after ABCD, which every action routes back
through. When the chain resolves an item (`_resolve_chain`), a triggered
ability runs each of its effect codes through the effect registry; cast spells
remain no-ops as before.

### On-screen

`GameState.event_feed` records what happened ("event"), what went on the chain
("trigger") and what resolved ("effect"); it is serialized to the wire
alongside the generalized `pending_chain` items (each now exposes `label` and,
for a triggered ability, its `effect` payload) and the new per-unit
`bonus_might`. Buffs (`GIVE_ME_+1` etc.) add to `bonus_might`, which
`might_at_battlefield` now folds into combat strength.

## 0. Playing a spell

Spells are the second card type the engine can play, alongside Units.
Mechanically the costs and the affordability gate are identical to a Unit
play; the difference is that a Spell never picks a location.

### Wire format

- `play:play_spell:<hand_index>` — 0-based index into the active player's
  hand. Only cards whose CSV `Card Type` is `Spell` may be played this way
  (non-Spell cards raise from the handler, mirroring the `play_unit` guard).

### Cost gate

Same as `play_unit`:

- `card Energy ≤ player Energy pool`, and
- `card Power ≤ available domain Power` (matched against the card's CSV
  `Domain`(s)).

Both costs are **deducted up front** the moment the play resolves. There is
no `pending_play` / `choose_location` follow-up — the card moves straight
from `hand` into `player_X_spells`.

### State

- `GameState.player_1_spells` / `player_2_spells` — lists of `PlayedSpell`
  in cast order. **Cleared at end of turn** in `_advance_turn` (both
  players' stacks wipe whenever the active player ends their turn), so the
  right-side spell overlay starts every turn empty. There is still no
  per-spell resolution step — cards just vanish.
- Serialized to the wire as `player_1_spells` / `player_2_spells`, each
  entry shaped `{"card": <name>}`.

### Options

Inside the action turn (no `pending_play`, no `pending_showdown`), the
options list emits a `play:play_spell:<i>` per Spell-typed hand card whose
Energy and domain Power costs the active player can already pay. These
appear just after the `play:play_unit:*` options.

### Surface

The frontend (`riftbound/src/components/play/SpellsOverlay.tsx`) renders
cast spells anchored to the right edge of the play surface, vertically
centered, slightly enlarged compared to a hand card. Player 2's stack
grows downward from the top half, Player 1's grows upward from the bottom
half. Newer casts overlap older ones. Spells stay in the overlay for the
turn they were cast in, and `_advance_turn` clears both players' spell
stacks when the active player ends the turn — so the overlay always reads
"what was cast during the turn that's just ending."

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

A unit may only be **played** (`play_unit` → `choose_location`) in a
location the active player **controls**.

- Each player always controls their own `base`.
- A battlefield is "controlled" by whichever player matches
  `battlefield_1_controller` / `battlefield_2_controller` on the game state.
  Both start as `None` (uncontrolled), so initially neither player can play
  units on either battlefield — `base` is the only legal target.
- Control of an uncontrolled or opponent-held battlefield is gained by
  walking a ready unit onto it via `play:move_unit` and winning the
  resulting showdown (see §3a).

### §3a — Moving units and showdowns

After a unit has been played, `play:move_unit:<unit_index>:<destination>`
lets the active player walk a ready unit between `base` and either
battlefield. Rules:

- The unit must be **ready** (no summoning sickness, not exhausted
  earlier this turn).
- Allowed transitions: `base` ↔ a battlefield. BF ↔ BF jumps are
  rejected (route through base).
- Moving always exhausts the unit, so it can't move again this turn.
- What happens at the destination depends on its current controller:
    - **Own-controlled BF** → just relocates the unit.
    - **Uncontrolled BF** → opens a `PendingShowdown` with the active
      player as initiator.
    - **Opponent-controlled BF** → also opens a `PendingShowdown` (the
      "invade" path). Unlike `play_unit`, which still refuses to deploy
      a *fresh* unit onto an opponent BF, moving a unit that's already
      in play across the line is allowed and forces the contest.

While a showdown is pending, both players' option menus collapse to a
single `play:pass_showdown` (initiator first, then opponent). When both
have passed, the battlefield's controller is set to the initiator (the
current minimal model — proper unit-vs-unit resolution is still a
placeholder) and the initiator scores 1 point (capped at 1 per BF per
turn via `award_bf_point`).

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
