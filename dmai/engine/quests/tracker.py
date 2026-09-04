"""Quests, objectives, and the ledger of what the world owes the party.

Spec sections 8 and 29.  Two ideas carry this module:

* **A quest is written to the log whole.**  Status changes, new objectives and
  rewards all record the resulting `Quest`, not a delta, so replay is exact and
  a save written by a newer build degrades gracefully in an older one.
* **A decision is not the same as its consequence.**  `decide` records what the
  players chose; `promise` records what the world will do about it, later.
  Keeping the two apart is what lets the DM honour a choice made three sessions
  ago instead of quietly forgetting it (anti-railroading, spec section 29).
"""

from __future__ import annotations

from ..models.base import Visibility
from ..models.events import EventType
from ..models.quests import Consequence, Objective, Quest, QuestStatus
from ..state import Journal

#: Transitions the tracker will make.  Anything else is a bug in the caller,
#: not a judgement call: a completed quest does not quietly reopen.
ALLOWED_TRANSITIONS: dict[QuestStatus, set[QuestStatus]] = {
    QuestStatus.RUMORED: {QuestStatus.ACTIVE, QuestStatus.ABANDONED, QuestStatus.FAILED},
    QuestStatus.ACTIVE: {QuestStatus.COMPLETED, QuestStatus.FAILED, QuestStatus.ABANDONED},
    QuestStatus.ABANDONED: {QuestStatus.ACTIVE},
    QuestStatus.COMPLETED: set(),
    QuestStatus.FAILED: set(),
}


class QuestError(ValueError):
    """The quest log was asked for something the story does not allow."""


class QuestTracker:
    """The write path for everything in `GameState.story`."""

    # --- creating ----------------------------------------------------------

    def add(
        self,
        journal: Journal,
        title: str,
        *,
        summary: str = "",
        objectives: list[str | Objective] | None = None,
        status: QuestStatus = QuestStatus.RUMORED,
        giver_npc_id: str | None = None,
        location_id: str | None = None,
        reward: str = "",
        is_main_plot: bool = False,
        follow_ups: list[str] | None = None,
        visibility: Visibility = Visibility.PUBLIC,
    ) -> Quest:
        """Put a quest on the board.  Rumoured by default: heard, not accepted."""
        quest = Quest(
            title=title,
            summary=summary,
            objectives=[_objective(o) for o in objectives or []],
            status=status,
            giver_npc_id=giver_npc_id,
            location_id=location_id,
            reward=reward,
            is_main_plot=is_main_plot,
            follow_ups=follow_ups or [],
        )
        journal.record(
            EventType.QUEST_ADDED,
            target_id=quest.id,
            summary=f"New quest: {quest.title}.",
            quest=quest.model_dump(mode="json"),
            visibility=visibility,
        )
        return journal.state.story.quests[quest.id]

    def add_objective(
        self,
        journal: Journal,
        quest_id: str,
        description: str,
        *,
        optional: bool = False,
        visibility: Visibility = Visibility.PUBLIC,
    ) -> Objective:
        """Extend a quest the party is already running -- the plot thickens."""
        quest = self._require(journal, quest_id)
        objective = Objective(
            description=description, optional=optional, visibility=visibility
        )
        updated = quest.model_copy(deep=True)
        updated.objectives = [*updated.objectives, objective]
        self._write(journal, updated, f"{quest.title}: {description}")
        return objective

    # --- moving a quest along ----------------------------------------------

    def accept(self, journal: Journal, quest_id: str) -> Quest:
        """The party takes the job."""
        return self.set_status(journal, quest_id, QuestStatus.ACTIVE)

    def complete_objective(
        self, journal: Journal, quest_id: str, objective_id: str
    ) -> Quest:
        """Tick one box.  The quest completes itself when the last one falls."""
        quest = self._require(journal, quest_id)
        objective = next((o for o in quest.objectives if o.id == objective_id), None)
        if objective is None:
            raise QuestError(f"{quest.title} has no objective {objective_id}")

        if not objective.completed:
            journal.record(
                EventType.OBJECTIVE_COMPLETED,
                target_id=quest_id,
                summary=f"{quest.title}: {objective.description}.",
                quest_id=quest_id,
                objective_id=objective_id,
                visibility=objective.visibility,
            )
        # The reducer flips ACTIVE -> COMPLETED once every required objective
        # is done; log that resolution so the story ledger reads correctly.
        quest = journal.state.story.quests[quest_id]
        if quest.status is QuestStatus.COMPLETED:
            self._write(journal, quest, f"Quest complete: {quest.title}.")
        return journal.state.story.quests[quest_id]

    def complete(self, journal: Journal, quest_id: str, *, reward: str = "") -> Quest:
        """Close a quest out, marking every required objective done.

        Used when the players resolve a quest in a way its objectives never
        anticipated -- which, per spec section 29, is a success and not an
        error.
        """
        quest = self._require(journal, quest_id)
        updated = quest.model_copy(deep=True)
        for objective in updated.objectives:
            if not objective.optional:
                objective.completed = True
        updated.status = QuestStatus.COMPLETED
        if reward:
            updated.reward = reward
        self._write(journal, updated, f"Quest complete: {updated.title}.")
        return journal.state.story.quests[quest_id]

    def fail(self, journal: Journal, quest_id: str, reason: str = "") -> Quest:
        return self.set_status(journal, quest_id, QuestStatus.FAILED, reason=reason)

    def abandon(self, journal: Journal, quest_id: str, reason: str = "") -> Quest:
        return self.set_status(journal, quest_id, QuestStatus.ABANDONED, reason=reason)

    def set_status(
        self,
        journal: Journal,
        quest_id: str,
        status: QuestStatus,
        *,
        reason: str = "",
    ) -> Quest:
        quest = self._require(journal, quest_id)
        if status is quest.status:
            return quest
        if status not in ALLOWED_TRANSITIONS[quest.status]:
            raise QuestError(
                f"{quest.title} is {quest.status.value}; it cannot become {status.value}"
            )
        updated = quest.model_copy(deep=True)
        updated.status = status
        headline = {
            QuestStatus.ACTIVE: f"Quest accepted: {quest.title}.",
            QuestStatus.COMPLETED: f"Quest complete: {quest.title}.",
            QuestStatus.FAILED: f"Quest failed: {quest.title}.",
            QuestStatus.ABANDONED: f"Quest abandoned: {quest.title}.",
            QuestStatus.RUMORED: f"Quest set aside: {quest.title}.",
        }[status]
        if reason:
            headline = f"{headline} ({reason})"
        self._write(journal, updated, headline)
        return journal.state.story.quests[quest_id]

    def unlock_follow_ups(self, journal: Journal, quest_id: str) -> list[Quest]:
        """Move a resolved quest's follow-ups from nowhere onto the board.

        Follow-ups are stored as titles, so a DM can author a chain before the
        later links exist as quests.
        """
        quest = self._require(journal, quest_id)
        if quest.status not in {QuestStatus.COMPLETED, QuestStatus.FAILED}:
            raise QuestError(f"{quest.title} has not resolved yet")
        existing = {q.title for q in journal.state.story.quests.values()}
        return [
            self.add(
                journal,
                title,
                summary=f"Follows from {quest.title}.",
                is_main_plot=quest.is_main_plot,
            )
            for title in quest.follow_ups
            if title not in existing
        ]

    # --- the story ledger --------------------------------------------------

    def discover(
        self,
        journal: Journal,
        content: str,
        *,
        actor_id: str | None = None,
        visibility: Visibility = Visibility.PUBLIC,
        audience_id: str | None = None,
    ) -> None:
        """Record what the party now knows (spec 31, WHAT PLAYERS KNOW)."""
        journal.record(
            EventType.DISCOVERY,
            actor_id=actor_id,
            summary=content,
            content=content,
            visibility=visibility,
            audience_id=audience_id,
        )

    def decide(
        self,
        journal: Journal,
        decision: str,
        *,
        actor_id: str | None = None,
        effect: str = "",
        due_day: int | None = None,
    ) -> Consequence | None:
        """Log a player choice, and optionally what the world owes it.

        The DM never decides *for* the players; it records what they chose and
        lets the world answer later.
        """
        journal.record(
            EventType.STORY_DECISION,
            actor_id=actor_id,
            summary=decision,
            decision=decision,
        )
        if not effect:
            return None
        return self.promise(journal, decision, effect, due_day=due_day)

    def promise(
        self,
        journal: Journal,
        trigger: str,
        effect: str,
        *,
        due_day: int | None = None,
    ) -> Consequence:
        """Owe the players a consequence.  Fired later by the world simulator."""
        consequence = Consequence(trigger=trigger, effect=effect, due_day=due_day)
        journal.record(
            EventType.CONSEQUENCE_ADDED,
            summary=f"Pending consequence: {effect}",
            consequence=consequence.model_dump(mode="json"),
            visibility=Visibility.DM_ONLY,
        )
        return consequence

    def resolve(
        self, journal: Journal, consequence_id: str, *, narration: str = ""
    ) -> Consequence:
        """Pay a debt the world owed the party."""
        consequence = self._consequence(journal, consequence_id)
        if consequence.resolved:
            return consequence
        journal.record(
            EventType.CONSEQUENCE_RESOLVED,
            summary=narration or consequence.effect,
            consequence_id=consequence_id,
        )
        return self._consequence(journal, consequence_id)

    # --- reading -----------------------------------------------------------

    @staticmethod
    def pending(
        journal: Journal, *, on_or_before_day: int | None = None
    ) -> list[Consequence]:
        """Unresolved consequences, optionally only those now due.

        A consequence with no ``due_day`` is always returned: it fires when the
        moment is narratively apt, and that is a judgement the DM layer makes.
        """
        pending = [c for c in journal.state.story.consequences if not c.resolved]
        if on_or_before_day is None:
            return pending
        return [c for c in pending if c.due_day is None or c.due_day <= on_or_before_day]

    @staticmethod
    def by_status(journal: Journal, status: QuestStatus) -> list[Quest]:
        return [q for q in journal.state.story.quests.values() if q.status is status]

    @staticmethod
    def open_objectives(journal: Journal) -> list[tuple[Quest, Objective]]:
        """Every box still unticked on an active quest: the party's to-do list."""
        return [
            (quest, objective)
            for quest in journal.state.story.quests.values()
            if quest.status is QuestStatus.ACTIVE
            for objective in quest.objectives
            if not objective.completed
        ]

    # --- internals ---------------------------------------------------------

    @staticmethod
    def _require(journal: Journal, quest_id: str) -> Quest:
        quest = journal.state.story.quests.get(quest_id)
        if quest is None:
            raise QuestError(f"no quest {quest_id} in this campaign")
        return quest

    @staticmethod
    def _consequence(journal: Journal, consequence_id: str) -> Consequence:
        found = next(
            (c for c in journal.state.story.consequences if c.id == consequence_id), None
        )
        if found is None:
            raise QuestError(f"no pending consequence {consequence_id}")
        return found

    @staticmethod
    def _write(journal: Journal, quest: Quest, summary: str) -> None:
        journal.record(
            EventType.QUEST_UPDATED,
            target_id=quest.id,
            summary=summary,
            quest=quest.model_dump(mode="json"),
            status=quest.status.value,
        )


def _objective(spec: str | Objective) -> Objective:
    return spec if isinstance(spec, Objective) else Objective(description=spec)


__all__ = ["ALLOWED_TRANSITIONS", "QuestError", "QuestTracker"]
