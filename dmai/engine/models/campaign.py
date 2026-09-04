"""Campaign metadata, table settings, and the aggregate game state."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from .base import Difficulty, Model, new_id, utcnow
from .character import Character, Creature
from .combat import CombatState, Encounter
from .events import GameEvent
from .memory import Memory
from .quests import CampaignState
from .world import WorldState


class StoryTone(str, Enum):
    LIGHTHEARTED = "lighthearted"
    HEROIC = "heroic"
    SERIOUS = "serious"
    DARK = "dark"
    GRIM = "grim"
    HORROR = "horror"
    COMEDIC = "comedic"
    POLITICAL = "political"
    MYSTERY = "mystery"
    EPIC = "epic"


class NarrativeStyle(str, Enum):
    CONCISE = "concise"
    CONVERSATIONAL = "conversational"
    DESCRIPTIVE = "descriptive"
    CINEMATIC = "cinematic"
    LITERARY = "literary"


class DMBehavior(str, Enum):
    STRICT = "strict"
    NEUTRAL = "neutral"
    GENEROUS = "generous"
    CHAOTIC = "chaotic"
    RULES_FOCUSED = "rules_focused"
    STORY_FOCUSED = "story_focused"


class CampaignSettings(Model):
    """Table preferences.  Style never overrides game integrity."""

    tone: StoryTone = StoryTone.HEROIC
    style: NarrativeStyle = NarrativeStyle.DESCRIPTIVE
    dm_behavior: DMBehavior = DMBehavior.NEUTRAL
    difficulty: Difficulty = Difficulty.MODERATE
    rules_pack: str = "srd51"
    #: Emergent play: the world advances whether or not the party engages.
    sandbox_mode: bool = False
    #: Deterministic play for tests and reproducible sessions.
    rng_seed: int | None = None
    genre: str = "high fantasy"
    themes: list[str] = Field(default_factory=list)

    #: Which DM AI answers for this table (spec section 18).  An id, never a
    #: credential: keys live in the environment and never in a save file.
    ai_provider: str = "offline"
    #: Empty means "whatever that provider defaults to".
    ai_model: str = ""


class Player(Model):
    """A human at the table.  Distinct from the character they control."""

    id: str = Field(default_factory=lambda: new_id("player"))
    name: str
    #: Chat/channel identity, used by the OpenClaw adapter to route messages.
    external_id: str | None = None
    character_ids: list[str] = Field(default_factory=list)
    is_host: bool = False


class Campaign(Model):
    """Campaign metadata.  The heavy state lives in GameState."""

    id: str = Field(default_factory=lambda: new_id("camp"))
    name: str
    premise: str = ""
    setting: str = ""
    settings: CampaignSettings = Field(default_factory=CampaignSettings)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    #: Schema version, so old saves can be migrated rather than rejected.
    save_version: int = 1


class GameState(Model):
    """The single source of truth for one campaign.

    Mutated only by applying `GameEvent`s, so the whole thing can be rebuilt
    from the log (see dmai.engine.state).
    """

    campaign: Campaign
    players: dict[str, Player] = Field(default_factory=dict)
    #: Player characters, keyed by character id.
    party: dict[str, Character] = Field(default_factory=dict)
    #: Monsters and other transient combatants for the current scene.
    bestiary: dict[str, Creature] = Field(default_factory=dict)

    world: WorldState = Field(default_factory=WorldState)
    story: CampaignState = Field(default_factory=CampaignState)
    combat: CombatState = Field(default_factory=CombatState)
    encounters: dict[str, Encounter] = Field(default_factory=dict)
    memories: list[Memory] = Field(default_factory=list)

    current_location_id: str | None = None
    #: Position in the event log this state reflects.
    last_event_seq: int = 0

    def creature(self, creature_id: str) -> Creature | None:
        """Look a combatant up wherever it lives: party, bestiary, or world."""
        return (
            self.party.get(creature_id)
            or self.bestiary.get(creature_id)
            or self.world.npcs.get(creature_id)
        )

    @property
    def location(self):
        if self.current_location_id is None:
            return None
        return self.world.locations.get(self.current_location_id)


class Session(Model):
    """One sitting at the table.  Produces a recap when it ends."""

    id: str = Field(default_factory=lambda: new_id("sess"))
    campaign_id: str
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    #: Event log range covered by this session.
    first_event_seq: int = 0
    last_event_seq: int = 0
    recap: str = ""


class Checkpoint(Model):
    """A named, restorable point in the event log."""

    id: str = Field(default_factory=lambda: new_id("ckpt"))
    campaign_id: str
    label: str = ""
    event_seq: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    automatic: bool = False


__all__ = [
    "StoryTone",
    "NarrativeStyle",
    "DMBehavior",
    "CampaignSettings",
    "Player",
    "Campaign",
    "GameState",
    "Session",
    "Checkpoint",
    "GameEvent",
]
