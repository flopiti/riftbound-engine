"""The trigger/event layer.

A :class:`GameEvent` is a small, immutable record of *something that just
happened* in the game (a unit entered play, a unit died, a battlefield was
conquered, a turn began, …). It carries no behaviour — it only describes the
fact. The engine emits events at every state transition that the card
taxonomy cares about (see ``GameEngine._emit``).

Cards declare *triggered abilities* via the taxonomy authoring wizard: a
``trigger`` code (e.g. ``WHEN_YOU_PLAY_ME``) paired with the ``activeEffects``
it produces. When an event fires, :func:`trigger_matches` decides whether a
given card's trigger code responds to it, given the relationship between the
card (the *owner*) and the event. Matching abilities become
:class:`TriggeredEffect` items that the engine pushes onto the chain; the
chain resolves them LIFO and runs their effects (see ``effects.py``).

This module is deliberately engine-free (only plain strings and dataclasses)
so the mapping and matching logic can be unit-tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Event kinds — the canonical vocabulary the engine emits and triggers match
# against. Kept as plain string constants (not an enum) so they interoperate
# cleanly with the taxonomy's string codes and serialize trivially.
# --------------------------------------------------------------------------- #
ON_PLAY_UNIT = "ON_PLAY_UNIT"
ON_PLAY_SPELL = "ON_PLAY_SPELL"
ON_DEATH = "ON_DEATH"
ON_CONQUER = "ON_CONQUER"
TURN_START = "TURN_START"
TURN_END = "TURN_END"
ON_CHANNEL = "ON_CHANNEL"
ON_DRAW = "ON_DRAW"

EVENT_KINDS = frozenset(
    {
        ON_PLAY_UNIT,
        ON_PLAY_SPELL,
        ON_DEATH,
        ON_CONQUER,
        TURN_START,
        TURN_END,
        ON_CHANNEL,
        ON_DRAW,
    }
)

# --------------------------------------------------------------------------- #
# Scopes — how the ability's OWNER must relate to the event for it to fire.
# --------------------------------------------------------------------------- #
SELF = "SELF"  # the owner card IS the event's source
FRIENDLY = "FRIENDLY"  # owner controlled by the player who caused the event
ENEMY = "ENEMY"  # owner controlled by the OTHER player
ANY = "ANY"  # fires regardless of who/where
HERE = "HERE"  # owner sits at the event's battlefield


@dataclass(frozen=True)
class TriggerSpec:
    """How one taxonomy trigger code maps onto an emitted event."""

    kind: str
    scope: str


#: Maps a wizard trigger code → the event it listens for + the scope it needs.
#: Codes absent from this table are inert (no emit site yet / continuous
#: abilities like ``WHILE_YOU_CONTROL_THIS_BF`` and ``VISION`` that are not
#: event-driven). New emit sites grow this table; nothing else changes.
TRIGGER_EVENT_MAP: dict[str, TriggerSpec] = {
    # --- a unit enters play -------------------------------------------------
    "WHEN_YOU_PLAY_ME": TriggerSpec(ON_PLAY_UNIT, SELF),
    "WHEN_YOU_PLAY_THIS": TriggerSpec(ON_PLAY_UNIT, SELF),
    "WHEN_YOU_PLAY_ME_FROM_FACEDOWN_ON_YOUR_TURN": TriggerSpec(ON_PLAY_UNIT, SELF),
    "WHEN_YOU_PLAY_UNIT": TriggerSpec(ON_PLAY_UNIT, FRIENDLY),
    "WHEN_PLAYER_PLAYS_UNIT_HERE": TriggerSpec(ON_PLAY_UNIT, HERE),
    "THE_FIRST_TIME_PLAYER_PLAYS_UNIT_HERE_EACH_TURN": TriggerSpec(ON_PLAY_UNIT, HERE),
    "WHEN_OPPONENT_PLAYS_UNIT": TriggerSpec(ON_PLAY_UNIT, ENEMY),
    # --- a spell is cast ----------------------------------------------------
    "WHEN_YOU_PLAY_SPELL": TriggerSpec(ON_PLAY_SPELL, FRIENDLY),
    "WHEN_YOU_PLAY_ME_SPELL": TriggerSpec(ON_PLAY_SPELL, SELF),
    "WHEN_PLAYER_PLAYS_SPELL": TriggerSpec(ON_PLAY_SPELL, ANY),
    "WHEN_YOU_PLAY_SPELL_OPP_TURN": TriggerSpec(ON_PLAY_SPELL, FRIENDLY),
    "WHILE_YOU_CONTROL_THIS_BF_WHEN_PLAY_SPELL": TriggerSpec(ON_PLAY_SPELL, FRIENDLY),
    "FIRST_TIME_PLAYER_CHOOSE_FRIENDLY_WITH_SPELL_EACH_TURN": TriggerSpec(ON_PLAY_SPELL, FRIENDLY),
    # --- a unit dies --------------------------------------------------------
    "DEATHKNELL": TriggerSpec(ON_DEATH, SELF),
    "WHEN_FRIENDLY_UNIT_DIES": TriggerSpec(ON_DEATH, FRIENDLY),
    "IF_UNIT_DIE_COMBAT": TriggerSpec(ON_DEATH, ANY),
    # --- a battlefield is conquered / scored / held -------------------------
    "WHEN_I_CONQUER": TriggerSpec(ON_CONQUER, FRIENDLY),
    "WHEN_CONQUER_HERE": TriggerSpec(ON_CONQUER, HERE),
    "WHEN_SCORE_HERE": TriggerSpec(ON_CONQUER, HERE),
    "WHEN_HOLD_HERE": TriggerSpec(ON_CONQUER, HERE),
    # --- start of a turn ----------------------------------------------------
    "AT_START_BEGGINNING_PHASE": TriggerSpec(TURN_START, FRIENDLY),
    "AT_START_EACH_FIRST_BEGGINNING_PHASE": TriggerSpec(TURN_START, FRIENDLY),
}


@dataclass(frozen=True)
class GameEvent:
    """An immutable record of a state transition that just occurred.

    ``controller`` is the player who *caused* (or benefits from) the event —
    e.g. the player who played the unit, conquered the battlefield, or whose
    turn just began. ``source`` is the card ref responsible (``"player_1:0"``)
    when there is one. ``battlefield`` is set for location-scoped events.
    """

    kind: str
    controller: str | None = None
    source: str | None = None
    battlefield: str | None = None
    data: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class TriggeredEffect:
    """A matched triggered ability, ready to go on the chain.

    Bundles every ``activeEffect`` of the ability so the whole ability
    resolves as one chain item (Riftbound resolves a triggered ability as a
    single unit). ``controller`` is the player who controls the ability.
    """

    controller: str
    source: str | None
    trigger: str
    event_kind: str
    effects: tuple[str, ...]
    conditions: tuple[str, ...] = ()
    label: str = ""
    #: Card NAME the triggering event was about, when there is one — e.g. the
    #: spell that just resolved for an ON_PLAY_SPELL trigger. Lets the UI
    #: underline "spell" in the trigger label and hover-preview that card.
    context_card: str | None = None


@dataclass
class EventLogEntry:
    """One line in the on-screen trigger/effect feed.

    ``kind`` is ``"event"`` (something happened), ``"trigger"`` (an ability
    fired and went on the chain) or ``"effect"`` (an effect resolved).
    ``text`` is a human-readable description for the UI.
    """

    sequence: int
    kind: str
    text: str


def trigger_matches(
    trigger_code: str,
    event: GameEvent,
    *,
    owner_controller: str | None,
    owner_location: str | None = None,
    owner_ref: str | None = None,
) -> bool:
    """Does ``trigger_code`` (on a card owned by ``owner_controller``, sitting
    at ``owner_location``, referenced as ``owner_ref``) respond to ``event``?

    Pure function — no engine state. Unknown / inert codes return ``False``.
    """
    spec = TRIGGER_EVENT_MAP.get(trigger_code)
    if spec is None or spec.kind != event.kind:
        return False
    if spec.scope == ANY:
        return True
    if spec.scope == SELF:
        return owner_ref is not None and owner_ref == event.source
    if spec.scope == FRIENDLY:
        return owner_controller is not None and owner_controller == event.controller
    if spec.scope == ENEMY:
        return (
            owner_controller is not None
            and event.controller is not None
            and owner_controller != event.controller
        )
    if spec.scope == HERE:
        return (
            owner_location is not None
            and event.battlefield is not None
            and owner_location == event.battlefield
        )
    return False
