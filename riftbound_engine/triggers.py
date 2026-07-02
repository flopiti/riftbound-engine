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
#: A GEAR is played (committed to base from hand). Emitted for the player who
#: played it (``controller``); ``source`` is the gear's ref.
ON_PLAY_GEAR = "ON_PLAY_GEAR"
#: A card is DISCARDED (moved from a hand to that player's trash). Emitted for
#: the player who discarded it (``controller``) — including a forced discard the
#: opponent caused (it's still "you discard"); ``data["card"]`` is the card.
#: Fires once per card discarded.
ON_DISCARD = "ON_DISCARD"
ON_DEATH = "ON_DEATH"
#: A battlefield is CONQUERED — control of it changes hands (showdown win or an
#: uncontested move-in). Distinct from holding it across turns.
ON_CONQUER = "ON_CONQUER"
#: A battlefield is HELD — the active player still controls it at their
#: beginning phase (it did NOT change hands; they kept it since last turn).
ON_HOLD = "ON_HOLD"
#: A point is SCORED at a battlefield — fires for ANY point, whether it came
#: from conquering or holding. ("Score here" cares only that a point landed.)
ON_SCORE = "ON_SCORE"
#: A showdown OPENS against a battlefield you control — you are the DEFENDER.
#: Emitted for the defender (the prior controller) the moment the attacker
#: moves in, before the muster/focus window.
ON_DEFEND = "ON_DEFEND"
#: A showdown OPENS at a battlefield — fires for ANY card sitting HERE, on
#: either side, regardless of who controlled the battlefield (or no one).
#: Distinct from ON_DEFEND, which fires only for the prior controller and not
#: at all on an uncontrolled battlefield. Emitted once, when the showdown is
#: born (not on muster/reinforce moves).
ON_SHOWDOWN_BEGIN = "ON_SHOWDOWN_BEGIN"
TURN_START = "TURN_START"
TURN_END = "TURN_END"
ON_CHANNEL = "ON_CHANNEL"
ON_DRAW = "ON_DRAW"
#: A unit MOVED (base ↔ battlefield). Emitted for the moving unit (SELF scope);
#: ``battlefield`` carries the destination when it is a battlefield (None for a
#: move back to base). ``data["origin"]`` carries where it moved FROM (a
#: battlefield slot or "base"), so "when a unit moves from here" can match.
ON_MOVE = "ON_MOVE"
#: A player MOVED an ENEMY unit (e.g. Charm). Emitted for the player who did the
#: moving (``controller``); ``data["unit"]`` is the moved enemy unit's ref.
ON_MOVE_ENEMY = "ON_MOVE_ENEMY"
#: A unit was RETURNED to its owner's hand (Gust, etc.). Emitted as the unit
#: leaves play; ``controller`` is the unit's owner, ``data["origin"]`` the
#: battlefield/base it sat at (so "a unit here is returned" can match).
ON_RETURN_TO_HAND = "ON_RETURN_TO_HAND"
#: A unit was CHOSEN (targeted) by a spell or ability. Emitted for the unit
#: (SELF scope via ``source``); ``controller`` is the player who chose it.
ON_CHOOSE = "ON_CHOOSE"
#: A unit was READIED (un-exhausted) — including the Awake step refresh and any
#: effect that readies it. Emitted per readied unit (SELF scope via ``source``);
#: ``controller`` is its controller.
ON_READY = "ON_READY"
#: An ENEMY unit was STUNNED. Emitted for the player who stunned it
#: (``controller`` = the stunner); ``source`` / ``data["unit"]`` is the stunned
#: unit's ref and ``battlefield`` its location (so "at a battlefield" / move-to
#: abilities can read where). Only emitted for enemy targets — stunning your own
#: unit (Facebreaker) does not fire "when you stun an enemy unit".
ON_STUN = "ON_STUN"

EVENT_KINDS = frozenset(
    {
        ON_PLAY_UNIT,
        ON_PLAY_SPELL,
        ON_PLAY_GEAR,
        ON_DISCARD,
        ON_DEATH,
        ON_CONQUER,
        ON_HOLD,
        ON_SCORE,
        ON_DEFEND,
        ON_SHOWDOWN_BEGIN,
        TURN_START,
        TURN_END,
        ON_CHANNEL,
        ON_DRAW,
        ON_MOVE,
        ON_MOVE_ENEMY,
        ON_RETURN_TO_HAND,
        ON_CHOOSE,
        ON_READY,
        ON_STUN,
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
ORIGIN_HERE = "ORIGIN_HERE"  # owner sits where the event ORIGINATED (data["origin"])


@dataclass(frozen=True)
class TriggerSpec:
    """How one taxonomy trigger code maps onto an emitted event.

    ``kind`` is the event kind, or a tuple of kinds when one trigger responds to
    several (e.g. "when you choose OR ready me")."""

    kind: "str | tuple[str, ...]"
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
    # The Dreaming Tree: "when a player chooses a friendly unit HERE with a
    # spell for the FIRST time each turn, they draw 1." Fires on any spell
    # (ANY); GameEngine narrows it to a caster who chose one of their units at
    # THIS battlefield, capped once per player per turn (._trigger_state_ok).
    "FIRST_TIME_PLAYER_CHOOSE_FRIENDLY_WITH_SPELL_EACH_TURN": TriggerSpec(ON_PLAY_SPELL, ANY),
    # --- a gear is played ---------------------------------------------------
    # "When you play a gear, …" (Pit Crew: "ready me"). FRIENDLY: fires for the
    # player who played the gear.
    "WHEN_YOU_PLAY_GEAR": TriggerSpec(ON_PLAY_GEAR, FRIENDLY),
    # --- you discard a card -------------------------------------------------
    # "When you discard one or more cards, …" (Jinx, Rebel: "ready me and give
    # me +1 might"). FRIENDLY: fires for the player who discarded.
    "WHEN_YOU_DISCARD": TriggerSpec(ON_DISCARD, FRIENDLY),
    # --- a unit dies --------------------------------------------------------
    "DEATHKNELL": TriggerSpec(ON_DEATH, SELF),
    "WHEN_FRIENDLY_UNIT_DIES": TriggerSpec(ON_DEATH, FRIENDLY),
    "IF_UNIT_DIE_COMBAT": TriggerSpec(ON_DEATH, ANY),
    # --- a battlefield is conquered / scored / held -------------------------
    # These three are now DISTINCT events (previously all mapped to ON_CONQUER,
    # which made conquer/hold triggers fire on each other and forced the planner
    # to exclude them). CONQUER = control changed hands; HOLD = kept it across
    # turns at the beginning phase; SCORE = any point landed (either source).
    "WHEN_I_CONQUER": TriggerSpec(ON_CONQUER, FRIENDLY),
    "WHEN_CONQUER_HERE": TriggerSpec(ON_CONQUER, HERE),
    "WHEN_HOLD_HERE": TriggerSpec(ON_HOLD, HERE),
    "WHEN_SCORE_HERE": TriggerSpec(ON_SCORE, HERE),
    # A showdown opened against a BF you control — fires for the card(s) HERE
    # (the battlefield card / your units there) on the defending side.
    "WHEN_YOU_DEFEND_HERE": TriggerSpec(ON_DEFEND, HERE),
    # A showdown began at this battlefield — fires for the card(s) HERE on
    # EITHER side (attacker's invaders, defender's units, the battlefield
    # card itself), including showdowns over an uncontrolled battlefield.
    "WHEN_SHOWDOWN_BEGINS_HERE": TriggerSpec(ON_SHOWDOWN_BEGIN, HERE),
    # --- start of a turn ----------------------------------------------------
    "AT_START_BEGGINNING_PHASE": TriggerSpec(TURN_START, FRIENDLY),
    # "each player's FIRST Beginning Phase" — fires for ANY player's turn start
    # (Obelisk of Power / The Arena's Greatest benefit "that player"), but only
    # the first one: the engine narrows it to turn_number == 1 for the
    # beneficiary in GameEngine._trigger_state_ok.
    "AT_START_EACH_FIRST_BEGGINNING_PHASE": TriggerSpec(TURN_START, ANY),
    # --- end of a turn ------------------------------------------------------
    # The owner's own turn ending (e.g. Blighted Battleaxe's end-of-turn
    # unattach + self-damage).
    "AT_END_OF_TURN": TriggerSpec(TURN_END, FRIENDLY),
    # --- a unit moves -------------------------------------------------------
    # The moving unit's own "when I move" ability (Stellacorn Herder's "draw 1";
    # Noxian Drummer / Corina's "play a Recruit token here"). SELF scope: only
    # the unit that actually moved fires.
    "WHEN_I_MOVE": TriggerSpec(ON_MOVE, SELF),
    # A unit moved AWAY from this battlefield (Back-Alley Bar: "give it +1
    # might"). ORIGIN_HERE: the battlefield card matches the move's origin.
    "WHEN_UNIT_MOVE_FROM_HERE": TriggerSpec(ON_MOVE, ORIGIN_HERE),
    # You moved an ENEMY unit (Blast Cone, after Charm-style movement). Fires
    # for the player who moved it.
    "WHEN_YOU_MOVE_ENEMY_UNIT": TriggerSpec(ON_MOVE_ENEMY, FRIENDLY),
    # --- a unit returns to hand ---------------------------------------------
    # A unit sitting HERE is bounced to hand (Ripper's Bay). ORIGIN_HERE: the
    # battlefield matches where the unit was when it left.
    "WHEN_UNIT_RETURNED_HAND": TriggerSpec(ON_RETURN_TO_HAND, ORIGIN_HERE),
    # --- this unit is interacted with ---------------------------------------
    # "When I hold" — this unit holds the battlefield it is on (Ahri, Alluring).
    # HERE scope: the unit sits at the battlefield that held a point.
    "WHEN_I_HOLD": TriggerSpec(ON_HOLD, HERE),
    # "When you (or an ally) hold" — a player-level hold (Gloomist legend).
    # FRIENDLY scope: fires when the ability's owner holds, wherever the source
    # sits (legends have no location).
    "WHEN_YOU_HOLD": TriggerSpec(ON_HOLD, FRIENDLY),
    # "When I attack or defend" — this unit is in a showdown at its battlefield
    # (Ahri, Inquisitive). HERE scope: it sits at the contested battlefield.
    "WHEN_I_ATTACK_OR_DEFEND": TriggerSpec(ON_SHOWDOWN_BEGIN, HERE),
    # "When you choose OR ready me" (Irelia, Fervent). Either event, SELF scope.
    "WHEN_YOU_CHOOSE_OR_READY_ME": TriggerSpec((ON_CHOOSE, ON_READY), SELF),
    # "When you choose a friendly unit" (Blade Dancer legend): fires when the
    # owner targets one of THEIR units. FRIENDLY scope catches "owner did it";
    # a state gate further restricts to a friendly CHOSEN unit (engine
    # ._trigger_state_ok), and the chosen unit rides along as the effect target.
    "WHEN_YOU_CHOOSE_FRIENDLY_UNIT": TriggerSpec(ON_CHOOSE, FRIENDLY),
    # --- you stun an enemy unit ---------------------------------------------
    # "When you stun an enemy unit …" (Eclipse Herald, Radiant Dawn, Vex
    # Mocking). FRIENDLY: fires for the player who did the stunning. The event
    # carries the stunned unit's ref + battlefield, so a "move me to that
    # battlefield" ability (Vex) reads it via the fed target.
    "WHEN_YOU_STUN_ENEMY_UNIT": TriggerSpec(ON_STUN, FRIENDLY),
    "WHEN_YOU_STUN_ONE_OR_MORE_ENEMY_UNITS": TriggerSpec(ON_STUN, FRIENDLY),
    # --- end of your turn ---------------------------------------------------
    # Dazzling Aurora: "At the end of your turn, reveal … until a unit … play
    # it …". The reveal/play behaviour rides on the bundled effect code; the
    # trigger just fires at the controller's turn end.
    "AT_END_OF_YOUR_TURN_REVEAL_TOP_UNTIL_UNIT_PLAY_IT": TriggerSpec(TURN_END, FRIENDLY),
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
    #: The ability's COSTS (e.g. ``EXHAUST_THIS``). A payable cost turns the
    #: triggered ability into a "you MAY pay to get the effect" decision when it
    #: resolves; an unpayable/declined cost means the effect doesn't happen.
    costs: tuple[str, ...] = ()
    label: str = ""
    #: Card NAME the triggering event was about, when there is one — e.g. the
    #: spell that just resolved for an ON_PLAY_SPELL trigger. Lets the UI
    #: underline "spell" in the trigger label and hover-preview that card.
    context_card: str | None = None
    #: Unit ref(s) the event was ABOUT, fed to the ability's targeted effects so
    #: "give IT +1 might" / "[Stun] IT" act on the moved/returned/chosen unit
    #: without a separate target pick. Empty for abilities that pick their own.
    targets: tuple[str, ...] = ()


@dataclass
class EventLogEntry:
    """One line in the on-screen trigger/effect feed.

    ``kind`` is ``"event"`` (something happened), ``"trigger"`` (an ability
    fired and went on the chain) or ``"effect"`` (an effect resolved).
    ``text`` is a raw human-ish description kept as a FALLBACK for the UI.

    ``code`` is the raw engine code the line is about (a trigger code, an
    effect code, or an event kind) when there is one; ``card`` is the source
    card NAME when there is one. The UI prefers these — it translates ``code``
    through its label table (``chainLabels.ts::humanizeCode``) so the feed
    reads "Draw 1" / "When you play this" instead of ``DRAW_1`` /
    ``WHEN_YOU_PLAY_ME``. ``text`` is only used when ``code`` is absent.
    """

    sequence: int
    kind: str
    text: str
    code: str | None = None
    card: str | None = None


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
    if spec is None:
        return False
    kinds = spec.kind if isinstance(spec.kind, tuple) else (spec.kind,)
    if event.kind not in kinds:
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
    if spec.scope == ORIGIN_HERE:
        origin = event.data.get("origin")
        return owner_location is not None and origin is not None and owner_location == origin
    return False
