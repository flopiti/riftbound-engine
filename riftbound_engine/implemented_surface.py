"""Single source of truth for "what the engine actually implements".

The web Implementation Planner (riftbound/src/utils/engineImplemented.ts) used
to hand-mirror this engine's registries, and drifted: e.g. RECRUIT and
WHEN_I_MOVE were implemented here but the planner kept showing those abilities
as unimplemented. This module makes the engine itself the source of truth — it
derives the implemented surface from the live registries and renders
``engineImplemented.ts`` from it, so the two can no longer fall out of sync.

Run ``python -m riftbound_engine.implemented_surface --check <path>`` (CI / the
drift test) or ``--write <path>`` (regenerate). From the web app:
``npm run gen:implemented``.

Most of the surface is DERIVED automatically:

  * effect codes      ← effects._REGISTRY + effects._CHOICE_REGISTRY
  * effect patterns   ← effects._PATTERN_HANDLERS + effects._CHOICE_PATTERN_HANDLERS
  * triggers          ← triggers.TRIGGER_EVENT_MAP

A few things can't be derived and are CURATED below, each with a reason:

  * EFFECT_INCLUDES / TRIGGER_INCLUDES — implemented via a path OTHER than the
    effect registry (the Gold-token action, death-replacement), so they don't
    appear in a registry but DO run.
  * EFFECT_EXCLUDES / TRIGGER_EXCLUDES — a handler/mapping exists but does NOT
    faithfully run the ability as written (simplifies a choice, fires too
    broadly), so the planner must not claim them.
  * costs / conditions / passives — the engine has no decorator-based registry
    for these yet; they're charged/evaluated ad hoc, so the implemented set is
    declared here explicitly.
"""

from __future__ import annotations

import argparse
import sys

from . import effects as _effects
from . import triggers as _triggers

# --------------------------------------------------------------------------- #
# Curation. Everything here is a deliberate deviation from "what's in a
# registry", documented so the deviation is auditable.
# --------------------------------------------------------------------------- #

#: Implemented, but NOT through the effect registry — so derivation misses them.
EFFECT_INCLUDES: dict[str, str] = {
    # The Gold token's "Kill this, exhaust: Add 1 Power of any domain" runs as
    # the play:use_gold activated action, not a chain effect handler.
    "ADD_1_POWER_ANY": "implemented as the play:use_gold activated action",
    # Guardian Angel's would-die replacement: the unit is recalled to hand
    # instead of dying, applied in engine._recall_instead_of_death.
    "HEAL_EXHAUST_RECALL": "implemented as a death replacement, not a chain effect",
}

#: Registered, but the handler does NOT run the ability faithfully — the planner
#: must not mark these implemented. (OPPONENT_DISCARD_1 used to live here; it is
#: now a faithful "choose a player; they discard 1" choice effect, so it derives
#: normally.)
EFFECT_EXCLUDES: dict[str, str] = {
    # Emperor's Dais: the RETURN-to-hand half is implemented, but the "play a
    # 2-might Sand Soldier token here" half can't run yet (no Sand Soldier token
    # card exists in the data), so the card isn't faithful end-to-end.
    "RETURN_UNIT_YOU_CONTROL_HERE_HAND_PLAY_SS_HERE": (
        "return half only; Sand Soldier token spawn pending a token card row"
    ),
}

#: Faithful triggers that are deliberately absent from TRIGGER_EVENT_MAP.
TRIGGER_INCLUDES: dict[str, str] = {
    # Replacement marker, not a chain trigger: consulted at the moment a unit
    # would die (engine._death_replacement_for) and substituted for the death.
    "IF_ID_DIE": "would-die replacement marker, consulted at death (not event-driven)",
}

#: Mapped to an event, but the engine fires them too broadly to match the card
#: text — excluded so the planner doesn't over-claim. (Both former entries —
#: AT_START_EACH_FIRST_BEGGINNING_PHASE and FIRST_TIME_PLAYER_CHOOSE_FRIENDLY_
#: WITH_SPELL_EACH_TURN — are now narrowed faithfully in
#: GameEngine._trigger_state_ok, so they derive normally.)
TRIGGER_EXCLUDES: dict[str, str] = {}

#: Costs the engine charges unconditionally by exact code. EXHAUST_THIS is
#: handled specially in the rendered isCostImplemented (gated on ability shape),
#: so it is NOT listed here.
IMPLEMENTED_COSTS: dict[str, str] = {}

#: Cost FAMILIES charged out of the Energy/Power pools (csv_data.parse_pay_cost).
COST_PATTERNS: list[tuple[str, str]] = [
    (r"PAY_\d+_ENERGY", "N Energy"),
    (r"PAY_\d+P", "N Power of any domain"),
    (r"PAY_\d+[A-Z]", "N Power of the card's own domain (PAY_1R = Fury, …)"),
]

#: Conditions the engine evaluates as a real gate.
IMPLEMENTED_CONDITIONS: dict[str, str] = {
    "ATTACHED_THIS_TURN": (
        "gates a continuous equipment passive to the turn the gear was attached "
        "(Brutalizer); checked in attached_might_bonus"
    ),
    "IF_DIED_ALONE": (
        "Lonely Poro's Deathknell: fires only when no other friendly unit shares "
        "the dying unit's location; evaluated in engine._condition_met at ON_DEATH"
    ),
    "IF_1+_UNIT_MIGHTY": (
        "Sunken Temple: fires only when the conquering player has a 5+-Might "
        "(Mighty) unit at this battlefield; evaluated in engine._condition_met "
        "at ON_CONQUER"
    ),
    "WHILE_AT_BATTLEFIELD": (
        "Vex, Apathetic: gates its on-opponent-play trigger to while the source "
        "is at a battlefield; evaluated in engine._condition_met"
    ),
}

#: Continuous/passive effect codes with real engine support (exact). The keyword
#: grants below fold into a unit's property total via the aura/equipment layer
#: (abilities.AURA_GRANTS / attached_*_bonus, summed in engine.aura_bonus), not a
#: registry — so they're declared here.
IMPLEMENTED_PASSIVES: dict[str, str] = {
    # A unit's own [Deflect] keyword (read from card text) AND equipment that
    # grants it (Hexdrinker's bare DEFLECT). Taxed by the chain-push gate.
    "DEFLECT": "keyword Deflect: own (card text) + equipment grant; taxed at target choice",
    # Allay: "while I'm at a battlefield, your other units here have [Deflect]."
    "OTHER_AT_THS_BF_DEFLECT": "aura: grants friendly units at the source's battlefield [Deflect]",
    # Trifarian War Camp: "units here have +1 Might." Folded into effective_unit_might.
    "UNITS_HERE_HAVE_+1M": "aura: +1 Might to units at this battlefield",
    # Void Gate: "Spells and abilities deal 1 Bonus Damage to units here."
    # Applied by engine._bonus_damage_at when a Deal action targets a unit at
    # this battlefield (rules 712-715).
    "SPELLS_ABILITIES_DEALING_DMG_DEAL_1_BONUS": "battlefield aura: +1 Bonus Damage to Deal actions hitting units here (Void Gate)",
    # Frozen Fortress: "At the start of each player's Beginning Phase, deal 1 to
    # each unit here." Resolved in engine._apply_beginning_phase_bf_damage
    # ahead of HOLD scoring.
    "DEAL_1_DAMAGE_TO_UNITS_HERE": "battlefield: deals 1 to each unit here at the start of each Beginning Phase (Frozen Fortress)",
}

#: Passive FAMILIES the engine applies continuously.
PASSIVE_PATTERNS: list[tuple[str, str]] = [
    (r"UNIT_ATTACHED_[+-]\d+M", "equipment grants the attached unit ±N Might"),
    (r"SHIELD_\d+", "equipment [Shield N]: +N Might to the host while it defends"),
]


# --------------------------------------------------------------------------- #
# Derivation.
# --------------------------------------------------------------------------- #
def implemented_effects() -> set[str]:
    """Exact effect codes the engine runs as-written: instant + choice
    registries, minus the unfaithful excludes, plus the non-registry includes.
    (Pattern families are reported separately by ``effect_patterns``.)"""
    derived = set(_effects._REGISTRY) | set(_effects._CHOICE_REGISTRY)
    return (derived - set(EFFECT_EXCLUDES)) | set(EFFECT_INCLUDES)


def effect_patterns() -> list[str]:
    """Signed-amount / family regexes (instant + choice), in engine order."""
    pats = [p.pattern for p, _ in _effects._PATTERN_HANDLERS]
    pats += [p.pattern for p, _ in _effects._CHOICE_PATTERN_HANDLERS]
    return pats


def mapped_triggers() -> set[str]:
    """Trigger codes wired to an emitted event, minus the over-broad excludes,
    plus the faithful non-mapped includes."""
    derived = set(_triggers.TRIGGER_EVENT_MAP)
    return (derived - set(TRIGGER_EXCLUDES)) | set(TRIGGER_INCLUDES)


def surface_data() -> dict:
    """The implemented surface as JSON-serializable data — the SAME content the
    generated ``engineImplemented.ts`` holds, but served LIVE so the web
    Implementation tracker never goes stale (no ``gen:implemented`` step needed).
    Mirrors the sets/patterns ``render_typescript`` emits."""
    return {
        "triggers": sorted(mapped_triggers()),
        "effects": sorted(implemented_effects()),
        "effectPatterns": effect_patterns(),
        "costs": sorted(IMPLEMENTED_COSTS),
        "costPatterns": [p for p, _ in COST_PATTERNS],
        "conditions": sorted(IMPLEMENTED_CONDITIONS),
        "passives": sorted(IMPLEMENTED_PASSIVES),
        "passivePatterns": [p for p, _ in PASSIVE_PATTERNS],
    }


# --------------------------------------------------------------------------- #
# TypeScript rendering.
# --------------------------------------------------------------------------- #
_HEADER = """\
/* AUTO-GENERATED — DO NOT EDIT BY HAND.
 *
 * Source of truth: riftbound-engine/riftbound_engine/implemented_surface.py
 * Regenerate:      npm run gen:implemented   (or python -m
 *                  riftbound_engine.implemented_surface --write <this file>)
 *
 * What the Python engine actually implements, mirrored here so the
 * Implementation Planner can SAY which tagged abilities really run. A trigger
 * not in MAPPED_TRIGGERS never fires; an effect not in IMPLEMENTED_EFFECTS
 * resolves as a logged no-op. Curated exceptions (effects/triggers implemented
 * via another path, or deliberately withheld) are encoded in the generator.
 */
"""

_LOGIC = """\
/** True if the engine runs ``code`` as-written — exact handler OR a dynamic
 *  family pattern. Use this instead of `IMPLEMENTED_EFFECTS.has`. */
export function isEffectImplemented(code: string): boolean {
  return IMPLEMENTED_EFFECTS.has(code) || IMPLEMENTED_EFFECT_PATTERNS.some((re) => re.test(code))
}

/** Whether the engine can actually charge ``code``. EXHAUST_THIS is handled
 *  both as a triggered ability's "you may exhaust me" cost and an activated
 *  ability's "exhaust: do X" cost; Energy/Power PAY_* costs are charged from
 *  the pools. Costs the engine can't pay (KILL_THIS, RECYCLE_*, SPEND_BUFF,
 *  XP, …) stay unimplemented. */
export function isCostImplemented(code: string, _hasTrigger: boolean): boolean {
  if (code === 'EXHAUST_THIS') return true
  if (IMPLEMENTED_COST_PATTERNS.some((re) => re.test(code))) return true
  return IMPLEMENTED_COSTS.has(code)
}

/** True if the engine evaluates ``code`` as a real gating condition. */
export function isConditionImplemented(code: string): boolean {
  return IMPLEMENTED_CONDITIONS.has(code)
}

/** True if the engine applies the continuous ``code`` as-written. */
export function isPassiveImplemented(code: string): boolean {
  return IMPLEMENTED_PASSIVES.has(code) || IMPLEMENTED_PASSIVE_PATTERNS.some((re) => re.test(code))
}

export type AbilityImplStatus = 'full' | 'partial' | 'none'

/** Whether an ability runs end-to-end in the engine, exactly as written.
 *  `full` = every component it has is implemented; `partial` = some but not
 *  all; `none` = nothing is. */
export function abilityImplStatus(ability: {
  triggers: string[]
  conditions: string[]
  costs: string[]
  activeEffects: string[]
  passiveEffects: string[]
}): AbilityImplStatus {
  const trigImpl = ability.triggers.filter((t) => MAPPED_TRIGGERS.has(t)).length
  const actImpl = ability.activeEffects.filter((e) => isEffectImplemented(e)).length
  const pasImpl = ability.passiveEffects.filter((p) => isPassiveImplemented(p)).length

  const hasTrigger = ability.triggers.length > 0
  const trigsOk = ability.triggers.every((t) => MAPPED_TRIGGERS.has(t))
  const actsOk = ability.activeEffects.every((e) => isEffectImplemented(e))
  const passivesOk = ability.passiveEffects.every((p) => isPassiveImplemented(p))
  const costsOk = ability.costs.every((c) => isCostImplemented(c, hasTrigger))
  const condsOk = ability.conditions.every((c) => IMPLEMENTED_CONDITIONS.has(c))

  const hasContent =
    ability.triggers.length + ability.activeEffects.length + ability.passiveEffects.length > 0
  if (hasContent && trigsOk && actsOk && passivesOk && costsOk && condsOk) return 'full'
  if (trigImpl + actImpl + pasImpl > 0) return 'partial'
  return 'none'
}
"""


def _ts_string_set(name: str, items: dict[str, str], derived_note: str) -> str:
    """Render an ``export const NAME = new Set<string>([...])`` with a provenance
    note and a trailing ``// reason`` on each curated entry."""
    lines = [f"/** {derived_note} */", f"export const {name} = new Set<string>(["]
    for code in sorted(items):
        reason = items[code]
        suffix = f" // {reason}" if reason else ""
        lines.append(f"  {_ts_str(code)},{suffix}")
    lines.append("])")
    return "\n".join(lines)


def _ts_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _ts_pattern_array(name: str, pats: list[tuple[str, str]], note: str) -> str:
    lines = [f"/** {note} */", f"const {name}: RegExp[] = ["]
    for pat, reason in pats:
        suffix = f" // {reason}" if reason else ""
        lines.append(f"  /^{pat}$/,{suffix}")
    lines.append("]")
    return "\n".join(lines)


def render_typescript() -> str:
    """The full ``engineImplemented.ts`` content, deterministically ordered."""
    effects_set = {c: "" for c in implemented_effects()}
    # Re-annotate curated includes so their reason shows in the output.
    for code, reason in EFFECT_INCLUDES.items():
        effects_set[code] = reason

    triggers_set = {c: "" for c in mapped_triggers()}
    for code, reason in TRIGGER_INCLUDES.items():
        triggers_set[code] = reason

    eff_pats = [(p, "") for p in effect_patterns()]

    blocks = [
        _HEADER,
        _ts_string_set(
            "MAPPED_TRIGGERS",
            triggers_set,
            "Trigger codes wired to an emitted event ↔ triggers.TRIGGER_EVENT_MAP "
            "(curated: over-broad mappings withheld, replacement markers added).",
        ),
        _ts_string_set(
            "IMPLEMENTED_EFFECTS",
            effects_set,
            "Exact effect codes with a faithful handler ↔ effects._REGISTRY + "
            "_CHOICE_REGISTRY (curated includes/excludes in the generator).",
        ),
        _ts_pattern_array(
            "IMPLEMENTED_EFFECT_PATTERNS",
            eff_pats,
            "Effect FAMILIES handled dynamically ↔ effects.register_effect_pattern "
            "/ register_choice_effect_pattern. Any amount in the family works.",
        ),
        _ts_string_set(
            "IMPLEMENTED_COSTS",
            IMPLEMENTED_COSTS,
            "Costs charged unconditionally by exact code (EXHAUST_THIS is gated "
            "in isCostImplemented instead).",
        ),
        _ts_pattern_array(
            "IMPLEMENTED_COST_PATTERNS",
            COST_PATTERNS,
            "Cost FAMILIES charged from the Energy/Power pools (csv_data.parse_pay_cost).",
        ),
        _ts_string_set(
            "IMPLEMENTED_CONDITIONS",
            IMPLEMENTED_CONDITIONS,
            "Condition codes the engine evaluates as a real gate.",
        ),
        _ts_string_set(
            "IMPLEMENTED_PASSIVES",
            IMPLEMENTED_PASSIVES,
            "Continuous/passive effect codes with real engine support (exact).",
        ),
        _ts_pattern_array(
            "IMPLEMENTED_PASSIVE_PATTERNS",
            PASSIVE_PATTERNS,
            "Passive FAMILIES the engine applies continuously.",
        ),
        _LOGIC,
    ]
    return "\n\n".join(b.rstrip() for b in blocks) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate engineImplemented.ts")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--write", metavar="PATH", help="write the generated file to PATH")
    g.add_argument(
        "--check",
        metavar="PATH",
        help="exit non-zero if PATH differs from the generated output",
    )
    args = parser.parse_args(argv)
    content = render_typescript()

    if args.write:
        with open(args.write, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"wrote {args.write} ({len(content)} bytes)")
        return 0
    if args.check:
        with open(args.check, encoding="utf-8") as fh:
            current = fh.read()
        if current != content:
            print(
                f"{args.check} is OUT OF DATE — run `npm run gen:implemented`",
                file=sys.stderr,
            )
            return 1
        print(f"{args.check} is up to date")
        return 0

    sys.stdout.write(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
