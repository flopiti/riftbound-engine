"""Shortcut planning — server-authoritative replacement for the old
client-side `riftbound/src/utils/shortcuts.ts`.

A "shortcut" is a single packaged play: the rune-payment steps + the final
`play_unit` / `play_spell` action, bundled so the user can pick a card from
their hand and have all the rune mechanics happen as one click. Previously
the client enumerated valid rune combinations from the engine's snapshot;
moving it here means the engine is the source of truth for which payment
plans are legal AND for the chain of actions they expand into. The client
just renders the chips the server hands it.

Why this lives in the engine, not in the action_turn handlers themselves:
the engine's `apply_action` operates on ONE step at a time (a single
exhaust_rune, a single play_unit). Shortcuts are a UI concept that sits
ABOVE that — they enumerate the COMBINATIONS of those individual steps
that would together satisfy a card's cost. We compute them when serving
the snapshot, then the `/branch/forward` endpoint executes whatever chain
the client sends back.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Iterator

from .csv_data import (
    card_domains_of,
    card_energy_of,
    card_is_reaction,
    card_power_of,
    card_type_of,
)
from .engine import GameEngine, RequiredTo, Rune
from .requirements import spell_playable


@dataclass(frozen=True)
class ShortcutStep:
    """One rune action inside a shortcut's payment chain.

    `rune_index` refers to the rune's position in the PLAYER'S CURRENT POOL
    at planning time. The executor (in http_api's /branch/forward) is
    responsible for translating these into the engine's per-step indices,
    which shift when recycle/exhaust_and_recycle POP runes from the pool.
    """

    action: str  # 'exhaust_rune' | 'recycle_rune' | 'exhaust_and_recycle_rune'
    rune_index: int
    rune_domain: str


@dataclass
class Shortcut:
    """One playable card × one rune-payment plan, ready to expand into a
    chain of engine actions. The `key` derives from the multiset of rune
    domains spent, so two plans that differ only in WHICH specific rune
    of a given domain is tapped collapse into a single chip.
    """

    actor: RequiredTo
    card_index: int
    card_name: str
    play_action: str  # 'play_unit' | 'play_spell'
    energy_cost: int
    power_cost: int
    cost_domains: tuple[str, ...]
    plan: tuple[ShortcutStep, ...]
    domain_counts: tuple[tuple[str, int], ...]  # ((domain, count), ...) sorted by domain
    key: str  # canonical multiset key, e.g. "Body×1#Mind×2"
    label: str  # pre-formatted human label, e.g. "Play Raven Bloom (1 Mind, 1 Body)"


@dataclass
class PlayIntent:
    """One playable card's "intent" — its PRINTED cost plus every valid
    rune-payment plan for it. The branch view's two-tier picker uses this:
    tier-1 chips are one PlayIntent per card (showing the printed cost
    only), and tier-2 only appears when the user picks a card with more
    than one combo, at which point we render `combos`.

    Single-combo cards auto-skip tier-2 — clicking the intent applies the
    only combo's chain directly. The auto-skip lives on the client (the
    server treats `intent_only` steps as a no-op path entry); the engine
    doesn't care which tier the user is on.
    """

    actor: RequiredTo
    card_index: int
    card_name: str
    play_action: str
    energy_cost: int
    power_cost: int
    cost_domains: tuple[str, ...]
    combos: tuple[Shortcut, ...]


# ---------------------------------------------------------------------------
# Planning algorithm — direct port of the TS `planPayments` + `solveSubset`.
# ---------------------------------------------------------------------------


def _canonical_key(plan: list[ShortcutStep]) -> str:
    """Sorted multiset of `<action>|<domain>` pairs. Plans with the same key
    are visually identical to the user (same count of each action per
    domain) and collapse into one shortcut."""
    return "#".join(sorted(f"{s.action}|{s.rune_domain}" for s in plan))


def _domain_multiset_key(plan: list[ShortcutStep]) -> str:
    """Sorted "<domain>×<count>" string. Two plans with the same domain
    multiset are interchangeable from the user's point of view (which
    specific domain provides power vs energy is internal detail)."""
    counts: dict[str, int] = {}
    for s in plan:
        counts[s.rune_domain] = counts.get(s.rune_domain, 0) + 1
    return "#".join(sorted(f"{d}×{n}" for d, n in counts.items()))


def _domain_counts(plan: list[ShortcutStep]) -> list[tuple[str, int]]:
    """Domain → count, sorted alphabetically for stable display order."""
    counts: dict[str, int] = {}
    for s in plan:
        counts[s.rune_domain] = counts.get(s.rune_domain, 0) + 1
    return sorted(counts.items(), key=lambda x: x[0])


def _is_minimal(
    plan: list[ShortcutStep],
    total_e: int,
    total_p: int,
    e_gap: int,
    p_gap: int,
) -> bool:
    """A plan is "minimal" if removing any single step would break at least
    one cost constraint, AND no exhaust_and_recycle is producing surplus
    power that a plain exhaust could've covered.

    Without this gate the enumerator emits plans like "exhaust 3 Mind" for
    a 2-Energy cost — the third exhaust contributes nothing the user would
    actually want — and "exhaust_and_recycle Mind" for a cost that already
    has enough Power without the recycle bit.
    """
    for step in plan:
        e_contrib = 0 if step.action == "recycle_rune" else 1
        p_contrib = 0 if step.action == "exhaust_rune" else 1
        # (a) Step entirely wasted? Removing it leaves both constraints
        # satisfied → the rune wasn't needed at all.
        if total_e - e_contrib >= e_gap and total_p - p_contrib >= p_gap:
            return False
        # (b) Downgrade check: an exhaust_and_recycle whose power isn't
        # actually needed should've been a plain exhaust.
        if step.action == "exhaust_and_recycle_rune" and total_p - 1 >= p_gap:
            return False
    return True


def _solve_subset(
    mask: int,
    runes: list[Rune],
    allowed_domains: set[str],
    e_gap: int,
    p_gap: int,
) -> Iterator[list[ShortcutStep]]:
    """Yield every valid action assignment for the runes in `mask`,
    respecting the engine's per-rune-state rules:

      * READY rune     → exhaust_rune OR exhaust_and_recycle_rune
      * EXHAUSTED rune → recycle_rune (only)

    Runes outside the cost's allowed domains can only contribute Energy
    (ready: exhaust_rune; exhausted: rejected because their power is
    unusable).
    """
    ready_allowed: list[int] = []
    ready_other: list[int] = []
    exhausted_allowed: list[int] = []
    for i in range(len(runes)):
        if (mask & (1 << i)) == 0:
            continue
        r = runes[i]
        in_allowed = r.domain in allowed_domains
        if r.exhausted:
            if not in_allowed:
                # Exhausted rune in a disallowed domain — recycle would
                # waste power → reject the whole subset.
                return
            exhausted_allowed.append(i)
        elif in_allowed:
            ready_allowed.append(i)
        else:
            ready_other.append(i)

    forced_energy = len(ready_other)
    forced_power = len(exhausted_allowed)

    forced_tail: list[ShortcutStep] = []
    for i in ready_other:
        forced_tail.append(ShortcutStep("exhaust_rune", i, runes[i].domain))
    for i in exhausted_allowed:
        forced_tail.append(ShortcutStep("recycle_rune", i, runes[i].domain))

    local_seen: set[str] = set()
    head: list[ShortcutStep] = []

    def go(idx: int, e_acc: int, p_acc: int) -> Iterator[list[ShortcutStep]]:
        if idx == len(ready_allowed):
            total_e = e_acc + forced_energy
            total_p = p_acc + forced_power
            if total_e < e_gap or total_p < p_gap:
                return
            full = head + forced_tail
            if not _is_minimal(full, total_e, total_p, e_gap, p_gap):
                return
            k = "#".join(sorted(f"{s.action}|{s.rune_domain}" for s in full))
            if k in local_seen:
                return
            local_seen.add(k)
            yield list(full)
            return
        i = ready_allowed[idx]
        d = runes[i].domain
        # Option A: exhaust_and_recycle (+1 energy, +1 power)
        head.append(ShortcutStep("exhaust_and_recycle_rune", i, d))
        yield from go(idx + 1, e_acc + 1, p_acc + 1)
        head.pop()
        # Option B: exhaust (+1 energy, +0 power)
        head.append(ShortcutStep("exhaust_rune", i, d))
        yield from go(idx + 1, e_acc + 1, p_acc)
        head.pop()
        # NOTE: recycle is NOT an option for ready runes — engine rejects.

    yield from go(0, 0, 0)


def _plan_payments(
    energy_cost: int,
    power_cost: int,
    cost_domains: tuple[str, ...],
    runes: list[Rune],
    current_energy: int,
    current_power: dict[str, int],
) -> list[list[ShortcutStep]]:
    """Enumerate every DISTINCT (by domain multiset) plan that exactly
    covers the cost gaps from the available rune pool. Returns [] if the
    card is already free-to-play (the engine surfaces play_unit directly
    in that case) or impossible to pay.

    Only rune subsets up to ``energy_gap + power_gap`` in size are considered:
    each rune contributes at most +1 Energy and +1 Power, so a MINIMAL plan
    can never use more runes than that. Bounding the subset size (instead of
    walking all 2**n subsets) keeps planning fast even with a full rune pool —
    the old all-subsets walk blew up to millions of states for a 12-rune pool
    and made both the UI poll and the advanced auto-pilot hang.
    """
    allowed = set(cost_domains)
    e_gap0 = max(0, energy_cost - current_energy)
    available_power = sum(current_power.get(d, 0) for d in cost_domains)
    p_gap0 = max(0, power_cost - available_power)
    if e_gap0 == 0 and p_gap0 == 0:
        return []
    n = len(runes)
    if n == 0:
        return []

    # Upper bound on a minimal plan's size (see docstring). Plans from a mask
    # always use every rune in the mask, so mask popcount == plan length.
    max_size = min(n, e_gap0 + p_gap0)
    #: Safety cap on distinct domain-multiset plans collected. The UI only
    #: ever shows a handful; this guards pathological high-cost enumerations.
    PLAN_CAP = 64

    seen_action_keys: set[str] = set()
    domain_keys_seen: set[str] = set()
    all_plans: list[list[ShortcutStep]] = []
    for size in range(1, max_size + 1):
        for combo in itertools.combinations(range(n), size):
            mask = 0
            for i in combo:
                mask |= 1 << i
            for plan in _solve_subset(mask, runes, allowed, e_gap0, p_gap0):
                k = _canonical_key(plan)
                if k in seen_action_keys:
                    continue
                seen_action_keys.add(k)
                all_plans.append(plan)
                domain_keys_seen.add(_domain_multiset_key(plan))
        # Smallest plans are found first (size ascending), so once we have
        # plenty of distinct domain-multiset options we can stop.
        if len(domain_keys_seen) >= PLAN_CAP:
            break

    # Sort so the "preferred" plan per domain-multiset wins:
    #   1. Fewer exhaust_and_recycle actions (leaves more ready runes for
    #      future plays this turn).
    #   2. More recycle-on-exhausted actions (uses already-spent runes).
    #   3. Tie-break on canonical key for stable ordering.
    def sort_key(p: list[ShortcutStep]) -> tuple[int, int, str]:
        xr = sum(1 for s in p if s.action == "exhaust_and_recycle_rune")
        rec = sum(1 for s in p if s.action == "recycle_rune")
        return (xr, -rec, _canonical_key(p))

    all_plans.sort(key=sort_key)

    # Collapse plans with the same domain multiset — internal action
    # assignment is an implementation detail for the user.
    seen_domain_keys: set[str] = set()
    plans: list[list[ShortcutStep]] = []
    for p in all_plans:
        dk = _domain_multiset_key(p)
        if dk in seen_domain_keys:
            continue
        seen_domain_keys.add(dk)
        plans.append(p)

    # Final ordering for display: total rune count ascending, then
    # domain-multiset key for stability.
    plans.sort(key=lambda p: (len(p), _domain_multiset_key(p)))
    return plans


# ---------------------------------------------------------------------------
# Shortcut assembly: turn a plan into a Shortcut + label.
# ---------------------------------------------------------------------------


def _build_shortcut(
    actor: RequiredTo,
    card_index: int,
    card_name: str,
    play_action: str,
    energy_cost: int,
    power_cost: int,
    cost_domains: tuple[str, ...],
    plan: list[ShortcutStep],
) -> Shortcut:
    domain_counts = _domain_counts(plan)
    verb = "Cast" if play_action == "play_spell" else "Play"
    if domain_counts:
        plan_desc = ", ".join(f"{n} {d}" for d, n in domain_counts)
        label = f"{verb} {card_name} ({plan_desc})"
    else:
        label = f"{verb} {card_name}"
    return Shortcut(
        actor=actor,
        card_index=card_index,
        card_name=card_name,
        play_action=play_action,
        energy_cost=energy_cost,
        power_cost=power_cost,
        cost_domains=cost_domains,
        plan=tuple(plan),
        domain_counts=tuple(domain_counts),
        key=_domain_multiset_key(plan),
        label=label,
    )


def compute_play_intents(engine: GameEngine, actor: RequiredTo) -> list[PlayIntent]:
    """Build the two-tier "play this card" picker for `actor`. One
    PlayIntent per playable hand card (deduplicated by card name so two
    copies of the same card don't show up as separate chips), each
    carrying every valid rune-payment combo.

    Returns [] when the player isn't currently the active actor or is
    mid-resolution — same gates as compute_shortcuts.
    """
    gs = engine._game_state
    if gs.pending_play is not None:
        return []
    if gs.pending_spell_choice is not None:
        return []
    if gs.pending_payment is not None:
        return []
    if gs.pending_showdown is not None:
        return []
    if gs.pending_combat is not None:
        # Mid-combat the only legal actions are the damage assignments
        # surfaced by the engine — no cards may be played.
        return []
    if actor not in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
        return []
    # During an open chain only the player holding priority gets a picker,
    # and it's restricted to [Reaction] spells (responses). With no chain,
    # only the active player gets the normal full picker.
    chain = gs.pending_chain
    if chain is not None:
        if actor != chain.priority:
            return []
        reaction_only = True
    else:
        if gs.current_player != actor:
            return []
        reaction_only = False

    hand = gs.player_1_hand if actor == RequiredTo.PLAYER_1 else gs.player_2_hand
    if not hand:
        return []
    runes = gs.player_1_runes if actor == RequiredTo.PLAYER_1 else gs.player_2_runes
    current_energy = (
        gs.player_1_energy if actor == RequiredTo.PLAYER_1 else gs.player_2_energy
    )
    current_power = dict(
        gs.player_1_power if actor == RequiredTo.PLAYER_1 else gs.player_2_power
    )

    seen_card: set[str] = set()
    out: list[PlayIntent] = []
    for i, card in enumerate(hand):
        if card in seen_card:
            # Multiple hand copies → one chip pointing at the leftmost
            # copy; the rest are noise (the user can only play one of
            # them at a time and the planning result is identical).
            continue
        card_type = card_type_of(card)
        if card_type not in ("Unit", "Spell", "Gear"):
            continue
        if reaction_only and not card_is_reaction(card):
            continue
        # Spells must also have a satisfiable Spell Choice Requirement —
        # a valid target set on the current board — not just an affordable
        # cost. See riftbound_engine/requirements.py. (Units have no such
        # requirement today.)
        if card_type == "Spell" and not spell_playable(gs, card):
            continue
        energy_cost = card_energy_of(card) or 0
        power_cost = card_power_of(card) or 0
        cost_domains = tuple(card_domains_of(card))
        plans = _plan_payments(
            energy_cost,
            power_cost,
            cost_domains,
            list(runes),
            current_energy,
            current_power,
        )
        if not plans:
            continue
        play_action = {"Spell": "play_spell", "Gear": "play_gear"}.get(
            card_type, "play_unit"
        )
        combos = tuple(
            _build_shortcut(
                actor=actor,
                card_index=i,
                card_name=card,
                play_action=play_action,
                energy_cost=energy_cost,
                power_cost=power_cost,
                cost_domains=cost_domains,
                plan=plan,
            )
            for plan in plans
        )
        seen_card.add(card)
        out.append(
            PlayIntent(
                actor=actor,
                card_index=i,
                card_name=card,
                play_action=play_action,
                energy_cost=energy_cost,
                power_cost=power_cost,
                cost_domains=cost_domains,
                combos=combos,
            )
        )
    return out


def compute_shortcuts(engine: GameEngine, actor: RequiredTo) -> list[Shortcut]:
    """Build the list of shortcut options for `actor` against the engine's
    current state. Returns [] when the player is mid-resolution
    (pending_play / pending_payment / pending_showdown set) — at those
    points the only legal actions are the engine's own restricted options
    and a "play this card" shortcut would be illegal."""
    gs = engine._game_state
    if gs.pending_play is not None:
        return []
    if gs.pending_spell_choice is not None:
        return []
    if gs.pending_payment is not None:
        return []
    if gs.pending_showdown is not None:
        return []
    if gs.pending_combat is not None:
        return []
    if actor not in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
        return []
    # Mirror compute_play_intents: during an open chain only the priority
    # holder gets options, restricted to [Reaction] spells; otherwise only
    # the active player gets the full picker.
    chain = gs.pending_chain
    if chain is not None:
        if actor != chain.priority:
            return []
        reaction_only = True
    else:
        if gs.current_player != actor:
            return []
        reaction_only = False

    hand = gs.player_1_hand if actor == RequiredTo.PLAYER_1 else gs.player_2_hand
    if not hand:
        return []
    runes = gs.player_1_runes if actor == RequiredTo.PLAYER_1 else gs.player_2_runes
    current_energy = (
        gs.player_1_energy if actor == RequiredTo.PLAYER_1 else gs.player_2_energy
    )
    current_power = dict(
        gs.player_1_power if actor == RequiredTo.PLAYER_1 else gs.player_2_power
    )

    out: list[Shortcut] = []
    for i, card in enumerate(hand):
        card_type = card_type_of(card)
        if card_type not in ("Unit", "Spell", "Gear"):
            continue
        if reaction_only and not card_is_reaction(card):
            continue
        # Same requirement gate as compute_play_intents: a Spell only
        # appears if it has a valid target set on the current board.
        if card_type == "Spell" and not spell_playable(gs, card):
            continue
        energy_cost = card_energy_of(card) or 0
        power_cost = card_power_of(card) or 0
        cost_domains = tuple(card_domains_of(card))
        plans = _plan_payments(
            energy_cost,
            power_cost,
            cost_domains,
            list(runes),
            current_energy,
            current_power,
        )
        if not plans:
            continue
        play_action = {"Spell": "play_spell", "Gear": "play_gear"}.get(
            card_type, "play_unit"
        )
        for plan in plans:
            out.append(
                _build_shortcut(
                    actor=actor,
                    card_index=i,
                    card_name=card,
                    play_action=play_action,
                    energy_cost=energy_cost,
                    power_cost=power_cost,
                    cost_domains=cost_domains,
                    plan=plan,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Chain execution: turn a plan into the sequence of engine actions to apply.
# ---------------------------------------------------------------------------


def execution_steps(shortcut: Shortcut) -> list[tuple[RequiredTo, str]]:
    """Build the ordered list of `(actor, action)` calls the engine should
    apply, in order, to execute `shortcut` end-to-end.

    The tricky bit is that recycle / exhaust_and_recycle POP the rune from
    the pool, which shifts the indices of later runes downward. Plain
    exhaust does not pop. Strategy: run all POP actions FIRST in descending
    index order (so a pop never disturbs the indices of remaining pop
    targets); THEN for each remaining (non-popping) exhaust, remap its
    original index by subtracting the count of already-popped lower-index
    runes; finally apply play_unit / play_spell.
    """
    pops = [s for s in shortcut.plan if s.action != "exhaust_rune"]
    pops_sorted_desc = sorted(pops, key=lambda s: s.rune_index, reverse=True)
    exhausts = [s for s in shortcut.plan if s.action == "exhaust_rune"]

    out: list[tuple[RequiredTo, str]] = []
    for p in pops_sorted_desc:
        out.append((shortcut.actor, f"play:{p.action}:{p.rune_index}"))

    pop_indices_asc = sorted(p.rune_index for p in pops)
    for e in exhausts:
        shift = sum(1 for idx in pop_indices_asc if idx < e.rune_index)
        out.append((shortcut.actor, f"play:exhaust_rune:{e.rune_index - shift}"))

    out.append(
        (shortcut.actor, f"play:{shortcut.play_action}:{shortcut.card_index}")
    )
    return out


# ---------------------------------------------------------------------------
# Serialization for the wire.
# ---------------------------------------------------------------------------


def serialize_shortcut(s: Shortcut) -> dict[str, Any]:
    """JSON-shape for one Shortcut. The client uses `label` for the chip
    text, `domain_counts` (paired with the rune-domain icon set) for the
    cost annotation, and `chain` to send back to /branch/forward on click."""
    return {
        "actor": s.actor.value,
        "action": _synthetic_action(s),
        "label": s.label,
        "card_index": s.card_index,
        "card_name": s.card_name,
        "play_action": s.play_action,
        "energy_cost": s.energy_cost,
        "power_cost": s.power_cost,
        "cost_domains": list(s.cost_domains),
        "domain_counts": [{"domain": d, "count": n} for d, n in s.domain_counts],
        "key": s.key,
        "chain": [
            {"actor": s.actor.value, "action": a}
            for _, a in execution_steps(s)
        ],
    }


def _synthetic_action(s: Shortcut) -> str:
    """Synthetic action string used as the shortcut's identity in the
    branch tree (layoutId, visited-paths key, etc.). It deliberately
    doesn't collide with any real engine action verb because of the
    `shortcut:` prefix — apply_action would reject it directly. The
    real chain lives in the `chain` field."""
    return f"shortcut:{s.play_action}:{s.card_index}:{s.key}"


def _intent_synthetic_action(i: PlayIntent) -> str:
    """Synthetic identity for a card-level intent. Same role as
    `_synthetic_action` but at the tier-1 (card-pick) level — used only
    for layoutId / visited-paths keys, never sent to /engine/action.

    The client records EVERY intent click under this key, regardless of
    combo count:
      • Single-combo intents auto-skip into the combo's chain (the
        engine state advances atomically) but the path step is still
        recorded with `action = intent:N` so the visited-paths lookup
        on the next render — which keys off the chip the user sees,
        i.e. the intent — matches.
      • Multi-combo intents record an `intent_only` no-op step under
        this key, then a follow-up combo step under the combo's
        synthetic action.

    Previously single-combo clicks recorded under the combo's
    synthetic action, which made the same chip look unvisited after a
    rewind because the chip's lookup key (`intent:N`) didn't match the
    recorded key (`shortcut:...`)."""
    return f"intent:{i.card_index}"


def serialize_play_intent(i: PlayIntent) -> dict[str, Any]:
    """JSON shape for one PlayIntent: the printed cost + every valid
    combo. The tier-1 chip renders from {label, card_name, energy_cost,
    power_cost, cost_domains}; if combos.length === 1 the click handler
    auto-skips into combos[0].chain, otherwise the path step stores
    `intent` for the tier-2 disambiguation render."""
    verb = "Cast" if i.play_action == "play_spell" else "Play"
    return {
        "actor": i.actor.value,
        "action": _intent_synthetic_action(i),
        "label": f"{verb} {i.card_name}",
        "card_index": i.card_index,
        "card_name": i.card_name,
        "play_action": i.play_action,
        "energy_cost": i.energy_cost,
        "power_cost": i.power_cost,
        "cost_domains": list(i.cost_domains),
        "combos": [serialize_shortcut(c) for c in i.combos],
    }
