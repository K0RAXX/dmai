"""Player characters, NPC stat blocks, and the creature model they share."""

from __future__ import annotations

from enum import Enum

from pydantic import Field, computed_field

from .base import (
    Ability,
    Condition,
    Currency,
    DamageType,
    Model,
    new_id,
)


class Abilities(Model):
    strength: int = 10
    dexterity: int = 10
    constitution: int = 10
    intelligence: int = 10
    wisdom: int = 10
    charisma: int = 10

    def score(self, ability: Ability) -> int:
        return getattr(self, ability.value)

    def modifier(self, ability: Ability) -> int:
        """SRD modifier: floor((score - 10) / 2), correct for scores below 10."""
        return (self.score(ability) - 10) // 2


class HitPoints(Model):
    current: int = 10
    maximum: int = 10
    temporary: int = 0

    @property
    def is_bloodied(self) -> bool:
        return self.current <= self.maximum // 2

    @property
    def is_down(self) -> bool:
        return self.current <= 0


class DeathSaves(Model):
    successes: int = 0
    failures: int = 0
    stable: bool = False

    def reset(self) -> None:
        self.successes = 0
        self.failures = 0
        self.stable = False


class ItemKind(str, Enum):
    WEAPON = "weapon"
    ARMOR = "armor"
    SHIELD = "shield"
    CONSUMABLE = "consumable"
    TOOL = "tool"
    TREASURE = "treasure"
    QUEST = "quest"
    MISC = "misc"


class Item(Model):
    id: str = Field(default_factory=lambda: new_id("item"))
    name: str
    kind: ItemKind = ItemKind.MISC
    quantity: int = 1
    weight: float = 0.0
    value_gp: float = 0.0
    description: str = ""
    equipped: bool = False
    #: Free-form rules payload from the active rules pack
    #: (damage dice, AC, properties, charges...).
    properties: dict = Field(default_factory=dict)


class Personality(Model):
    traits: list[str] = Field(default_factory=list)
    ideals: list[str] = Field(default_factory=list)
    bonds: list[str] = Field(default_factory=list)
    flaws: list[str] = Field(default_factory=list)
    appearance: str = ""
    backstory: str = ""
    alignment: str | None = None


class CreatureKind(str, Enum):
    PLAYER = "player"
    NPC = "npc"
    MONSTER = "monster"


class Creature(Model):
    """Anything with hit points and a turn in initiative.

    Player characters, friendly NPCs and monsters share this shape so the
    combat engine never needs to branch on who is fighting.
    """

    id: str = Field(default_factory=lambda: new_id("creature"))
    name: str
    kind: CreatureKind = CreatureKind.MONSTER
    level: int = 1
    species: str = ""
    character_class: str = Field(default="", alias="class")

    abilities: Abilities = Field(default_factory=Abilities)
    hp: HitPoints = Field(default_factory=HitPoints)
    armor_class: int = 10
    speed: int = 30
    proficiency_bonus: int = 2

    #: Skill name -> proficiency multiplier (0 none, 1 proficient, 2 expertise).
    skill_proficiencies: dict[str, int] = Field(default_factory=dict)
    saving_throw_proficiencies: list[Ability] = Field(default_factory=list)

    conditions: list[Condition] = Field(default_factory=list)
    resistances: list[DamageType] = Field(default_factory=list)
    vulnerabilities: list[DamageType] = Field(default_factory=list)
    immunities: list[DamageType] = Field(default_factory=list)

    inventory: list[Item] = Field(default_factory=list)
    currency: Currency = Field(default_factory=Currency)
    death_saves: DeathSaves = Field(default_factory=DeathSaves)
    #: Set by the CREATURE_DIED event.  Zero hit points alone means *dying*,
    #: which is a state a character can be pulled back out of.
    dead: bool = False

    #: Spell slots, rage uses, ki points -> {"name": [current, maximum]}
    resources: dict[str, list[int]] = Field(default_factory=dict)
    spells: list[str] = Field(default_factory=list)

    #: Stat-block attacks, straight from the rules pack:
    #: {"name": "Bite", "bonus": 4, "damage": "2d4+2", "damage_type": "piercing"}.
    #: Characters usually attack with equipped weapons and leave this empty.
    attacks: list[dict] = Field(default_factory=list)

    #: Monster tactics, read by the combat behaviour system (spec section 11).
    morale: int = 50          # 0 = flees instantly, 100 = fights to the death
    goals: list[str] = Field(default_factory=list)
    tactics: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_alive(self) -> bool:
        return not self.dead

    @property
    def is_dying(self) -> bool:
        """Down but not gone: unconscious at zero hit points, making saves."""
        return self.hp.is_down and not self.dead

    @property
    def can_act(self) -> bool:
        return self.is_alive and not self.hp.is_down

    def has_condition(self, condition) -> bool:
        return any(c.type == condition for c in self.conditions)


class Character(Creature):
    """A player character: a creature plus the things only players have."""

    kind: CreatureKind = CreatureKind.PLAYER
    pronouns: str = "they/them"
    background: str = ""
    personality: Personality = Field(default_factory=Personality)
    experience: int = 0
    #: Which human player controls this sheet (multiplayer, spec section 5).
    player_id: str | None = None
