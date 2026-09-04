"""The rules adapter interface.

Nothing above this layer may assume a particular game system.  A rules pack is
*data*; this class is the small amount of behaviour needed to interpret it.
Swap in another implementation to play a different game.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models.base import Ability, DamageType
from ..models.character import Creature


class RulesEngine(ABC):
    """Everything the engine needs to ask of a ruleset."""

    id: str = "abstract"
    name: str = "Abstract Rules"

    # --- core maths --------------------------------------------------------

    @abstractmethod
    def ability_modifier(self, score: int) -> int: ...

    @abstractmethod
    def proficiency_bonus(self, level: int) -> int: ...

    @abstractmethod
    def skill_modifier(self, creature: Creature, skill: str) -> int: ...

    @abstractmethod
    def save_modifier(self, creature: Creature, ability: Ability) -> int: ...

    @abstractmethod
    def armor_class(self, creature: Creature) -> int: ...

    @abstractmethod
    def max_hit_points(self, class_key: str, level: int, con_modifier: int) -> int: ...

    # --- combat ------------------------------------------------------------

    @abstractmethod
    def attack_bonus(self, creature: Creature, weapon_key: str | None = None) -> int: ...

    @abstractmethod
    def damage_expression(
        self, creature: Creature, weapon_key: str | None = None, critical: bool = False
    ) -> tuple[str, DamageType]: ...

    @abstractmethod
    def apply_damage(self, creature: Creature, amount: int, damage_type: DamageType) -> int:
        """Apply damage after resistances.  Returns the amount actually dealt."""

    @abstractmethod
    def heal(self, creature: Creature, amount: int) -> int: ...

    # --- content lookup ----------------------------------------------------

    @abstractmethod
    def weapon(self, key: str) -> dict | None: ...

    @abstractmethod
    def armor(self, key: str) -> dict | None: ...

    @abstractmethod
    def monster(self, key: str) -> dict | None: ...

    @abstractmethod
    def species(self, key: str) -> dict | None: ...

    @abstractmethod
    def character_class(self, key: str) -> dict | None: ...

    # --- encounter budgeting ----------------------------------------------

    @abstractmethod
    def xp_threshold(self, level: int, difficulty: str) -> int: ...
