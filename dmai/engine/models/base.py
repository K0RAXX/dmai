"""Shared primitives every layer of DungeonMaster AI speaks.

Nothing in `dmai.engine` may import from `dmai.ai`, `dmai.api`,
`dmai.persistence` or `dmai.web`.  See tests/test_architecture.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


def new_id(prefix: str) -> str:
    """Readable, sortable-enough identifier, e.g. ``goblin-4f3a91``."""
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Model(BaseModel):
    """Base for every domain model: strict, but forgiving of extra rules data."""

    model_config = ConfigDict(
        validate_assignment=True,
        use_enum_values=False,
        populate_by_name=True,
    )


class Ability(str, Enum):
    STR = "strength"
    DEX = "dexterity"
    CON = "constitution"
    INT = "intelligence"
    WIS = "wisdom"
    CHA = "charisma"


#: Canonical SRD skill -> governing ability.  Loaded rules packs may extend this.
SKILL_ABILITIES: dict[str, Ability] = {
    "acrobatics": Ability.DEX,
    "animal_handling": Ability.WIS,
    "arcana": Ability.INT,
    "athletics": Ability.STR,
    "deception": Ability.CHA,
    "history": Ability.INT,
    "insight": Ability.WIS,
    "intimidation": Ability.CHA,
    "investigation": Ability.INT,
    "medicine": Ability.WIS,
    "nature": Ability.INT,
    "perception": Ability.WIS,
    "performance": Ability.CHA,
    "persuasion": Ability.CHA,
    "religion": Ability.WIS,
    "sleight_of_hand": Ability.DEX,
    "stealth": Ability.DEX,
    "survival": Ability.WIS,
}


class DamageType(str, Enum):
    ACID = "acid"
    BLUDGEONING = "bludgeoning"
    COLD = "cold"
    FIRE = "fire"
    FORCE = "force"
    LIGHTNING = "lightning"
    NECROTIC = "necrotic"
    PIERCING = "piercing"
    POISON = "poison"
    PSYCHIC = "psychic"
    RADIANT = "radiant"
    SLASHING = "slashing"
    THUNDER = "thunder"


class ConditionType(str, Enum):
    BLINDED = "blinded"
    CHARMED = "charmed"
    DEAFENED = "deafened"
    FRIGHTENED = "frightened"
    GRAPPLED = "grappled"
    INCAPACITATED = "incapacitated"
    INVISIBLE = "invisible"
    PARALYZED = "paralyzed"
    PETRIFIED = "petrified"
    POISONED = "poisoned"
    PRONE = "prone"
    RESTRAINED = "restrained"
    STUNNED = "stunned"
    UNCONSCIOUS = "unconscious"
    EXHAUSTION = "exhaustion"


class Condition(Model):
    """An active condition on a creature."""

    type: ConditionType
    source: str | None = None
    #: Rounds remaining; ``None`` means "until removed".
    duration_rounds: int | None = None
    level: int = 1  # exhaustion levels; 1 for binary conditions
    notes: str | None = None


class Currency(Model):
    copper: int = 0
    silver: int = 0
    electrum: int = 0
    gold: int = 0
    platinum: int = 0

    @property
    def total_in_copper(self) -> int:
        return (
            self.copper
            + self.silver * 10
            + self.electrum * 50
            + self.gold * 100
            + self.platinum * 1000
        )


class Difficulty(str, Enum):
    EASY = "easy"
    MODERATE = "moderate"
    HARD = "hard"
    DEADLY = "deadly"
    CUSTOM = "custom"


class Visibility(str, Enum):
    """Who may see a piece of information.  Enforced in the multiplayer layer."""

    PUBLIC = "public"       # everyone at the table
    PRIVATE = "private"     # a single player, whispered by the DM
    DM_ONLY = "dm_only"     # never shown to players


class Timestamped(Model):
    created_at: datetime = Field(default_factory=utcnow)
