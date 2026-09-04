"""The engine's front door, and the promises clients rely on.

These tests double as the demonstration that the engine is playable without a
GUI, a network or a language model -- which is the spec's central architectural
requirement.
"""

from __future__ import annotations

import pytest

from dmai.engine.models import (
    Campaign,
    CampaignSettings,
    DamageType,
    EventType,
    Visibility,
)
from dmai.engine.session import GameSession
from dmai.engine.state import rebuild


@pytest.fixture
def session(campaign: Campaign) -> GameSession:
    return GameSession.create(campaign)


def test_a_new_campaign_opens_with_its_creation_logged(session):
    assert session.store.head == 1
    assert next(iter(session.store)).type is EventType.CAMPAIGN_CREATED
    assert session.rules.id == "srd51"


def test_a_character_joins_the_party_and_is_fully_derived(session):
    player = session.add_player("James", is_host=True)
    vale = session.add_character("Vale", "human", "fighter", 2, player_id=player.id)

    assert session.state.party[vale.id].name == "Vale"
    assert vale.hp.maximum > 0
    assert vale.armor_class > 10  # chain mail and a shield, from the pack
    assert session.state.players[player.id].character_ids == [vale.id]


def test_a_player_is_found_again_by_their_chat_identity(session):
    session.add_player("James", external_id="discord:1234")

    assert session.player_by_external_id("discord:1234").name == "James"
    assert session.player_by_external_id("discord:9999") is None


def test_experience_is_split_across_the_living_party(session):
    a = session.add_character("Vale", "human", "fighter")
    b = session.add_character("Nim", "elf", "rogue")

    session.award_experience(100, reason="the goblin camp")

    assert session.state.party[a.id].experience == 50
    assert session.state.party[b.id].experience == 50


def test_levelling_up_raises_hit_points_and_proficiency(session):
    vale = session.add_character("Vale", "human", "fighter", 4)
    before = vale.hp.maximum

    levelled = session.award_level(vale.id)

    assert levelled.level == 5
    assert levelled.hp.maximum > before
    assert levelled.proficiency_bonus == 3


def test_private_information_stays_with_its_seat(session):
    james = session.add_player("James")
    dana = session.add_player("Dana")
    session.narrate("The hall is silent.")
    session.whisper(james.id, "You alone notice the tripwire.")

    assert "You alone notice the tripwire." in session.transcript(james.id)
    assert "You alone notice the tripwire." not in session.transcript(dana.id)
    assert "You alone notice the tripwire." in session.transcript(is_dm=True)


def test_a_dm_only_event_reaches_nobody_at_the_table(session):
    session.narrate("A shape moves in the rafters.", visibility=Visibility.DM_ONLY)

    assert "A shape moves in the rafters." not in session.transcript("player-1")
    assert "A shape moves in the rafters." in session.transcript(is_dm=True)


def test_rolling_dice_is_logged_where_the_table_can_see_it(session):
    result = session.roll("1d20+3", reason="perception")

    assert 4 <= result.total <= 23
    assert any(e.type is EventType.DICE_ROLLED for e in session.store)


def test_the_same_seed_produces_the_same_game(campaign):
    def play() -> list[int]:
        session = GameSession.create(campaign.model_copy(deep=True))
        return [session.roll("1d20").total for _ in range(10)]

    assert play() == play()


def test_a_campaign_rebuilds_exactly_from_its_log(session):
    player = session.add_player("James")
    session.add_character("Vale", "human", "fighter", 2, player_id=player.id)
    inn = session.atlas.add_location(session.journal, "The Ash & Anchor", kind="building")
    session.atlas.enter(session.journal, inn.id)
    session.atlas.add_npc(session.journal, "Marda Quill", location_id=inn.id)
    quest = session.quests.add(session.journal, "The Cinder Road", objectives=["Find it"])
    session.quests.accept(session.journal, quest.id)
    session.spawn("goblin", 2)
    session.world.advance_time(session.journal, 45)

    replayed = rebuild(session.campaign, session.store.all())

    assert replayed.model_dump(mode="json") == session.state.model_dump(mode="json")


def test_rolling_back_to_a_checkpoint_undoes_everything_after_it(session):
    session.add_character("Vale", "human", "fighter")
    mark = session.checkpoint("before the ambush")
    session.spawn("goblin", 3)
    assert len(session.state.bestiary) == 3

    session.rollback_to_checkpoint(mark)

    assert session.state.bestiary == {}
    assert session.state.party  # the party survived the rewind
    assert session.store.head == mark.event_seq
    # A checkpoint marks its own event, so rolling back to it does not erase it.
    assert [c.label for c in session.checkpoints()] == ["before the ambush"]


def test_checkpoints_are_readable_back_out_of_the_log(session):
    session.checkpoint("one")
    session.checkpoint("two", automatic=True)

    labels = [c.label for c in session.checkpoints()]
    assert labels == ["one", "two"]


def test_a_recap_reports_only_what_the_log_contains(session):
    sitting = session.begin_session()
    session.quests.discover(session.journal, "The caravan never reached the pass.")
    inn = session.atlas.add_location(session.journal, "Emberfall")
    session.atlas.enter(session.journal, inn.id)

    recap = session.recap(since=sitting.first_event_seq)

    assert recap["discoveries"] == ["The caravan never reached the pass."]
    assert recap["location"] == "Emberfall"
    assert recap["combats"] == []
    assert "Emberfall" in recap["narrative"]


def test_ending_a_session_attaches_its_recap(session):
    sitting = session.begin_session()
    session.quests.discover(session.journal, "The mayor is lying.")

    closed = session.end_session(sitting)

    # The record covers play, not the end marker that carries it.
    assert closed.last_event_seq == session.store.head - 1
    assert "The mayor is lying." in closed.recap


def test_the_briefing_answers_the_resume_questions(session):
    player = session.add_player("James")
    vale = session.add_character("Vale", "human", "fighter", 2, player_id=player.id)
    inn = session.atlas.add_location(session.journal, "The Ash & Anchor")
    road = session.atlas.add_location(session.journal, "The Cinder Road", connect_to=[inn.id])
    session.atlas.enter(session.journal, inn.id)
    marda = session.atlas.add_npc(
        session.journal, "Marda Quill", location_id=inn.id, knowledge=["The mayor pays them"]
    )
    guild = session.atlas.add_faction(session.journal, "The Ash Guild", agenda=["Buy the docks"])
    session.atlas.adjust_standing(session.journal, guild.id, -20)
    quest = session.quests.add(session.journal, "The Cinder Road", objectives=["Find it"])
    session.quests.accept(session.journal, quest.id)
    session.quests.discover(session.journal, "The caravan never reached the pass.")
    session.quests.promise(session.journal, "they burned the bridge", "The valley is cut off", due_day=3)
    session.remember("The party distrusts the mayor.", importance=90)

    briefing = session.briefing()

    assert [c["name"] for c in briefing["who"]] == ["Vale"]
    assert briefing["who"][0]["player"] == "James"
    assert briefing["who"][0]["hp"] == f"{vale.hp.current}/{vale.hp.maximum}"
    assert briefing["where"]["location"] == "The Ash & Anchor"
    assert briefing["where"]["npcs_present"] == ["Marda Quill"]
    assert briefing["where"]["exits"] == ["The Cinder Road"]
    assert briefing["when"]["day"] == 1
    assert briefing["players_know"] == ["The caravan never reached the pass."]
    assert briefing["npcs_know"]["Marda Quill"] == ["The mayor pays them"]
    assert briefing["factions"] == [
        {"name": "The Ash Guild", "party_standing": -20, "next_move": "Buy the docks"}
    ]
    assert briefing["active_quests"][0]["title"] == "The Cinder Road"
    assert briefing["pending_consequences"][0]["effect"] == "The valley is cut off"
    assert briefing["memories"] == ["The party distrusts the mayor."]
    assert road.id  # the road exists whether or not the party went there


def test_the_briefing_does_not_leak_what_the_party_has_not_learned(session):
    from dmai.engine.models.world import NPC, Secret

    inn = session.atlas.add_location(session.journal, "Inn")
    session.atlas.enter(session.journal, inn.id)
    secret = Secret(content="The mayor pays the raiders.")
    session.atlas.add_npc(session.journal, NPC(name="Marda", secrets=[secret]), location_id=inn.id)

    briefing = session.briefing()

    assert "The mayor pays the raiders." in briefing["players_do_not_know"]
    assert "The mayor pays the raiders." not in briefing["players_know"]


def test_an_unknown_rules_pack_is_refused_rather_than_guessed(campaign):
    from dmai.engine.rules import RulesPackError

    campaign.settings = CampaignSettings(rules_pack="pathfinder-but-invented")

    with pytest.raises(RulesPackError, match="unknown rules pack"):
        GameSession.create(campaign)


def test_a_whole_solo_scene_runs_without_a_model_or_a_network(session):
    """The definition-of-done walk: party, place, talk, fight, loot, quest."""
    player = session.add_player("James", is_host=True)
    vale = session.add_character("Vale", "human", "fighter", 3, player_id=player.id)

    inn = session.atlas.add_location(session.journal, "The Ash & Anchor", kind="building")
    session.atlas.enter(session.journal, inn.id)
    marda = session.atlas.add_npc(session.journal, "Marda Quill", location_id=inn.id)
    session.atlas.meet(session.journal, marda.id, [vale.id])
    session.npc_says(marda.id, "The caravan never came back.")

    quest = session.quests.add(
        session.journal, "The Cinder Road", objectives=["Find the caravan"],
        giver_npc_id=marda.id,
    )
    session.quests.accept(session.journal, quest.id)

    session.player_says("I search the common room", character_id=vale.id, player_id=player.id)
    check = session.checks.check(session.journal, vale.id, skill="investigation", dc=10)
    assert check.total >= 1

    goblins = session.spawn("goblin", 2)
    session.combat.start(session.journal, [vale.id, *[g.id for g in goblins]])
    assert session.state.combat.active

    for goblin in goblins:  # the fighter wins; this is a smoke test, not a duel
        session.combat.deal_damage(
            session.journal, goblin.id, 50, DamageType.SLASHING, source_id=vale.id
        )
    assert session.combat.is_over(session.journal)
    session.combat.end(session.journal, "The goblins are down.")

    session.inventory.give(session.journal, vale.id, "longsword", reason="loot")
    session.quests.complete_objective(session.journal, quest.id, quest.objectives[0].id)
    session.award_experience(100, reason="the goblins")
    session.world.long_rest(session.journal, [vale.id])

    state = session.state
    assert state.story.quests[quest.id].status.value == "completed"
    assert state.party[vale.id].experience == 100
    assert state.party[vale.id].hp.current == state.party[vale.id].hp.maximum
    assert not state.combat.active
    assert rebuild(session.campaign, session.store.all()).model_dump(
        mode="json"
    ) == state.model_dump(mode="json")
