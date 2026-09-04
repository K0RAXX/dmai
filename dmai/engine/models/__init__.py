"""Domain schemas shared by every layer."""

from .actions import (
    ActionInterpretation,
    ActionResult,
    CheckRequest,
    IntentKind,
    PlayerAction,
)
from .base import (
    SKILL_ABILITIES,
    Ability,
    Condition,
    ConditionType,
    Currency,
    DamageType,
    Difficulty,
    Model,
    Visibility,
    new_id,
    utcnow,
)
from .campaign import (
    Campaign,
    CampaignSettings,
    Checkpoint,
    DMBehavior,
    GameState,
    NarrativeStyle,
    Player,
    Session,
    StoryTone,
)
from .character import (
    Abilities,
    Character,
    Creature,
    CreatureKind,
    DeathSaves,
    HitPoints,
    Item,
    ItemKind,
    Personality,
)
from .combat import Combatant, CombatState, Encounter, EncounterKind
from .dice import DiceRequest, DiceResult, DieRoll, Outcome, RollMode
from .events import EventType, GameEvent
from .memory import Memory, MemoryKind, MemoryTier
from .quests import CampaignState, Consequence, Objective, Quest, QuestStatus
from .world import (
    Attitude,
    Faction,
    GameTime,
    Location,
    NPC,
    Secret,
    WorldState,
)

__all__ = [
    # base
    "Model", "Ability", "SKILL_ABILITIES", "DamageType", "ConditionType",
    "Condition", "Currency", "Difficulty", "Visibility", "new_id", "utcnow",
    # dice
    "DiceRequest", "DiceResult", "DieRoll", "RollMode", "Outcome",
    # creatures
    "Abilities", "HitPoints", "DeathSaves", "Item", "ItemKind", "Personality",
    "Creature", "CreatureKind", "Character",
    # world
    "Location", "NPC", "Faction", "Secret", "Attitude", "GameTime", "WorldState",
    # story
    "Quest", "QuestStatus", "Objective", "Consequence", "CampaignState",
    # combat
    "Combatant", "CombatState", "Encounter", "EncounterKind",
    # events
    "GameEvent", "EventType",
    # memory
    "Memory", "MemoryTier", "MemoryKind",
    # actions
    "PlayerAction", "ActionInterpretation", "ActionResult", "CheckRequest",
    "IntentKind",
    # campaign
    "Campaign", "CampaignSettings", "GameState", "Player", "Session",
    "Checkpoint", "StoryTone", "NarrativeStyle", "DMBehavior",
]
