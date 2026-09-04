"""The append-only event log.

Every meaningful change to a campaign is an event.  The full game state must
be reconstructable by replaying this log from empty (spec section 10), which
is what makes checkpoints, rollback and audit possible.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from .base import Model, Visibility, new_id, utcnow


class EventType(str, Enum):
    # campaign lifecycle
    CAMPAIGN_CREATED = "campaign_created"
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    CHECKPOINT = "checkpoint"
    PLAYER_JOINED = "player_joined"

    # characters
    CHARACTER_CREATED = "character_created"
    CHARACTER_UPDATED = "character_updated"
    LEVEL_UP = "level_up"
    HP_CHANGED = "hp_changed"
    CONDITION_ADDED = "condition_added"
    CONDITION_REMOVED = "condition_removed"
    DEATH_SAVE = "death_save"
    CREATURE_DIED = "creature_died"
    CREATURE_SPAWNED = "creature_spawned"
    CREATURE_REMOVED = "creature_removed"
    RESOURCE_CHANGED = "resource_changed"

    # inventory
    ITEM_GAINED = "item_gained"
    ITEM_LOST = "item_lost"
    ITEM_EQUIPPED = "item_equipped"
    CURRENCY_CHANGED = "currency_changed"

    # dice and checks
    DICE_ROLLED = "dice_rolled"
    CHECK_RESOLVED = "check_resolved"
    SAVE_RESOLVED = "save_resolved"

    # combat
    COMBAT_STARTED = "combat_started"
    INITIATIVE_ROLLED = "initiative_rolled"
    #: The turn order or a combatant's action economy changed.
    COMBAT_UPDATED = "combat_updated"
    TURN_STARTED = "turn_started"
    TURN_ENDED = "turn_ended"
    ATTACK = "attack"
    DAMAGE_DEALT = "damage_dealt"
    HEALING = "healing"
    MOVEMENT = "movement"
    COMBAT_ENDED = "combat_ended"
    ENEMY_FLED = "enemy_fled"
    ENEMY_SURRENDERED = "enemy_surrendered"

    # world and story
    LOCATION_ADDED = "location_added"
    LOCATION_ENTERED = "location_entered"
    LOCATION_DISCOVERED = "location_discovered"
    NPC_ADDED = "npc_added"
    FACTION_ADDED = "faction_added"
    NPC_MET = "npc_met"
    NPC_ATTITUDE_CHANGED = "npc_attitude_changed"
    QUEST_ADDED = "quest_added"
    QUEST_UPDATED = "quest_updated"
    OBJECTIVE_COMPLETED = "objective_completed"
    DISCOVERY = "discovery"
    STORY_DECISION = "story_decision"
    CONSEQUENCE_ADDED = "consequence_added"
    CONSEQUENCE_RESOLVED = "consequence_resolved"
    TIME_ADVANCED = "time_advanced"
    WEATHER_CHANGED = "weather_changed"
    WORLD_EVENT = "world_event"
    ENCOUNTER_STARTED = "encounter_started"
    ENCOUNTER_ENDED = "encounter_ended"

    # narrative
    PLAYER_ACTION = "player_action"
    DM_NARRATION = "dm_narration"
    NPC_DIALOGUE = "npc_dialogue"
    PRIVATE_MESSAGE = "private_message"
    MEMORY_ADDED = "memory_added"

    # human DM overrides (spec section 17)
    DM_OVERRIDE = "dm_override"


class GameEvent(Model):
    """One immutable fact about what happened."""

    id: str = Field(default_factory=lambda: new_id("evt"))
    #: Monotonic position in the campaign log; assigned by the event store.
    seq: int = 0
    type: EventType
    timestamp: datetime = Field(default_factory=utcnow)

    actor_id: str | None = None
    target_id: str | None = None
    #: Type-specific data.  Kept loose on purpose so new event types do not
    #: require a schema migration; readers must tolerate missing keys.
    data: dict = Field(default_factory=dict)

    #: Player-facing summary, if this event should appear in the log.
    summary: str = ""
    visibility: Visibility = Visibility.PUBLIC
    #: When visibility is PRIVATE, which player may see it.
    audience_id: str | None = None

    def visible_to(self, player_id: str | None, is_dm: bool = False) -> bool:
        if is_dm:
            return True
        if self.visibility is Visibility.PUBLIC:
            return True
        if self.visibility is Visibility.DM_ONLY:
            return False
        return player_id is not None and self.audience_id == player_id
