"""Human-readable labels for raw engine action strings.

This is a server-side port of the client labeler in
``riftbound/src/utils/actionLabel.ts``. It exists so engine-generated moves
(specifically the ADVANCED fake-fill fast-forward in ``http_api.py``) can be
recorded into the branch tree with the SAME clean labels the interactive
client produces, instead of the raw ``play:move_unit:0:battlefield_1`` strings.

Keep this in sync with ``actionLabel.ts``: the two must format identically so a
fast-forwarded node and a hand-played node read the same in the branch view.
"""

from __future__ import annotations

import re
from typing import Any

from .csv_data import card_domains_of, card_energy_of, card_power_of

_LOCATION_NAME = {
    "base": "Base",
    "battlefield_1": "Battlefield 1",
    "battlefield_2": "Battlefield 2",
}


def _location_display(loc: str, bf1: str | None, bf2: str | None) -> str:
    if loc == "battlefield_1" and bf1:
        return bf1
    if loc == "battlefield_2" and bf2:
        return bf2
    return _LOCATION_NAME.get(loc, loc)


def _destination_display(loc: str, bf1: str | None, bf2: str | None) -> str:
    """Like ``_location_display`` but appends a slot marker ("(BF1)"/"(BF2)")
    when both battlefields share the same card name, so a move target is
    unambiguous. Mirrors ``destinationDisplay`` in actionLabel.ts."""
    base = _location_display(loc, bf1, bf2)
    if loc not in ("battlefield_1", "battlefield_2"):
        return base
    if bf1 and bf2 and bf1 == bf2:
        slot = "BF1" if loc == "battlefield_1" else "BF2"
        return f"{base} ({slot})"
    return base


def _format_cost(card: str | None) -> str:
    if not card:
        return ""
    energy = card_energy_of(card) or 0
    power = card_power_of(card) or 0
    parts: list[str] = []
    if energy > 0:
        parts.append(f"{energy} Energy")
    if power > 0:
        domains = card_domains_of(card)
        domain_label = "/".join(domains) if domains else "Power"
        parts.append(f"{power} {domain_label} Power")
    return f" ({', '.join(parts)})" if parts else ""


def _actor_value(actor: Any) -> str:
    """Accept a ``RequiredTo`` enum or its string value."""
    return getattr(actor, "value", actor)


def label_for_action(game_state: Any, actor: Any, action: str) -> str:
    """Translate one raw engine action string into the label the Control /
    branch UI shows. ``game_state`` is the state the action is an option in
    (i.e. BEFORE it is applied), matching how the client labels a live option.

    Unrecognised actions fall through to the raw string, exactly like the
    client's ``actionLabel``.
    """
    gs = game_state
    side = _actor_value(actor)
    is_p1 = side == "player_1"

    hand: list[str] = (gs.player_1_hand if is_p1 else gs.player_2_hand) or []
    runes = (gs.player_1_runes if is_p1 else gs.player_2_runes) or []
    units = (gs.player_1_units if is_p1 else gs.player_2_units) or []
    bf1 = gs.battlefield_1
    bf2 = gs.battlefield_2
    pending_card = gs.pending_play.card if gs.pending_play is not None else None

    if action == "play:end_turn":
        return "End turn"
    if action == "play:pass_showdown":
        return "Pass (showdown)"
    if action == "play:pass_priority":
        return "Pass (priority)"

    m = re.fullmatch(r"play:play_unit:(\d+)", action)
    if m:
        idx = int(m.group(1))
        card = hand[idx] if idx < len(hand) else None
        cost = _format_cost(card)
        return f"Play {card}{cost}" if card else f"Play hand[{idx}]{cost}"

    m = re.fullmatch(r"play:play_spell:(\d+)", action)
    if m:
        idx = int(m.group(1))
        card = hand[idx] if idx < len(hand) else None
        cost = _format_cost(card)
        return f"Cast {card}{cost}" if card else f"Cast hand[{idx}]{cost}"

    m = re.fullmatch(r"play:choose_location:(.+)", action)
    if m:
        loc = m.group(1)
        specific = "on Base" if loc == "base" else f"on {_destination_display(loc, bf1, bf2)}"
        return f"Place {pending_card} {specific}" if pending_card else f"Place {specific}"

    m = re.fullmatch(r"play:choose_spell_destination:(.+)", action)
    if m:
        loc = m.group(1)
        where = "Base" if loc == "base" else _destination_display(loc, bf1, bf2)
        return f"Move to {where}"

    m = re.fullmatch(r"play:choose_spell_battlefield:(.+)", action)
    if m:
        return f"Target {_destination_display(m.group(1), bf1, bf2)}"

    m = re.fullmatch(r"play:choose_spell_gear:(g[12])-(\d+)", action)
    if m:
        gears = gs.player_1_gears if m.group(1) == "g1" else gs.player_2_gears
        gi = int(m.group(2))
        name = gears[gi].card if 0 <= gi < len(gears) else f"gear #{gi}"
        return f"Target {name}"

    m = re.fullmatch(r"play:choose_spell_trash:(t[12])-(\d+)", action)
    if m:
        pile = gs.player_1_trash if m.group(1) == "t1" else gs.player_2_trash
        ti = int(m.group(2))
        name = pile[ti] if 0 <= ti < len(pile) else f"trash #{ti}"
        return f"Recover {name}"

    m = re.fullmatch(r"play:choose_spell_chain:c-(\d+)", action)
    if m:
        ci = int(m.group(1))
        items = gs.pending_chain.items if gs.pending_chain is not None else []
        name = items[ci].card if 0 <= ci < len(items) else f"spell #{ci}"
        return f"Counter {name}"

    m = re.fullmatch(r"play:choose_spell_location:(.+)", action)
    if m:
        loc = m.group(1)
        where = "Base" if loc == "base" else _destination_display(loc, bf1, bf2)
        return f"Target {where}"

    m = re.fullmatch(r"play:exhaust_rune:(\d+)", action)
    if m:
        idx = int(m.group(1))
        domain = runes[idx].domain if idx < len(runes) else None
        return f"Exhaust {domain} rune (+1 Energy)" if domain else f"Exhaust rune #{idx}"

    m = re.fullmatch(r"play:recycle_rune:(\d+)", action)
    if m:
        idx = int(m.group(1))
        domain = runes[idx].domain if idx < len(runes) else None
        return f"Recycle {domain} rune (+1 {domain} Power)" if domain else f"Recycle rune #{idx}"

    m = re.fullmatch(r"play:exhaust_and_recycle_rune:(\d+)", action)
    if m:
        idx = int(m.group(1))
        domain = runes[idx].domain if idx < len(runes) else None
        return (
            f"Exhaust & Recycle {domain} rune (+1 Energy, +1 {domain} Power)"
            if domain
            else f"Exhaust & Recycle rune #{idx}"
        )

    m = re.fullmatch(r"play:equip:(\d+):(player_[12]):(\d+)", action)
    if m:
        gi, ctrl, ui = int(m.group(1)), m.group(2), int(m.group(3))
        gears = gs.player_1_gears if is_p1 else gs.player_2_gears
        tunits = gs.player_1_units if ctrl == "player_1" else gs.player_2_units
        gname = gears[gi].card if 0 <= gi < len(gears) else f"gear #{gi}"
        uname = tunits[ui].card if 0 <= ui < len(tunits) else f"unit #{ui}"
        return f"Equip {gname} to {uname}"

    m = re.fullmatch(r"play:move_unit:(\d+):(.+)", action)
    if m:
        idx = int(m.group(1))
        dest = m.group(2)
        unit = units[idx] if idx < len(units) else None
        from_label = _location_display(unit.location, bf1, bf2) if unit else f"unit #{idx}"
        to_label = _destination_display(dest, bf1, bf2)
        unit_name = unit.card if unit else f"unit #{idx}"
        return f"Move {unit_name} from {from_label} → {to_label}"

    m = re.fullmatch(r"play:choose_effect_target:(.+)", action)
    if m:
        token = m.group(1)
        choice = getattr(gs, "pending_effect_choice", None)
        src = (choice.source_card if choice else None) or "Effect"
        if token == "pass":
            return f"{src}: decline"
        tm = re.fullmatch(r"p([12])-(\d+)", token)
        if tm:
            tunits = gs.player_1_units if tm.group(1) == "1" else gs.player_2_units
            ui = int(tm.group(2))
            uname = tunits[ui].card if 0 <= ui < len(tunits) else f"unit #{ui}"
            # Per-code phrasing; fall back to a neutral "choose" for codes
            # this labeler doesn't know yet.
            if choice is not None and choice.code == "MAY_GIVE_UNIT_HERE_+1M":
                return f"{src}: +1 Might → {uname}"
            return f"{src}: choose {uname}"
        return f"{src}: {token}"

    return action
