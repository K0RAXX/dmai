"""Quests, objectives, and the campaign's story ledger."""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .base import Model, Visibility, new_id


class QuestStatus(str, Enum):
    RUMORED = "rumored"      # heard about, not yet accepted
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class Objective(Model):
    id: str = Field(default_factory=lambda: new_id("obj"))
    description: str
    completed: bool = False
    optional: bool = False
    visibility: Visibility = Visibility.PUBLIC


class Quest(Model):
    id: str = Field(default_factory=lambda: new_id("quest"))
    title: str
    summary: str = ""
    status: QuestStatus = QuestStatus.RUMORED
    objectives: list[Objective] = Field(default_factory=list)
    giver_npc_id: str | None = None
    location_id: str | None = None
    reward: str = ""
    is_main_plot: bool = False
    #: Quests unlocked when this one resolves.
    follow_ups: list[str] = Field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        required = [o for o in self.objectives if not o.optional]
        return bool(required) and all(o.completed for o in required)


class Consequence(Model):
    """A pending result of a player decision, resolved later by the world."""

    id: str = Field(default_factory=lambda: new_id("csq"))
    trigger: str = Field(description="What the players did")
    effect: str = Field(description="What the world will do about it")
    resolved: bool = False
    #: In-world day this should fire; None means "when narratively apt".
    due_day: int | None = None


class CampaignState(Model):
    """The story layer: what has happened and what is owed (spec section 8)."""

    premise: str = ""
    quests: dict[str, Quest] = Field(default_factory=dict)
    decisions: list[str] = Field(default_factory=list)
    consequences: list[Consequence] = Field(default_factory=list)
    discoveries: list[str] = Field(default_factory=list)

    @property
    def active_quests(self) -> list[Quest]:
        return [q for q in self.quests.values() if q.status == QuestStatus.ACTIVE]
