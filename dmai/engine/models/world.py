"""Locations, NPCs, factions, and the passage of time."""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .base import Model, Visibility, new_id
from .character import Creature, CreatureKind, Personality


class Attitude(str, Enum):
    HOSTILE = "hostile"
    UNFRIENDLY = "unfriendly"
    INDIFFERENT = "indifferent"
    FRIENDLY = "friendly"
    ALLIED = "allied"


class Secret(Model):
    """Something known to some parties and not others."""

    id: str = Field(default_factory=lambda: new_id("secret"))
    content: str
    visibility: Visibility = Visibility.DM_ONLY
    #: Character/NPC ids that currently know this.
    known_by: list[str] = Field(default_factory=list)


class NPC(Creature):
    """An NPC is a creature that also has an inner life.

    Extending Creature means an NPC can step straight into initiative order
    without conversion (spec section 8).
    """

    kind: CreatureKind = CreatureKind.NPC
    role: str = ""
    personality: Personality = Field(default_factory=Personality)
    fears: list[str] = Field(default_factory=list)
    #: Facts this NPC knows and may reveal in conversation.
    knowledge: list[str] = Field(default_factory=list)
    secrets: list[Secret] = Field(default_factory=list)
    #: character_id -> Attitude
    attitudes: dict[str, Attitude] = Field(default_factory=dict)
    #: npc_id/faction_id -> description of the relationship
    relationships: dict[str, str] = Field(default_factory=dict)
    emotional_state: str = "calm"
    dialogue_style: str = ""
    location_id: str | None = None
    faction_id: str | None = None

    def attitude_toward(self, character_id: str) -> Attitude:
        return self.attitudes.get(character_id, Attitude.INDIFFERENT)


class Location(Model):
    id: str = Field(default_factory=lambda: new_id("loc"))
    name: str
    kind: str = "place"  # settlement, dungeon, wilderness, building, room...
    description: str = ""
    parent_id: str | None = None
    connections: list[str] = Field(default_factory=list)
    npc_ids: list[str] = Field(default_factory=list)
    #: Things a character can find here, gated behind checks.
    features: list[str] = Field(default_factory=list)
    secrets: list[Secret] = Field(default_factory=list)
    discovered: bool = False
    notes: str = ""


class Faction(Model):
    id: str = Field(default_factory=lambda: new_id("faction"))
    name: str
    description: str = ""
    goals: list[str] = Field(default_factory=list)
    #: Long-running plans advanced by the sandbox simulator (spec section 15).
    agenda: list[str] = Field(default_factory=list)
    #: faction_id -> standing from -100 (war) to +100 (allied)
    relations: dict[str, int] = Field(default_factory=dict)
    #: Party standing with this faction.
    party_standing: int = 0
    power: int = 50
    headquarters_id: str | None = None
    member_ids: list[str] = Field(default_factory=list)


class GameTime(Model):
    """Coarse in-world clock.  Fine enough for travel and rests."""

    day: int = 1
    hour: int = 8
    minute: int = 0

    def advance(self, minutes: int) -> None:
        total = self.minute + minutes
        self.minute = total % 60
        hours = self.hour + total // 60
        self.hour = hours % 24
        self.day += hours // 24

    @property
    def time_of_day(self) -> str:
        if 5 <= self.hour < 12:
            return "morning"
        if 12 <= self.hour < 17:
            return "afternoon"
        if 17 <= self.hour < 21:
            return "evening"
        return "night"

    def __str__(self) -> str:
        return f"Day {self.day}, {self.hour:02d}:{self.minute:02d} ({self.time_of_day})"


class WorldState(Model):
    """Everything that exists independently of the party."""

    locations: dict[str, Location] = Field(default_factory=dict)
    npcs: dict[str, NPC] = Field(default_factory=dict)
    factions: dict[str, Faction] = Field(default_factory=dict)
    time: GameTime = Field(default_factory=GameTime)
    weather: str = "clear"
    #: World events that happened whether or not the party was present.
    world_events: list[str] = Field(default_factory=list)
