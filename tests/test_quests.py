"""Quests, the story ledger, and the promises the world keeps."""

from __future__ import annotations

import pytest

from dmai.engine.models import EventType, QuestStatus, Visibility
from dmai.engine.quests.tracker import QuestError, QuestTracker
from dmai.engine.state import EventStore, Journal, rebuild


@pytest.fixture
def journal(state) -> Journal:
    return Journal(state, EventStore(state.campaign.id))


@pytest.fixture
def tracker() -> QuestTracker:
    return QuestTracker()


def test_a_new_quest_starts_as_a_rumour(journal, tracker):
    quest = tracker.add(journal, "The Cinder Road", objectives=["Find the caravan"])

    assert quest.status is QuestStatus.RUMORED
    assert journal.state.story.quests[quest.id].title == "The Cinder Road"
    assert not journal.state.story.active_quests


def test_completing_the_last_objective_completes_the_quest(journal, tracker):
    quest = tracker.add(journal, "The Cinder Road", objectives=["Find it", "Bring it home"])
    tracker.accept(journal, quest.id)

    tracker.complete_objective(journal, quest.id, quest.objectives[0].id)
    assert journal.state.story.quests[quest.id].status is QuestStatus.ACTIVE

    quest = tracker.complete_objective(journal, quest.id, quest.objectives[1].id)
    assert quest.status is QuestStatus.COMPLETED


def test_optional_objectives_do_not_block_completion(journal, tracker):
    quest = tracker.add(journal, "Side Work", objectives=["Required"])
    tracker.add_objective(journal, quest.id, "Nice to have", optional=True)
    tracker.accept(journal, quest.id)

    quest = journal.state.story.quests[quest.id]
    required = next(o for o in quest.objectives if not o.optional)
    assert tracker.complete_objective(journal, quest.id, required.id).status is (
        QuestStatus.COMPLETED
    )


def test_a_quest_can_be_completed_a_way_nobody_planned(journal, tracker):
    """Spec section 29: an unexpected solution is a success, not an error."""
    quest = tracker.add(journal, "The Siege", objectives=["Kill the warlord"])
    tracker.accept(journal, quest.id)

    quest = tracker.complete(journal, quest.id, reward="the warlord's parole")

    assert quest.status is QuestStatus.COMPLETED
    assert all(o.completed for o in quest.objectives)
    assert quest.reward == "the warlord's parole"


def test_a_completed_quest_does_not_reopen(journal, tracker):
    quest = tracker.add(journal, "Done", status=QuestStatus.ACTIVE)
    tracker.complete(journal, quest.id)

    with pytest.raises(QuestError, match="cannot become"):
        tracker.accept(journal, quest.id)


def test_follow_ups_open_only_once_a_quest_resolves(journal, tracker):
    quest = tracker.add(journal, "The First Seal", follow_ups=["The Second Seal"])

    with pytest.raises(QuestError, match="has not resolved"):
        tracker.unlock_follow_ups(journal, quest.id)

    tracker.accept(journal, quest.id)
    tracker.complete(journal, quest.id)
    opened = tracker.unlock_follow_ups(journal, quest.id)

    assert [q.title for q in opened] == ["The Second Seal"]
    assert tracker.unlock_follow_ups(journal, quest.id) == []  # not twice


def test_a_decision_can_owe_the_world_a_consequence(journal, tracker):
    consequence = tracker.decide(
        journal,
        "The party let the smuggler go.",
        effect="The smuggler's crew raids Emberfall.",
        due_day=4,
    )

    assert journal.state.story.decisions == ["The party let the smuggler go."]
    assert tracker.pending(journal) == [consequence]
    assert tracker.pending(journal, on_or_before_day=2) == []
    assert tracker.pending(journal, on_or_before_day=4) == [consequence]

    tracker.resolve(journal, consequence.id)
    assert tracker.pending(journal) == []


def test_a_consequence_without_a_due_day_is_always_pending(journal, tracker):
    tracker.promise(journal, "They took the crown", "Someone will want it back")
    assert len(tracker.pending(journal, on_or_before_day=1)) == 1


def test_a_promise_is_dm_only_but_a_discovery_is_not(journal, tracker):
    tracker.promise(journal, "trigger", "effect")
    tracker.discover(journal, "The caravan never reached the pass.")

    player_view = [e.summary for e in journal.store.visible_to("player-1")]
    assert "The caravan never reached the pass." in player_view
    assert "Pending consequence: effect" not in player_view


def test_open_objectives_lists_only_live_work(journal, tracker):
    active = tracker.add(journal, "Live", objectives=["Do the thing"])
    tracker.accept(journal, active.id)
    tracker.add(journal, "Rumour", objectives=["Not yet"])

    assert [o.description for _, o in tracker.open_objectives(journal)] == ["Do the thing"]


def test_the_quest_log_replays_exactly(journal, tracker):
    quest = tracker.add(journal, "The Cinder Road", objectives=["Find it"])
    tracker.accept(journal, quest.id)
    tracker.complete_objective(journal, quest.id, quest.objectives[0].id)
    tracker.decide(journal, "Burned the bridge", effect="The valley is cut off", due_day=2)

    replayed = rebuild(journal.state.campaign, journal.store.all())

    assert replayed.model_dump(mode="json") == journal.state.model_dump(mode="json")


def test_a_secret_objective_stays_off_the_player_log(journal, tracker):
    quest = tracker.add(journal, "The Cinder Road")
    hidden = tracker.add_objective(
        journal, quest.id, "Betray the guild", visibility=Visibility.DM_ONLY
    )
    tracker.accept(journal, quest.id)
    tracker.complete_objective(journal, quest.id, hidden.id)

    completions = [
        e for e in journal.store.visible_to("player-1")
        if e.type is EventType.OBJECTIVE_COMPLETED
    ]
    assert completions == []
