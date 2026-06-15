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
    card_accelerate_cost,
    card_domains_of,
    card_energy_of,
    card_equip_cost,
    card_has_accelerate,
    card_has_quick_draw,
    card_is_equipment,
    card_is_reaction,
    card_playable_in_showdown,
    card_power_of,
    card_type_of,
)

_ALL_DOMAINS: tuple[str, ...] = ("Fury", "Calm", "Mind", "Body", "Chaos", "Order")
from .engine import GameEngine, RequiredTo, Rune
from .requirements import spell_playable

#: Sentinel "card index" for the chosen champion's PlayIntent/Shortcut. The
#: champion isn't a hand card, so it has no real hand index; this value is
#: large enough to never collide with one, keeping its synthetic ids
#: (``intent:<idx>`` / ``shortcut:play_champion:<idx>:<key>``) distinct. The
#: ``play_champion`` handler ignores the index entirely.
CHAMPION_CARD_INDEX = 10_000


@dataclass(frozen=True)
class ShortcutStep:
    """One rune action inside a shortcut's payment chain.

    `rune_index` refers to the rune's position in the PLAYER'S CURRENT POOL
    at planning time. The executor (in http_api's /branch/forward) is
    responsible for translating these into the engine's per-step indices,
    which shift when recycle/exhaust_and_recycle POP runes from the pool.
    """

    action: str  # 'exhaust_rune' | 'recycle_rune' | 'exhaust_and_recycle_rune' | 'use_gold'
    #: Rune actions: the rune's index in the player's pool. A 'use_gold' step
    #: instead stores the GEAR index of the ready Gold token being killed, and
    #: ``rune_domain`` is the domain of Power it produces.
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
    #: When set, the chain ends with this raw action instead of
    #: ``play:<play_action>:<card_index>``. Used by equip shortcuts whose final
    #: step is ``play:equip:<gear>:<controller>:<unit>``.
    final_action: str | None = None
    #: Optional override for the shortcut's synthetic identity (branch layoutId
    #: / visited key). Defaults to the play_action+card_index+key form.
    synthetic: str | None = None


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
    #: Optional override for the intent's synthetic identity (branch layoutId /
    #: visited key). Defaults to ``intent:<card_index>``. Equip intents set this
    #: so distinct gear/unit chips don't collide on card_index.
    synthetic: str | None = None


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
        d = "Gold" if s.action == "use_gold" else s.rune_domain
        counts[d] = counts.get(d, 0) + 1
    return "#".join(sorted(f"{d}×{n}" for d, n in counts.items()))


def _rune_outputs(plan: list[ShortcutStep]) -> list[tuple[str, str, int]]:
    """Break a plan down by what each rune PRODUCES, not just which domains are
    tapped — so the UI can show "Energy from a Mind rune" distinctly from "Mind
    power". Each rune step contributes:

      * ``exhaust_rune``             → +1 ENERGY (of its domain)
      * ``recycle_rune``             → +1 POWER  (of its domain)
      * ``exhaust_and_recycle_rune`` → +1 ENERGY AND +1 POWER (one rune, both)

    Returns ``[(kind, domain, count), ...]`` with ``kind`` in
    ``{"energy", "power"}``, energy first then power, each domain-sorted. This
    is why "1 Mind" (one exhaust-and-recycle) and "2 Mind" (exhaust one +
    recycle another) are the SAME thing: both yield 1 Energy + 1 Mind power."""
    energy: dict[str, int] = {}
    power: dict[str, int] = {}
    gold: dict[str, int] = {}
    for s in plan:
        if s.action == "use_gold":  # killed Gold token → +1 Power of its domain
            gold[s.rune_domain] = gold.get(s.rune_domain, 0) + 1
            continue
        if s.action != "recycle_rune":  # exhaust / exhaust_and_recycle → Energy
            energy[s.rune_domain] = energy.get(s.rune_domain, 0) + 1
        if s.action != "exhaust_rune":  # recycle / exhaust_and_recycle → Power
            power[s.rune_domain] = power.get(s.rune_domain, 0) + 1
    out: list[tuple[str, str, int]] = []
    for d, n in sorted(energy.items()):
        out.append(("energy", d, n))
    for d, n in sorted(power.items()):
        out.append(("power", d, n))
    for d, n in sorted(gold.items()):
        out.append(("gold", d, n))
    return out


def _output_key(plan: list[ShortcutStep]) -> str:
    """Dedup key by PRODUCED outputs (Energy-by-domain + power-by-domain). Two
    plans that yield the same Energy/power from the same domains are
    interchangeable to the player even if they tap a different NUMBER of runes
    (e.g. one dual-purpose rune vs two), so they collapse to one chip. Plans
    that differ in WHICH domain supplies the Energy stay distinct."""
    return "|".join(f"{k}:{d}×{n}" for k, d, n in _rune_outputs(plan))


def _domain_counts(plan: list[ShortcutStep]) -> list[tuple[str, int]]:
    """Domain → count, sorted alphabetically for stable display order."""
    counts: dict[str, int] = {}
    for s in plan:
        d = "Gold" if s.action == "use_gold" else s.rune_domain
        counts[d] = counts.get(d, 0) + 1
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


def _enumerate_rune_plans(
    e_gap: int,
    p_gap: int,
    runes: list[Rune],
    allowed: set[str],
) -> list[list[ShortcutStep]]:
    """Every DISTINCT (by produced output) rune-only plan covering the given
    Energy/Power gaps. Returns ``[[]]`` (one empty plan) when both gaps are
    already <= 0 (nothing to pay from runes), and ``[]`` when the gaps can't be
    covered from this pool."""
    if e_gap <= 0 and p_gap <= 0:
        return [[]]
    n = len(runes)
    if n == 0:
        return []
    max_size = min(n, e_gap + p_gap)
    PLAN_CAP = 64
    seen_action_keys: set[str] = set()
    domain_keys_seen: set[str] = set()
    all_plans: list[list[ShortcutStep]] = []
    for size in range(1, max_size + 1):
        for combo in itertools.combinations(range(n), size):
            mask = 0
            for i in combo:
                mask |= 1 << i
            for plan in _solve_subset(mask, runes, allowed, e_gap, p_gap):
                k = _canonical_key(plan)
                if k in seen_action_keys:
                    continue
                seen_action_keys.add(k)
                all_plans.append(plan)
                domain_keys_seen.add(_domain_multiset_key(plan))
        if len(domain_keys_seen) >= PLAN_CAP:
            break

    def sort_key(p: list[ShortcutStep]) -> tuple[int, int, str]:
        xr = sum(1 for s in p if s.action == "exhaust_and_recycle_rune")
        rec = sum(1 for s in p if s.action == "recycle_rune")
        return (xr, -rec, _canonical_key(p))

    all_plans.sort(key=sort_key)
    seen_output_keys: set[str] = set()
    plans: list[list[ShortcutStep]] = []
    for p in all_plans:
        ok = _output_key(p)
        if ok in seen_output_keys:
            continue
        seen_output_keys.add(ok)
        plans.append(p)
    plans.sort(key=lambda p: (len(p), _output_key(p)))
    return plans


def _ready_gold_indices(gs, actor: RequiredTo) -> tuple[int, ...]:
    """Gear indices of ``actor``'s READY Gold tokens. Each can be killed to add
    1 Power of any domain, so they're folded into a card's payment plans as an
    extra power source (never surfaced as a standalone action). Empty if none."""
    gears = gs.player_1_gears if actor == RequiredTo.PLAYER_1 else gs.player_2_gears
    return tuple(i for i, g in enumerate(gears) if g.card == "Gold" and not g.exhausted)


def _plan_payments(
    energy_cost: int,
    power_cost: int,
    cost_domains: tuple[str, ...],
    runes: list[Rune],
    current_energy: int,
    current_power: dict[str, int],
    gold_indices: tuple[int, ...] = (),
) -> list[list[ShortcutStep]]:
    """Enumerate every DISTINCT plan that covers the cost gaps. Runes supply
    Energy (exhaust) and/or domain Power (recycle); each READY Gold token in
    ``gold_indices`` can instead supply 1 Power of an allowed domain (killing
    the token). Returns ``[]`` if the card is already free-to-play or can't be
    paid.

    Gold-using plans are offered ALONGSIDE the pure-rune plans (the player picks
    whether to spend a token), de-duplicated by produced output so a gold-power
    plan stays distinct from a rune-power one."""
    e_gap0 = max(0, energy_cost - current_energy)
    available_power = sum(current_power.get(d, 0) for d in cost_domains)
    p_gap0 = max(0, power_cost - available_power)
    if e_gap0 == 0 and p_gap0 == 0:
        return []
    allowed = set(cost_domains)
    # A Gold token produces 1 Power of any domain; power is fungible across the
    # cost's domains, so produce the card's first listed domain.
    gold_domain = cost_domains[0] if cost_domains else None
    max_gold = min(len(gold_indices), p_gap0) if gold_domain is not None else 0

    seen_output_keys: set[str] = set()
    combined: list[list[ShortcutStep]] = []
    for k in range(0, max_gold + 1):
        gold_steps = [
            ShortcutStep("use_gold", gold_indices[j], gold_domain) for j in range(k)
        ]
        for rune_plan in _enumerate_rune_plans(e_gap0, p_gap0 - k, list(runes), allowed):
            plan = gold_steps + rune_plan
            ok = _output_key(plan)
            if ok in seen_output_keys:
                continue
            seen_output_keys.add(ok)
            combined.append(plan)
    # Prefer plans that spend FEWER gold tokens (keep the ramp), then fewer
    # total steps, then a stable output key.
    combined.sort(
        key=lambda p: (
            sum(1 for s in p if s.action == "use_gold"),
            len(p),
            _output_key(p),
        )
    )
    return combined


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
    verb = (
        "Cast"
        if play_action == "play_spell"
        else "Accelerate"
        if play_action == "accelerate"
        else "Play"
    )
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


def _picker_phase(gs, actor: RequiredTo) -> tuple[bool | None, bool]:
    """Decide whether ``actor`` gets a play-picker right now and how it's
    scoped, shared by ``compute_play_intents`` and ``compute_shortcuts``.

    Returns ``(reaction_only, showdown_only)``:
      • ``(None, False)`` — this actor gets NO picker right now.
      • open chain → only the priority holder, ``(True, False)`` ([Reaction]
        spells only).
      • open showdown (no chain yet) → only the FOCUS holder, ``(False, True)``
        (Action/Reaction spells the showdown rules allow). This is what makes
        casting IN a showdown use the same pre-costed picker as a normal turn
        instead of hand-tapping runes.
      • otherwise → only the active player, ``(False, False)`` (any card).

    Callers handle the coarse ``pending_play``/``pending_spell_choice``/
    ``pending_payment``/``pending_combat`` short-circuits before calling this.
    """
    chain = gs.pending_chain
    if chain is not None:
        return (True, False) if actor == chain.priority else (None, False)
    showdown = gs.pending_showdown
    if showdown is not None:
        return (False, True) if actor == showdown.focus_holder else (None, False)
    if gs.current_player != actor:
        return (None, False)
    return (False, False)


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
    if gs.pending_combat is not None:
        # Mid-combat the only legal actions are the damage assignments
        # surfaced by the engine — no cards may be played.
        return []
    if getattr(gs, "pending_spell_repeat", None) is not None:
        # A [Repeat] decision owns the clock — only the repeat picker
        # (compute_repeat_intents) and the decline option are valid.
        return []
    if getattr(gs, "pending_accelerate", None) is not None:
        # An [Accelerate] decision owns the clock — only the accelerate picker
        # (compute_accelerate_intents) and the decline option are valid.
        return []
    if getattr(gs, "pending_ability_payment", None) is not None:
        return []
    if getattr(gs, "pending_ability_payment", None) is not None:
        return []  # an any-type Power payment owns the clock
    if getattr(gs, "pending_ability_cost", None) is not None:
        return []  # a triggered ability's pay-cost decision owns the clock
    if getattr(gs, "pending_effect_choice", None) is not None:
        return []
    if actor not in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
        return []
    reaction_only, showdown_only = _picker_phase(gs, actor)
    if reaction_only is None:
        return []  # this actor gets no picker right now

    # Note: we do NOT early-return on an empty hand — the chosen champion can
    # still be playable even when the hand is empty (see the champion block
    # after the hand loop).
    hand = (gs.player_1_hand if actor == RequiredTo.PLAYER_1 else gs.player_2_hand) or []
    runes = gs.player_1_runes if actor == RequiredTo.PLAYER_1 else gs.player_2_runes
    current_energy = (
        gs.player_1_energy if actor == RequiredTo.PLAYER_1 else gs.player_2_energy
    )
    current_power = dict(
        gs.player_1_power if actor == RequiredTo.PLAYER_1 else gs.player_2_power
    )
    # Ready Gold tokens are offered as a payment source only at normal action
    # timing (not as reactions / in showdowns, where the use_gold handler would
    # reject them).
    gold_indices = (
        _ready_gold_indices(gs, actor)
        if not reaction_only and not showdown_only
        else ()
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
        if showdown_only and not (
            card_type == "Spell" and card_playable_in_showdown(card)
        ):
            # In a showdown only Action/Reaction spells can be cast — no
            # fresh units/gears, and no spells the showdown rules forbid.
            continue
        if reaction_only and not card_is_reaction(card):
            continue
        # Spells must also have a satisfiable Spell Choice Requirement —
        # a valid target set on the current board — not just an affordable
        # cost. See riftbound_engine/requirements.py. (Units have no such
        # requirement today.)
        if card_type == "Spell" and not spell_playable(gs, card, caster=actor):
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
            gold_indices=gold_indices,
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

    # The chosen champion — a fixed Unit beside the board — is playable at
    # normal action timing like a hand unit (but never as a reaction or in a
    # showdown). Offered once, until it's been played. Uses a sentinel
    # card_index so its synthetic ids don't collide with any hand card's.
    if not reaction_only and not showdown_only:
        deck = gs.player_1_deck if actor == RequiredTo.PLAYER_1 else gs.player_2_deck
        played = (
            gs.player_1_champion_played
            if actor == RequiredTo.PLAYER_1
            else gs.player_2_champion_played
        )
        champ = deck.chosen_champion if deck is not None else None
        if champ and not played:
            energy_cost = card_energy_of(champ) or 0
            power_cost = card_power_of(champ) or 0
            cost_domains = tuple(card_domains_of(champ))
            plans = _plan_payments(
                energy_cost,
                power_cost,
                cost_domains,
                list(runes),
                current_energy,
                current_power,
                gold_indices=gold_indices,
            )
            if plans:
                combos = tuple(
                    _build_shortcut(
                        actor=actor,
                        card_index=CHAMPION_CARD_INDEX,
                        card_name=champ,
                        play_action="play_champion",
                        energy_cost=energy_cost,
                        power_cost=power_cost,
                        cost_domains=cost_domains,
                        plan=plan,
                    )
                    for plan in plans
                )
                out.append(
                    PlayIntent(
                        actor=actor,
                        card_index=CHAMPION_CARD_INDEX,
                        card_name=champ,
                        play_action="play_champion",
                        energy_cost=energy_cost,
                        power_cost=power_cost,
                        cost_domains=cost_domains,
                        combos=combos,
                    )
                )
    return out


#: Sentinel card index for [Repeat] payment shortcuts (the repeat isn't a
#: hand card, so it has no real index).
REPEAT_CARD_INDEX = 10_001


def _repeat_cost_as_plan_inputs(cost: dict) -> tuple[int, int, tuple[str, ...]]:
    """Convert a parsed [Repeat] cost ({energy, power:{dom:n}, any_power}) into
    the (energy_cost, power_cost, cost_domains) shape ``_plan_payments`` wants.

    The 18 authored repeat costs are energy-only, energy + one specific-domain
    rune, or energy + an any-type rune — never specific+any mixed — so this
    maps cleanly: a specific-domain power restricts ``cost_domains`` to that
    domain; an any-type power lets any domain pay."""
    energy = int(cost.get("energy", 0))
    power_map = dict(cost.get("power", {}))
    any_power = int(cost.get("any_power", 0))
    specific = sum(power_map.values())
    if specific:
        return energy, specific, tuple(power_map.keys())
    if any_power:
        return energy, any_power, _ALL_DOMAINS
    return energy, 0, ()


def _build_repeat_shortcut(
    actor: RequiredTo, card_name: str, plan: list[ShortcutStep]
) -> Shortcut:
    """A payment plan for repeating ``card_name``: rune steps then
    ``play:choose_repeat:yes``. Empty ``plan`` ⇒ already affordable from the
    pool (the chip is a bare "Repeat", no rune cost annotation)."""
    domain_counts = _domain_counts(plan)
    if domain_counts:
        plan_desc = ", ".join(f"{n} {d}" for d, n in domain_counts)
        label = f"Repeat {card_name} ({plan_desc})"
    else:
        label = f"Repeat {card_name}"
    key = _domain_multiset_key(plan) or "free"
    return Shortcut(
        actor=actor,
        card_index=REPEAT_CARD_INDEX,
        card_name=card_name,
        play_action="repeat",
        energy_cost=0,
        power_cost=0,
        cost_domains=(),
        plan=tuple(plan),
        domain_counts=tuple(domain_counts),
        key=key,
        label=label,
        final_action="play:choose_repeat:yes",
        synthetic=f"shortcut:repeat:{key}",
    )


def compute_repeat_intents(engine: GameEngine, actor: RequiredTo) -> list[PlayIntent]:
    """The [Repeat] payment picker: when a repeat decision is pending for
    ``actor``, one PlayIntent ("Repeat <card>") whose combos are every valid
    way to pay the additional cost — the SAME pre-costed chips the initial
    cast uses. Declining is a separate flat ``play:choose_repeat:no`` option
    surfaced by the engine, not here.

    Returns [] when no repeat is pending for this actor, or when the cost
    can't be paid at all (no affordable plan and not free from the pool) — in
    that case only the decline option is offered."""
    gs = engine._game_state
    repeat = getattr(gs, "pending_spell_repeat", None)
    if repeat is None or repeat.actor != actor:
        return []

    energy_cost, power_cost, cost_domains = _repeat_cost_as_plan_inputs(repeat.cost)
    runes = gs.player_1_runes if actor == RequiredTo.PLAYER_1 else gs.player_2_runes
    current_energy = gs.player_1_energy if actor == RequiredTo.PLAYER_1 else gs.player_2_energy
    current_power = dict(
        gs.player_1_power if actor == RequiredTo.PLAYER_1 else gs.player_2_power
    )
    plans = _plan_payments(
        energy_cost, power_cost, cost_domains, list(runes), current_energy, current_power
    )
    if plans:
        combos = tuple(_build_repeat_shortcut(actor, repeat.card, plan) for plan in plans)
    elif engine.can_afford_equip_cost(actor, repeat.cost):
        # Already affordable from the pool → a single bare "Repeat" chip whose
        # chain is just the commit (no rune steps).
        combos = (_build_repeat_shortcut(actor, repeat.card, []),)
    else:
        return []  # can't pay → only the decline option

    return [
        PlayIntent(
            actor=actor,
            card_index=REPEAT_CARD_INDEX,
            card_name=repeat.card,
            play_action="repeat",
            energy_cost=energy_cost,
            power_cost=power_cost,
            cost_domains=cost_domains,
            combos=combos,
            synthetic="intent:repeat",
        )
    ]


#: Sentinel card index for [Accelerate] payment shortcuts (the accelerate isn't
#: a hand card, so it has no real index).
ACCELERATE_CARD_INDEX = 10_002


def _build_accelerate_shortcut(
    actor: RequiredTo, card_name: str, plan: list[ShortcutStep]
) -> Shortcut:
    """A payment plan for accelerating ``card_name``: rune steps then
    ``play:choose_accelerate:yes``. Empty ``plan`` ⇒ already affordable from
    the pool (a bare "Accelerate" chip, no rune annotation)."""
    domain_counts = _domain_counts(plan)
    if domain_counts:
        plan_desc = ", ".join(f"{n} {d}" for d, n in domain_counts)
        label = f"Accelerate {card_name} ({plan_desc})"
    else:
        label = f"Accelerate {card_name}"
    key = _domain_multiset_key(plan) or "free"
    return Shortcut(
        actor=actor,
        card_index=ACCELERATE_CARD_INDEX,
        card_name=card_name,
        play_action="accelerate",
        energy_cost=0,
        power_cost=0,
        cost_domains=(),
        plan=tuple(plan),
        domain_counts=tuple(domain_counts),
        key=key,
        label=label,
        final_action="play:choose_accelerate:yes",
        synthetic=f"shortcut:accelerate:{key}",
    )


def compute_accelerate_intents(engine: GameEngine, actor: RequiredTo) -> list[PlayIntent]:
    """The [Accelerate] payment picker: when an accelerate decision is pending
    for ``actor``, one PlayIntent ("Accelerate <card>") whose combos are every
    valid way to pay the additional cost — the SAME pre-costed chips a normal
    play uses. Declining is the flat ``play:choose_accelerate:no`` option the
    engine surfaces, not here.

    Returns [] when no accelerate is pending for this actor, or the cost can't
    be paid at all (only the decline option is offered then)."""
    gs = engine._game_state
    acc = getattr(gs, "pending_accelerate", None)
    if acc is None or acc.actor != actor:
        return []

    energy_cost, power_cost, cost_domains = _repeat_cost_as_plan_inputs(acc.cost)
    runes = gs.player_1_runes if actor == RequiredTo.PLAYER_1 else gs.player_2_runes
    current_energy = gs.player_1_energy if actor == RequiredTo.PLAYER_1 else gs.player_2_energy
    current_power = dict(
        gs.player_1_power if actor == RequiredTo.PLAYER_1 else gs.player_2_power
    )
    plans = _plan_payments(
        energy_cost, power_cost, cost_domains, list(runes), current_energy, current_power
    )
    if plans:
        combos = tuple(_build_accelerate_shortcut(actor, acc.card, plan) for plan in plans)
    elif engine.can_afford_equip_cost(actor, acc.cost):
        combos = (_build_accelerate_shortcut(actor, acc.card, []),)
    else:
        return []  # can't pay → only the decline option

    return [
        PlayIntent(
            actor=actor,
            card_index=ACCELERATE_CARD_INDEX,
            card_name=acc.card,
            play_action="accelerate",
            energy_cost=energy_cost,
            power_cost=power_cost,
            cost_domains=cost_domains,
            combos=combos,
            synthetic="intent:accelerate",
        )
    ]


def _build_equip_shortcut(
    actor: RequiredTo,
    gear_index: int,
    gear_name: str,
    unit_name: str,
    controller: str,
    unit_index: int,
    energy_cost: int,
    power_cost: int,
    cost_domains: tuple[str, ...],
    plan: list[ShortcutStep],
) -> Shortcut:
    domain_counts = _domain_counts(plan)
    base_key = _domain_multiset_key(plan)
    # Tier-2 chip text: "To <unit> (<cost>)" — the gear is named on tier 1.
    if domain_counts:
        plan_desc = ", ".join(f"{n} {d}" for d, n in domain_counts)
        label = f"To {unit_name} ({plan_desc})"
    else:
        label = f"To {unit_name}"
    return Shortcut(
        actor=actor,
        card_index=gear_index,
        card_name=gear_name,
        play_action="equip",
        energy_cost=energy_cost,
        power_cost=power_cost,
        cost_domains=cost_domains,
        plan=tuple(plan),
        domain_counts=tuple(domain_counts),
        key=f"{base_key}@{controller}:{unit_index}",
        label=label,
        final_action=f"play:equip:{gear_index}:{controller}:{unit_index}",
        synthetic=f"shortcut:equip:{gear_index}:{controller}:{unit_index}:{base_key}",
    )


def _equip_cost_to_pay(cost: dict[str, object]) -> tuple[int, int, tuple[str, ...]]:
    """Flatten a parsed [Equip] cost into (energy, power_cost, cost_domains) for
    the payment planner. A specific-domain cost keeps its domains; an
    any-type cost spreads across all domains (data never mixes the two)."""
    energy_cost = int(cost.get("energy", 0))
    power = dict(cost.get("power", {}))
    any_power = int(cost.get("any_power", 0))
    if power:
        return energy_cost, sum(power.values()), tuple(power.keys())
    if any_power:
        return energy_cost, any_power, _ALL_DOMAINS
    return energy_cost, 0, ()


def compute_equip_intents(engine: GameEngine, actor: RequiredTo) -> list[PlayIntent]:
    """Pre-costed picker chips for equipping Equipment gears the active player
    has on the board onto ANY unit on the board. Each chip's combos carry the
    rune-payment chain that produces the [Equip] cost, ending in the
    ``play:equip:<gear>:<controller>:<unit>`` action — so the player can equip
    straight from runes (no pre-banked power needed), exactly like playing a
    card. Returns [] outside the active player's normal action turn."""
    gs = engine._game_state
    if (
        gs.pending_play is not None
        or gs.pending_spell_choice is not None
        or gs.pending_payment is not None
        or gs.pending_combat is not None
        or getattr(gs, "pending_spell_repeat", None) is not None
        or getattr(gs, "pending_accelerate", None) is not None
        or getattr(gs, "pending_ability_cost", None) is not None
        or getattr(gs, "pending_ability_payment", None) is not None
        or getattr(gs, "pending_effect_choice", None) is not None
    ):
        return []
    if actor not in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
        return []
    reaction_only, showdown_only = _picker_phase(gs, actor)
    # Equip is a normal-turn play — not a reaction or a showdown play.
    if reaction_only is None or reaction_only or showdown_only:
        return []

    gears = gs.player_1_gears if actor == RequiredTo.PLAYER_1 else gs.player_2_gears
    runes = gs.player_1_runes if actor == RequiredTo.PLAYER_1 else gs.player_2_runes
    current_energy = (
        gs.player_1_energy if actor == RequiredTo.PLAYER_1 else gs.player_2_energy
    )
    current_power = dict(
        gs.player_1_power if actor == RequiredTo.PLAYER_1 else gs.player_2_power
    )
    # Equipment attaches only to a unit YOU control (per the card text), so
    # the target candidates are the caster's own units.
    own_controller = actor.value
    own_units = gs.player_1_units if actor == RequiredTo.PLAYER_1 else gs.player_2_units
    board_units = ((own_controller, own_units),)

    out: list[PlayIntent] = []
    for gi, gear in enumerate(gears):
        if not card_is_equipment(gear.card):
            continue
        cost = card_equip_cost(gear.card)
        if cost is None:
            continue
        energy_cost, power_cost, cost_domains = _equip_cost_to_pay(cost)
        available = sum(current_power.get(d, 0) for d in cost_domains)
        e_gap = max(0, energy_cost - current_energy)
        p_gap = max(0, power_cost - available)
        if e_gap == 0 and p_gap == 0:
            plans: list[list[ShortcutStep]] = [[]]  # already affordable
        else:
            plans = _plan_payments(
                energy_cost, power_cost, cost_domains, list(runes), current_energy, current_power
            )
            if not plans:
                continue  # can't pay even by tapping/recycling runes
        # One tier-1 chip per equipment ("Equip <gear>"); tier-2 combos are the
        # target units × payment plans ("To <unit> (<cost>)").
        combos = tuple(
            _build_equip_shortcut(
                actor, gi, gear.card, units[ui].card, controller, ui,
                energy_cost, power_cost, cost_domains, plan,
            )
            for controller, units in board_units
            for ui in range(len(units))
            for plan in plans
        )
        if not combos:
            continue  # no target units → nothing to equip
        out.append(
            PlayIntent(
                actor=actor,
                card_index=gi,
                card_name=f"Equip {gear.card}",
                play_action="equip",
                energy_cost=energy_cost,
                power_cost=power_cost,
                cost_domains=cost_domains,
                combos=combos,
                synthetic=f"intent:equip:{gi}",
            )
        )
    return out


def _build_quick_draw_shortcut(
    actor: RequiredTo,
    hand_index: int,
    card_name: str,
    unit_name: str,
    controller: str,
    unit_index: int,
    energy_cost: int,
    power_cost: int,
    cost_domains: tuple[str, ...],
    plan: list[ShortcutStep],
) -> Shortcut:
    """A Quick-Draw combo: pay the CARD cost (rune steps) then play the gear
    and attach it for free to ``unit_index`` (``play:quick_draw:…``). The tier-1
    chip names the gear; this tier-2 chip is the target unit + payment."""
    domain_counts = _domain_counts(plan)
    base_key = _domain_multiset_key(plan)
    if domain_counts:
        plan_desc = ", ".join(f"{n} {d}" for d, n in domain_counts)
        label = f"To {unit_name} ({plan_desc})"
    else:
        label = f"To {unit_name}"
    return Shortcut(
        actor=actor,
        card_index=hand_index,
        card_name=card_name,
        play_action="quick_draw",
        energy_cost=energy_cost,
        power_cost=power_cost,
        cost_domains=cost_domains,
        plan=tuple(plan),
        domain_counts=tuple(domain_counts),
        key=f"{base_key}@{controller}:{unit_index}",
        label=label,
        final_action=f"play:quick_draw:{hand_index}:{controller}:{unit_index}",
        synthetic=f"shortcut:quick_draw:{hand_index}:{controller}:{unit_index}:{base_key}",
    )


def compute_quick_draw_intents(engine: GameEngine, actor: RequiredTo) -> list[PlayIntent]:
    """Pre-costed picker chips for playing a ``[Quick-Draw]`` Equipment from
    hand and attaching it for FREE to a unit you control. Because Quick-Draw
    grants [Reaction], these are offered at REACTION speed (an open chain or a
    showdown where ``actor`` may act) as well as on ``actor``'s normal turn.
    Each combo pays only the CARD cost and ends in
    ``play:quick_draw:<hand_idx>:<controller>:<unit_idx>`` — never the [Equip]
    cost. Offered only when the actor controls at least one unit."""
    gs = engine._game_state
    if (
        gs.pending_play is not None
        or gs.pending_spell_choice is not None
        or gs.pending_payment is not None
        or gs.pending_combat is not None
        or getattr(gs, "pending_spell_repeat", None) is not None
        or getattr(gs, "pending_accelerate", None) is not None
        or getattr(gs, "pending_ability_cost", None) is not None
        or getattr(gs, "pending_ability_payment", None) is not None
        or getattr(gs, "pending_effect_choice", None) is not None
    ):
        return []
    if actor not in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
        return []
    reaction_only, showdown_only = _picker_phase(gs, actor)
    if reaction_only is None and not showdown_only:
        return []  # this actor gets no picker right now

    hand = (gs.player_1_hand if actor == RequiredTo.PLAYER_1 else gs.player_2_hand) or []
    units = gs.player_1_units if actor == RequiredTo.PLAYER_1 else gs.player_2_units
    if not units:
        return []  # nothing to attach to → Quick-Draw not offered
    runes = gs.player_1_runes if actor == RequiredTo.PLAYER_1 else gs.player_2_runes
    current_energy = gs.player_1_energy if actor == RequiredTo.PLAYER_1 else gs.player_2_energy
    current_power = dict(gs.player_1_power if actor == RequiredTo.PLAYER_1 else gs.player_2_power)
    own = actor.value

    seen: set[str] = set()
    out: list[PlayIntent] = []
    for i, card in enumerate(hand):
        if card in seen or not card_has_quick_draw(card):
            continue
        energy_cost = card_energy_of(card) or 0
        power_cost = card_power_of(card) or 0
        cost_domains = tuple(card_domains_of(card))
        available = sum(current_power.get(d, 0) for d in cost_domains)
        e_gap = max(0, energy_cost - current_energy)
        p_gap = max(0, power_cost - available)
        if e_gap == 0 and p_gap == 0:
            plans: list[list[ShortcutStep]] = [[]]  # already affordable from the pool
        else:
            plans = _plan_payments(
                energy_cost, power_cost, cost_domains, list(runes), current_energy, current_power
            )
            if not plans:
                continue  # can't pay even by tapping/recycling runes
        combos = tuple(
            _build_quick_draw_shortcut(
                actor, i, card, units[ui].card, own, ui, energy_cost, power_cost, cost_domains, plan
            )
            for ui in range(len(units))
            for plan in plans
        )
        if not combos:
            continue
        seen.add(card)
        out.append(
            PlayIntent(
                actor=actor,
                card_index=i,
                card_name=card,
                play_action="quick_draw",
                energy_cost=energy_cost,
                power_cost=power_cost,
                cost_domains=cost_domains,
                combos=combos,
                synthetic=f"intent:quick_draw:{i}",
            )
        )
    return out


def _build_move_shortcut(
    actor: RequiredTo, unit_index: int, unit_name: str, dest: str, dest_label: str
) -> Shortcut:
    return Shortcut(
        actor=actor,
        card_index=unit_index,
        card_name=unit_name,
        play_action="move",
        energy_cost=0,
        power_cost=0,
        cost_domains=(),
        plan=(),
        domain_counts=(),
        key=dest,
        label=f"To {dest_label}",
        final_action=f"play:move_unit:{unit_index}:{dest}",
        synthetic=f"shortcut:move:{unit_index}:{dest}",
    )


def compute_move_intents(engine: GameEngine, actor: RequiredTo) -> list[PlayIntent]:
    """Two-tier move picker: one tier-1 chip per movable (ready) unit
    ("Move <unit>"), whose tier-2 combos are the legal destinations
    ("To Base" / "To <battlefield>"). A unit with a single destination
    auto-applies on click; one with two drops into the tier-2 picker.

    Mirrors the engine's own action-turn move rules (base ↔ battlefield only,
    no BF↔BF). Returns [] outside the active player's normal action turn."""
    gs = engine._game_state
    if (
        gs.pending_play is not None
        or gs.pending_spell_choice is not None
        or gs.pending_payment is not None
        or gs.pending_combat is not None
        or getattr(gs, "pending_spell_repeat", None) is not None
        or getattr(gs, "pending_accelerate", None) is not None
        or getattr(gs, "pending_ability_cost", None) is not None
        or getattr(gs, "pending_ability_payment", None) is not None
        or getattr(gs, "pending_effect_choice", None) is not None
    ):
        return []
    if actor not in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
        return []
    reaction_only, showdown_only = _picker_phase(gs, actor)
    if reaction_only is None or reaction_only or showdown_only:
        return []

    units = gs.player_1_units if actor == RequiredTo.PLAYER_1 else gs.player_2_units
    bf1, bf2 = gs.battlefield_1, gs.battlefield_2

    same_bf_names = bool(bf1) and bf1 == bf2  # disambiguate identical names

    def dest_label(loc: str) -> str:
        if loc == "battlefield_1":
            return f"{bf1} (BF1)" if same_bf_names else (bf1 or "Battlefield 1")
        if loc == "battlefield_2":
            return f"{bf2} (BF2)" if same_bf_names else (bf2 or "Battlefield 2")
        return "Base"

    out: list[PlayIntent] = []
    for ui, u in enumerate(units):
        if u.exhausted:
            continue
        dests = ["battlefield_1", "battlefield_2"] if u.location == "base" else ["base"]
        combos = tuple(
            _build_move_shortcut(actor, ui, u.card, dest, dest_label(dest)) for dest in dests
        )
        out.append(
            PlayIntent(
                actor=actor,
                card_index=ui,
                card_name=f"Move {u.card}",
                play_action="move",
                energy_cost=0,
                power_cost=0,
                cost_domains=(),
                combos=combos,
                synthetic=f"intent:move:{ui}",
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
    if gs.pending_combat is not None:
        return []
    if getattr(gs, "pending_spell_repeat", None) is not None:
        return []
    if getattr(gs, "pending_accelerate", None) is not None:
        return []
    if getattr(gs, "pending_ability_payment", None) is not None:
        return []
    if getattr(gs, "pending_ability_cost", None) is not None:
        return []
    if getattr(gs, "pending_effect_choice", None) is not None:
        return []
    if actor not in (RequiredTo.PLAYER_1, RequiredTo.PLAYER_2):
        return []
    # Mirror compute_play_intents: chain → priority holder, [Reaction] only;
    # showdown → focus holder, showdown-legal spells; else → active player.
    reaction_only, showdown_only = _picker_phase(gs, actor)
    if reaction_only is None:
        return []

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
    gold_indices = (
        _ready_gold_indices(gs, actor)
        if not reaction_only and not showdown_only
        else ()
    )

    out: list[Shortcut] = []
    for i, card in enumerate(hand):
        card_type = card_type_of(card)
        if card_type not in ("Unit", "Spell", "Gear"):
            continue
        if showdown_only and not (
            card_type == "Spell" and card_playable_in_showdown(card)
        ):
            continue
        if reaction_only and not card_is_reaction(card):
            continue
        # Same requirement gate as compute_play_intents: a Spell only
        # appears if it has a valid target set on the current board.
        if card_type == "Spell" and not spell_playable(gs, card, caster=actor):
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
            gold_indices=gold_indices,
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
    gold = [s for s in shortcut.plan if s.action == "use_gold"]
    rune_steps = [s for s in shortcut.plan if s.action != "use_gold"]
    pops = [s for s in rune_steps if s.action != "exhaust_rune"]
    pops_sorted_desc = sorted(pops, key=lambda s: s.rune_index, reverse=True)
    exhausts = [s for s in rune_steps if s.action == "exhaust_rune"]

    out: list[tuple[RequiredTo, str]] = []
    # Gold tokens: each kill POPS a gear, so apply high-index-first to keep the
    # remaining gold targets' indices stable. Independent of the rune pool.
    for g in sorted(gold, key=lambda s: s.rune_index, reverse=True):
        out.append((shortcut.actor, f"play:use_gold:{g.rune_index}:{g.rune_domain}"))
    for p in pops_sorted_desc:
        out.append((shortcut.actor, f"play:{p.action}:{p.rune_index}"))

    pop_indices_asc = sorted(p.rune_index for p in pops)
    for e in exhausts:
        shift = sum(1 for idx in pop_indices_asc if idx < e.rune_index)
        out.append((shortcut.actor, f"play:exhaust_rune:{e.rune_index - shift}"))

    final = shortcut.final_action or f"play:{shortcut.play_action}:{shortcut.card_index}"
    out.append((shortcut.actor, final))
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
        # What each tapped rune PRODUCES, so the chip can show Energy (a tapped/
        # exhausted rune) distinctly from domain power (a recycled rune) instead
        # of a bare per-domain rune count. kind ∈ {"energy","power"}.
        "rune_outputs": [
            {"kind": k, "domain": d, "count": n} for k, d, n in _rune_outputs(list(s.plan))
        ],
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
    return s.synthetic or f"shortcut:{s.play_action}:{s.card_index}:{s.key}"


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
    if i.play_action == "repeat":
        label = f"Repeat {i.card_name}"
    elif i.play_action == "quick_draw":
        label = f"Quick-Draw {i.card_name}"
    elif i.play_action == "accelerate":
        label = f"Accelerate {i.card_name}"
    elif i.play_action in ("equip", "move"):
        label = i.card_name
    else:
        label = f"{verb} {i.card_name}"
    return {
        "actor": i.actor.value,
        "action": i.synthetic or _intent_synthetic_action(i),
        "label": label,
        "card_index": i.card_index,
        "card_name": i.card_name,
        "play_action": i.play_action,
        "energy_cost": i.energy_cost,
        "power_cost": i.power_cost,
        "cost_domains": list(i.cost_domains),
        "combos": [serialize_shortcut(c) for c in i.combos],
    }
