"""Combat and encounter schemas."""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .base import Difficulty, Model, new_id


class Combatant(Model):
    creature_id: str
    initiative: int = 0
    #: Dexterity modifier, kept for deterministic tie-breaking.
    initiative_tiebreak: int = 0
    is_player: bool = False
    has_acted: bool = False
    movement_remaining: int = 30
    action_used: bool = False
    bonus_action_used: bool = False
    reaction_used: bool = False
    fled: bool = False
    surrendered: bool = False
    #: Lost the first round to an ambush; their opening turn is skipped.
    surprised: bool = False

    @property
    def active(self) -> bool:
        return not (self.fled or self.surrendered)


class CombatState(Model):
    #: Empty until a fight actually starts.  A generated id on a combat that
    #: never happened would be the one part of the state that a replay could
    #: not reproduce, so there isn't one.
    id: str = ""
    active: bool = False
    round: int = 0
    turn_index: int = 0
    #: Initiative order, highest first.
    order: list[Combatant] = Field(default_factory=list)
    encounter_id: str | None = None

    @property
    def current(self) -> Combatant | None:
        live = [c for c in self.order if c.active]
        if not live or not self.active:
            return None
        return self.order[self.turn_index % len(self.order)]


class EncounterKind(str, Enum):
    COMBAT = "combat"
    SOCIAL = "social"
    EXPLORATION = "exploration"
    PUZZLE = "puzzle"
    TRAP = "trap"
    MYSTERY = "mystery"
    TRAVEL = "travel"
    ENVIRONMENTAL = "environmental"
    RANDOM = "random"


class Encounter(Model):
    id: str = Field(default_factory=lambda: new_id("enc"))
    kind: EncounterKind
    name: str = ""
    description: str = ""
    difficulty: Difficulty = Difficulty.MODERATE
    location_id: str | None = None
    #: Creature ids participating (monsters are added to the campaign roster).
    creature_ids: list[str] = Field(default_factory=list)
    #: What ends this encounter well, and what ends it badly.
    success_conditions: list[str] = Field(default_factory=list)
    failure_conditions: list[str] = Field(default_factory=list)
    rewards: list[str] = Field(default_factory=list)
    resolved: bool = False
    #: The DM may abandon an encounter the players have made irrelevant
    #: (spec section 12).
    abandoned: bool = False
