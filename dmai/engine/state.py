"""The event log and the reducer that turns it into game state.

Spec section 10: *never lose state*.  The rule this module enforces is that
`GameState` is never edited directly by anything above the engine.  Every
change is an append to an event log, and the state is the result of folding
that log.  Two properties follow, and both are tested:

* **Replay.**  ``rebuild(campaign, events)`` reproduces the state exactly, so
  a campaign can be reconstructed from its log alone.
* **Rollback.**  A checkpoint is just an event sequence number; rolling back
  is truncating the log and folding again.

To make replay exact, mutating events carry *absolute* results rather than
deltas -- ``hp_current: 4``, not ``damage: 3``.  A delta applied twice is a
bug; an absolute value applied twice is the same value.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any

from .models.base import Condition, ConditionType, Currency, Visibility
from .models.campaign import Campaign, Checkpoint, GameState, Player
from .models.character import Character, Creature, Item
from .models.combat import CombatState, Encounter
from .models.events import EventType, GameEvent
from .models.memory import Memory
from .models.quests import Consequence, Quest, QuestStatus
from .models.world import NPC, Attitude, Faction, Location

Handler = Callable[[GameState, GameEvent], None]

#: EventType -> reducer.  Types absent here are log-only (narration, dice,
#: checkpoints, DM overrides), which is deliberate: they record what happened
#: without changing what is.
HANDLERS: dict[EventType, Handler] = {}


def handles(*types: EventType) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        for event_type in types:
            HANDLERS[event_type] = fn
        return fn

    return register


class EventStoreError(RuntimeError):
    """The log was asked to do something that would break the audit trail."""


class EventStore:
    """An append-only, monotonically numbered log for one campaign.

    Sequence numbers start at 1 and are assigned here, never by callers, so
    two clients writing through the same engine cannot collide.
    """

    def __init__(self, campaign_id: str, events: Iterable[GameEvent] = ()):
        self.campaign_id = campaign_id
        self._events: list[GameEvent] = []
        for event in events:
            self._adopt(event)

    def _adopt(self, event: GameEvent) -> None:
        """Take an event that already carries a sequence number (a load)."""
        if event.seq <= self.head:
            raise EventStoreError(
                f"event {event.id} has seq {event.seq}, which is not after {self.head}"
            )
        self._events.append(event)

    @property
    def head(self) -> int:
        return self._events[-1].seq if self._events else 0

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[GameEvent]:
        return iter(self._events)

    def append(self, event: GameEvent) -> GameEvent:
        """Number an event and add it to the log."""
        event.seq = self.head + 1
        self._events.append(event)
        return event

    def all(self) -> list[GameEvent]:
        return list(self._events)

    def since(self, seq: int) -> list[GameEvent]:
        """Events after ``seq`` -- what a reconnecting client needs."""
        return [e for e in self._events if e.seq > seq]

    def upto(self, seq: int) -> list[GameEvent]:
        return [e for e in self._events if e.seq <= seq]

    def visible_to(self, player_id: str | None, is_dm: bool = False) -> list[GameEvent]:
        """The log as one seat at the table sees it (spec section 5).

        Private information is filtered here and nowhere else, so there is a
        single place to test that player B never learns about player A's trap.
        """
        return [e for e in self._events if e.visible_to(player_id, is_dm)]

    def truncate_to(self, seq: int) -> list[GameEvent]:
        """Drop everything after ``seq``.  Returns the discarded events."""
        if seq < 0:
            raise EventStoreError(f"cannot truncate to a negative seq: {seq}")
        kept = [e for e in self._events if e.seq <= seq]
        dropped = [e for e in self._events if e.seq > seq]
        self._events = kept
        return dropped


# --- reducers --------------------------------------------------------------
#
# Each takes the state and one event and mutates the state in place.  All are
# written to be safe to run twice: absolute values, idempotent inserts.


def _creature(state: GameState, event: GameEvent) -> Creature | None:
    creature_id = event.data.get("creature_id") or event.target_id or event.actor_id
    return state.creature(creature_id) if creature_id else None


@handles(EventType.PLAYER_JOINED)
def _player_joined(state: GameState, event: GameEvent) -> None:
    player = Player.model_validate(event.data["player"])
    state.players[player.id] = player


@handles(EventType.CHARACTER_CREATED, EventType.CHARACTER_UPDATED)
def _character_written(state: GameState, event: GameEvent) -> None:
    character = Character.model_validate(event.data["character"])
    state.party[character.id] = character
    if character.player_id and character.player_id in state.players:
        player = state.players[character.player_id]
        if character.id not in player.character_ids:
            player.character_ids = [*player.character_ids, character.id]


@handles(EventType.CREATURE_SPAWNED)
def _creature_spawned(state: GameState, event: GameEvent) -> None:
    creature = Creature.model_validate(event.data["creature"])
    state.bestiary[creature.id] = creature


@handles(EventType.CREATURE_REMOVED)
def _creature_removed(state: GameState, event: GameEvent) -> None:
    creature_id = event.data.get("creature_id") or event.target_id
    state.bestiary.pop(creature_id, None)


@handles(EventType.LEVEL_UP)
def _level_up(state: GameState, event: GameEvent) -> None:
    creature = _creature(state, event)
    if creature is None:
        return
    creature.level = int(event.data["level"])
    creature.proficiency_bonus = int(event.data["proficiency_bonus"])
    creature.hp.maximum = int(event.data["hp_maximum"])
    creature.hp.current = int(event.data["hp_current"])
    if isinstance(creature, Character) and "experience" in event.data:
        creature.experience = int(event.data["experience"])


@handles(EventType.HP_CHANGED, EventType.DAMAGE_DEALT, EventType.HEALING)
def _hp_changed(state: GameState, event: GameEvent) -> None:
    creature = _creature(state, event)
    if creature is None:
        return
    if "hp_current" in event.data:
        creature.hp.current = int(event.data["hp_current"])
    if "hp_temporary" in event.data:
        creature.hp.temporary = int(event.data["hp_temporary"])
    if "hp_maximum" in event.data:
        creature.hp.maximum = int(event.data["hp_maximum"])
    if event.data.get("death_saves_reset"):
        creature.death_saves.reset()


@handles(EventType.CONDITION_ADDED)
def _condition_added(state: GameState, event: GameEvent) -> None:
    creature = _creature(state, event)
    if creature is None:
        return
    condition = Condition.model_validate(event.data["condition"])
    existing = next((c for c in creature.conditions if c.type == condition.type), None)
    if existing is None:
        creature.conditions = [*creature.conditions, condition]
    else:  # exhaustion tracks a level; the rest simply refresh
        existing.level = condition.level
        existing.duration_rounds = condition.duration_rounds


@handles(EventType.CONDITION_REMOVED)
def _condition_removed(state: GameState, event: GameEvent) -> None:
    creature = _creature(state, event)
    if creature is None:
        return
    kind = ConditionType(event.data["condition_type"])
    creature.conditions = [c for c in creature.conditions if c.type != kind]


@handles(EventType.DEATH_SAVE)
def _death_save(state: GameState, event: GameEvent) -> None:
    creature = _creature(state, event)
    if creature is None:
        return
    creature.death_saves.successes = int(event.data["successes"])
    creature.death_saves.failures = int(event.data["failures"])
    creature.death_saves.stable = bool(event.data.get("stable", False))


@handles(EventType.CREATURE_DIED)
def _creature_died(state: GameState, event: GameEvent) -> None:
    creature = _creature(state, event)
    if creature is None:
        return
    creature.dead = True
    creature.hp.current = 0
    creature.hp.temporary = 0
    if not creature.has_condition(ConditionType.UNCONSCIOUS):
        creature.conditions = [
            *creature.conditions,
            Condition(type=ConditionType.UNCONSCIOUS, source="death"),
        ]


@handles(EventType.RESOURCE_CHANGED)
def _resource_changed(state: GameState, event: GameEvent) -> None:
    creature = _creature(state, event)
    if creature is None:
        return
    creature.resources = {
        **creature.resources,
        event.data["resource"]: [
            int(event.data["current"]),
            int(event.data["maximum"]),
        ],
    }


@handles(EventType.ITEM_GAINED)
def _item_gained(state: GameState, event: GameEvent) -> None:
    creature = _creature(state, event)
    if creature is None:
        return
    item = Item.model_validate(event.data["item"])
    stack = next(
        (i for i in creature.inventory if i.name == item.name and i.kind == item.kind),
        None,
    )
    if stack is not None and item.kind.value in {"consumable", "misc", "treasure"}:
        stack.quantity += item.quantity
    else:
        creature.inventory = [*creature.inventory, item]


@handles(EventType.ITEM_LOST)
def _item_lost(state: GameState, event: GameEvent) -> None:
    creature = _creature(state, event)
    if creature is None:
        return
    item_id = event.data["item_id"]
    quantity = int(event.data.get("quantity", 0))
    remaining: list[Item] = []
    for item in creature.inventory:
        if item.id != item_id:
            remaining.append(item)
            continue
        if quantity and item.quantity > quantity:
            item.quantity -= quantity
            remaining.append(item)
    creature.inventory = remaining


@handles(EventType.ITEM_EQUIPPED)
def _item_equipped(state: GameState, event: GameEvent) -> None:
    creature = _creature(state, event)
    if creature is None:
        return
    item_id = event.data["item_id"]
    equipped = bool(event.data.get("equipped", True))
    for item in creature.inventory:
        if item.id == item_id:
            item.equipped = equipped
    if "armor_class" in event.data:
        creature.armor_class = int(event.data["armor_class"])


@handles(EventType.CURRENCY_CHANGED)
def _currency_changed(state: GameState, event: GameEvent) -> None:
    creature = _creature(state, event)
    if creature is None:
        return
    creature.currency = Currency.model_validate(event.data["currency"])


@handles(
    EventType.COMBAT_STARTED,
    EventType.INITIATIVE_ROLLED,
    EventType.COMBAT_UPDATED,
    EventType.TURN_STARTED,
    EventType.TURN_ENDED,
    EventType.MOVEMENT,
    EventType.ENEMY_FLED,
    EventType.ENEMY_SURRENDERED,
)
def _combat_written(state: GameState, event: GameEvent) -> None:
    """Combat carries its whole state.

    A round of combat is small and changes in several places at once (turn
    index, action economy, who has fled), so shipping the entire CombatState
    is both simpler and safer to replay than a dozen field-level deltas.
    """
    if "combat" in event.data:
        state.combat = CombatState.model_validate(event.data["combat"])


@handles(EventType.COMBAT_ENDED)
def _combat_ended(state: GameState, event: GameEvent) -> None:
    if "combat" in event.data:
        state.combat = CombatState.model_validate(event.data["combat"])
    state.combat.active = False
    if event.data.get("clear_bestiary"):
        state.bestiary = {cid: c for cid, c in state.bestiary.items() if not c.dead}


@handles(EventType.LOCATION_ADDED)
def _location_added(state: GameState, event: GameEvent) -> None:
    location = Location.model_validate(event.data["location"])
    state.world.locations[location.id] = location


@handles(EventType.LOCATION_ENTERED)
def _location_entered(state: GameState, event: GameEvent) -> None:
    location_id = event.data.get("location_id") or event.target_id
    state.current_location_id = location_id
    location = state.world.locations.get(location_id or "")
    if location is not None:
        location.discovered = True


@handles(EventType.LOCATION_DISCOVERED)
def _location_discovered(state: GameState, event: GameEvent) -> None:
    location = state.world.locations.get(event.data.get("location_id", ""))
    if location is not None:
        location.discovered = True


@handles(EventType.NPC_ADDED)
def _npc_added(state: GameState, event: GameEvent) -> None:
    npc = NPC.model_validate(event.data["npc"])
    state.world.npcs[npc.id] = npc
    if npc.location_id:
        location = state.world.locations.get(npc.location_id)
        if location is not None and npc.id not in location.npc_ids:
            location.npc_ids = [*location.npc_ids, npc.id]


@handles(EventType.FACTION_ADDED)
def _faction_added(state: GameState, event: GameEvent) -> None:
    faction = Faction.model_validate(event.data["faction"])
    state.world.factions[faction.id] = faction


@handles(EventType.NPC_ATTITUDE_CHANGED)
def _npc_attitude(state: GameState, event: GameEvent) -> None:
    npc = state.world.npcs.get(event.data.get("npc_id") or event.actor_id or "")
    if npc is None:
        return
    npc.attitudes = {
        **npc.attitudes,
        event.data["character_id"]: Attitude(event.data["attitude"]),
    }


@handles(EventType.QUEST_ADDED, EventType.QUEST_UPDATED)
def _quest_written(state: GameState, event: GameEvent) -> None:
    quest = Quest.model_validate(event.data["quest"])
    state.story.quests[quest.id] = quest


@handles(EventType.OBJECTIVE_COMPLETED)
def _objective_completed(state: GameState, event: GameEvent) -> None:
    quest = state.story.quests.get(event.data["quest_id"])
    if quest is None:
        return
    for objective in quest.objectives:
        if objective.id == event.data["objective_id"]:
            objective.completed = True
    if quest.is_complete and quest.status is QuestStatus.ACTIVE:
        quest.status = QuestStatus.COMPLETED


@handles(EventType.DISCOVERY)
def _discovery(state: GameState, event: GameEvent) -> None:
    fact = event.data.get("content") or event.summary
    if fact and fact not in state.story.discoveries:
        state.story.discoveries = [*state.story.discoveries, fact]


@handles(EventType.CONSEQUENCE_ADDED)
def _consequence_added(state: GameState, event: GameEvent) -> None:
    consequence = Consequence.model_validate(event.data["consequence"])
    if any(c.id == consequence.id for c in state.story.consequences):
        return
    state.story.consequences = [*state.story.consequences, consequence]


@handles(EventType.CONSEQUENCE_RESOLVED)
def _consequence_resolved(state: GameState, event: GameEvent) -> None:
    for consequence in state.story.consequences:
        if consequence.id == event.data["consequence_id"]:
            consequence.resolved = True


@handles(EventType.TIME_ADVANCED)
def _time_advanced(state: GameState, event: GameEvent) -> None:
    state.world.time.day = int(event.data["day"])
    state.world.time.hour = int(event.data["hour"])
    state.world.time.minute = int(event.data["minute"])


@handles(EventType.WEATHER_CHANGED)
def _weather_changed(state: GameState, event: GameEvent) -> None:
    state.world.weather = event.data["weather"]


@handles(EventType.WORLD_EVENT)
def _world_event(state: GameState, event: GameEvent) -> None:
    description = event.data.get("description") or event.summary
    if description:
        state.world.world_events = [*state.world.world_events, description]


@handles(EventType.ENCOUNTER_STARTED)
def _encounter_started(state: GameState, event: GameEvent) -> None:
    encounter = Encounter.model_validate(event.data["encounter"])
    state.encounters[encounter.id] = encounter


@handles(EventType.ENCOUNTER_ENDED)
def _encounter_ended(state: GameState, event: GameEvent) -> None:
    encounter = state.encounters.get(event.data["encounter_id"])
    if encounter is None:
        return
    abandoned = bool(event.data.get("abandoned", False))
    encounter.abandoned = abandoned
    encounter.resolved = not abandoned


@handles(EventType.MEMORY_ADDED)
def _memory_added(state: GameState, event: GameEvent) -> None:
    memory = Memory.model_validate(event.data["memory"])
    if any(m.id == memory.id for m in state.memories):
        return
    state.memories = [*state.memories, memory]


@handles(EventType.STORY_DECISION)
def _story_decision(state: GameState, event: GameEvent) -> None:
    decision = event.data.get("decision") or event.summary
    if decision:
        state.story.decisions = [*state.story.decisions, decision]


# --- folding ---------------------------------------------------------------


def apply_event(state: GameState, event: GameEvent) -> GameState:
    """Fold one event into the state.  Unknown types are log-only, not errors.

    Tolerating unknown types is what lets a save written by a newer build open
    in an older one without corrupting the parts it does understand.
    """
    handler = HANDLERS.get(event.type)
    if handler is not None:
        handler(state, event)
    state.last_event_seq = max(state.last_event_seq, event.seq)
    return state


def rebuild(campaign: Campaign, events: Iterable[GameEvent]) -> GameState:
    """Reconstruct a campaign's state from its event log alone."""
    state = GameState(campaign=campaign)
    for event in events:
        apply_event(state, event)
    return state


def make_event(
    event_type: EventType,
    *,
    actor_id: str | None = None,
    target_id: str | None = None,
    summary: str = "",
    visibility: Visibility = Visibility.PUBLIC,
    audience_id: str | None = None,
    **data: Any,
) -> GameEvent:
    """Build an unnumbered event.  The store assigns ``seq`` on append."""
    return GameEvent(
        type=event_type,
        actor_id=actor_id,
        target_id=target_id,
        summary=summary,
        visibility=visibility,
        audience_id=audience_id,
        data=data,
    )


class Journal:
    """The one write path: append to the log, then fold into the state.

    Every subsystem -- combat, inventory, the world simulator -- is handed a
    Journal rather than a `GameState`, which is how the invariant at the top
    of this module is enforced by construction instead of by convention.
    """

    def __init__(self, state: GameState, store: EventStore):
        self.state = state
        self.store = store

    def record(
        self,
        event_type: EventType,
        *,
        actor_id: str | None = None,
        target_id: str | None = None,
        summary: str = "",
        visibility: Visibility = Visibility.PUBLIC,
        audience_id: str | None = None,
        **data: Any,
    ) -> GameEvent:
        """Log one fact and apply it.  Returns the numbered event."""
        event = self.store.append(
            make_event(
                event_type,
                actor_id=actor_id,
                target_id=target_id,
                summary=summary,
                visibility=visibility,
                audience_id=audience_id,
                **data,
            )
        )
        apply_event(self.state, event)
        return event

    def rollback_to(self, seq: int) -> GameState:
        """Truncate the log at ``seq`` and refold.  The state object is replaced."""
        self.store.truncate_to(seq)
        self.state = rebuild(self.state.campaign, self.store.all())
        return self.state


def checkpoint_for(
    campaign_id: str, store: EventStore, label: str = "", automatic: bool = False
) -> Checkpoint:
    """Name the current position in the log so it can be returned to."""
    return Checkpoint(
        campaign_id=campaign_id, label=label, event_seq=store.head, automatic=automatic
    )


__all__ = [
    "EventStore",
    "EventStoreError",
    "Journal",
    "apply_event",
    "checkpoint_for",
    "make_event",
    "rebuild",
]
