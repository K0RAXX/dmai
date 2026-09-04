"""The map, the people on it, and the world that moves without the party."""

from __future__ import annotations

import pytest

from dmai.engine.dice import DiceEngine
from dmai.engine.models import (
    Attitude,
    Condition,
    ConditionType,
    EventType,
    Visibility,
)
from dmai.engine.models.world import NPC, Secret
from dmai.engine.rules import load_rules
from dmai.engine.state import EventStore, Journal, rebuild
from dmai.engine.world.atlas import Atlas, AtlasError
from dmai.engine.world.simulation import LONG_REST_MINUTES, WorldSimulator


@pytest.fixture
def journal(state) -> Journal:
    return Journal(state, EventStore(state.campaign.id))


@pytest.fixture
def atlas() -> Atlas:
    return Atlas()


@pytest.fixture
def rules():
    return load_rules("srd51")


@pytest.fixture
def world(rules) -> WorldSimulator:
    return WorldSimulator(rules, DiceEngine(seed=99))


# --- the map ---------------------------------------------------------------


def test_connections_are_two_way_by_default(journal, atlas):
    town = atlas.add_location(journal, "Emberfall")
    road = atlas.add_location(journal, "The Cinder Road", connect_to=[town.id])

    assert road.id in journal.state.world.locations[town.id].connections
    assert town.id in journal.state.world.locations[road.id].connections


def test_a_one_way_route_stays_one_way(journal, atlas):
    top = atlas.add_location(journal, "Cliff top")
    bottom = atlas.add_location(journal, "Ravine floor")
    atlas.connect(journal, top.id, bottom.id, one_way=True)

    assert bottom.id in journal.state.world.locations[top.id].connections
    assert journal.state.world.locations[bottom.id].connections == []


def test_route_finds_the_shortest_path(journal, atlas):
    gate = atlas.add_location(journal, "Town gate")
    road = atlas.add_location(journal, "Road", connect_to=[gate.id])
    ford = atlas.add_location(journal, "Ford", connect_to=[road.id])
    keep = atlas.add_location(journal, "Keep", connect_to=[ford.id])

    assert atlas.route(journal, gate.id, keep.id) == [gate.id, road.id, ford.id, keep.id]

    atlas.connect(journal, gate.id, keep.id)  # someone finds the tunnel

    assert atlas.route(journal, gate.id, keep.id) == [gate.id, keep.id]
    assert atlas.route(journal, gate.id, gate.id) == [gate.id]


def test_route_returns_none_when_there_is_no_way_through(journal, atlas):
    a = atlas.add_location(journal, "Here")
    island = atlas.add_location(journal, "Island")

    assert atlas.route(journal, a.id, island.id) is None
    assert atlas.route(journal, a.id, "loc-nowhere") is None


def test_entering_a_place_discovers_it_and_moves_the_party(journal, atlas):
    town = atlas.add_location(journal, "Emberfall")
    assert not town.discovered

    atlas.enter(journal, town.id)

    assert journal.state.current_location_id == town.id
    assert journal.state.world.locations[town.id].discovered


def test_an_undiscovered_place_is_not_announced_to_players(journal, atlas):
    atlas.add_location(journal, "The Hidden Vault")

    assert [e for e in journal.store.visible_to("player-1")] == []


def test_asking_about_a_place_that_is_not_there_is_an_error(journal, atlas):
    with pytest.raises(AtlasError, match="no location"):
        atlas.enter(journal, "loc-nowhere")


# --- people ----------------------------------------------------------------


def test_an_npc_is_placed_in_a_room_and_found_there(journal, atlas):
    inn = atlas.add_location(journal, "The Ash & Anchor")
    marda = atlas.add_npc(journal, "Marda Quill", location_id=inn.id, role="innkeeper")

    assert [n.name for n in atlas.npcs_at(journal, inn.id)] == ["Marda Quill"]
    assert marda.id in journal.state.world.locations[inn.id].npc_ids


def test_moving_an_npc_empties_the_room_they_left(journal, atlas):
    inn = atlas.add_location(journal, "Inn")
    street = atlas.add_location(journal, "Street", connect_to=[inn.id])
    marda = atlas.add_npc(journal, "Marda", location_id=inn.id)

    atlas.move_npc(journal, marda.id, street.id)

    assert atlas.npcs_at(journal, inn.id) == []
    assert journal.state.world.locations[inn.id].npc_ids == []
    assert [n.name for n in atlas.npcs_at(journal, street.id)] == ["Marda"]


def test_attitude_shifts_one_rung_at_a_time_and_clamps(journal, atlas):
    marda = atlas.add_npc(journal, "Marda")

    assert atlas.shift_attitude(journal, marda.id, "pc-1", 1) is Attitude.FRIENDLY
    assert atlas.shift_attitude(journal, marda.id, "pc-1", 5) is Attitude.ALLIED
    assert atlas.shift_attitude(journal, marda.id, "pc-1", 5) is Attitude.ALLIED
    assert atlas.shift_attitude(journal, marda.id, "pc-1", -9) is Attitude.HOSTILE


def test_attitudes_are_tracked_per_character(journal, atlas):
    marda = atlas.add_npc(journal, "Marda")
    atlas.set_attitude(journal, marda.id, "pc-1", Attitude.ALLIED)

    npc = journal.state.world.npcs[marda.id]
    assert npc.attitude_toward("pc-1") is Attitude.ALLIED
    assert npc.attitude_toward("pc-2") is Attitude.INDIFFERENT


def test_revealing_a_secret_records_who_now_knows(journal, atlas):
    secret = Secret(content="The mayor pays the raiders.")
    marda = atlas.add_npc(journal, NPC(name="Marda", secrets=[secret]))

    atlas.reveal(journal, marda.id, secret.id, "pc-1")

    kept = journal.state.world.npcs[marda.id].secrets[0]
    assert kept.known_by == ["pc-1"]
    assert kept.visibility is Visibility.DM_ONLY


# --- factions --------------------------------------------------------------


def test_standing_moves_and_clamps(journal, atlas):
    guild = atlas.add_faction(journal, "The Ash Guild")

    assert atlas.adjust_standing(journal, guild.id, 30, reason="returned the ledger") == 30
    assert atlas.adjust_standing(journal, guild.id, 500) == 100
    assert atlas.adjust_standing(journal, guild.id, -500) == -100


def test_faction_relations_are_written_on_both_sides(journal, atlas):
    guild = atlas.add_faction(journal, "Guild")
    crown = atlas.add_faction(journal, "Crown")

    atlas.set_relation(journal, guild.id, crown.id, -60)

    assert journal.state.world.factions[guild.id].relations[crown.id] == -60
    assert journal.state.world.factions[crown.id].relations[guild.id] == -60


def test_joining_a_faction_enrols_the_npc(journal, atlas):
    guild = atlas.add_faction(journal, "Guild")
    marda = atlas.add_npc(journal, "Marda", faction_id=guild.id)

    assert journal.state.world.factions[guild.id].member_ids == [marda.id]


# --- the clock and the world's own agenda ----------------------------------


def test_time_is_recorded_absolutely_so_replay_lands_on_the_same_hour(journal, world):
    world.advance_time(journal, 90)
    world.advance_time(journal, 60 * 24)

    replayed = rebuild(journal.state.campaign, journal.store.all())
    assert str(replayed.world.time) == str(journal.state.world.time)
    assert journal.state.world.time.day == 2


def test_a_long_rest_restores_hit_points_resources_and_eases_exhaustion(
    journal, world, rules
):
    from dmai.engine.characters.builder import create_character

    character = create_character(rules, "Vale", "human", "fighter", 3)
    character.hp.current = 4
    character.resources = {"second_wind": [0, 1]}
    character.conditions = [Condition(type=ConditionType.EXHAUSTION, level=2)]
    journal.record(
        EventType.CHARACTER_CREATED,
        character=character.model_dump(mode="json", by_alias=True),
    )

    world.long_rest(journal, [character.id])

    rested = journal.state.party[character.id]
    assert rested.hp.current == rested.hp.maximum
    assert rested.resources["second_wind"] == [1, 1]
    assert rested.conditions[0].level == 1
    assert journal.state.world.time.hour == 8 + LONG_REST_MINUTES // 60  # 08:00 -> 16:00


def test_a_short_rest_heals_only_what_the_hit_dice_give(journal, world, rules):
    from dmai.engine.characters.builder import create_character

    character = create_character(rules, "Vale", "human", "fighter", 3)
    character.hp.current = 1
    journal.record(
        EventType.CHARACTER_CREATED,
        character=character.model_dump(mode="json", by_alias=True),
    )

    healed = world.short_rest(journal, [character.id], hit_dice={character.id: 1})

    restored = journal.state.party[character.id]
    assert 0 < healed[character.id] <= 10 + 5
    assert restored.hp.current == 1 + healed[character.id]


def test_factions_advance_their_agenda_only_in_sandbox_mode(journal, world, atlas):
    atlas.add_faction(journal, "Guild", agenda=["Buy the docks", "Buy the guard"])

    journal.state.campaign.settings.sandbox_mode = False
    assert world.tick(journal, 60 * 24) == []

    journal.state.campaign.settings.sandbox_mode = True
    happened = world.tick(journal, 60 * 24)

    assert happened == ["Guild: Buy the docks"]
    assert journal.state.world.factions[list(journal.state.world.factions)[0]].agenda == [
        "Buy the guard"
    ]


def test_an_agenda_step_never_fires_twice(journal, world, atlas):
    atlas.add_faction(journal, "Guild", agenda=["The one plan"])

    world.advance_factions(journal)
    world.advance_factions(journal)

    assert journal.state.world.world_events == ["Guild: The one plan"]


def test_a_due_consequence_fires_when_the_day_arrives(journal, world):
    world.tracker.promise(journal, "let the smuggler go", "The crew raids the town", due_day=3)

    assert world.tick(journal, 60) == []

    fired = world.tick(journal, 60 * 24 * 3)

    assert fired == ["The crew raids the town"]
    assert world.tracker.pending(journal) == []


def test_a_world_event_is_dm_only_until_the_party_sees_it(journal, world):
    world.world_event(journal, "A tower falls in the north.")
    world.world_event(journal, "The bells ring.", visibility=Visibility.PUBLIC)

    seen = [e.summary for e in journal.store.visible_to("player-1")]
    assert seen == ["The bells ring."]


def test_the_weather_is_reproducible_under_a_seed(journal, rules):
    first = WorldSimulator(rules, DiceEngine(seed=5)).roll_weather(journal)
    journal.state.world.weather = "clear"
    second = WorldSimulator(rules, DiceEngine(seed=5)).roll_weather(journal)

    assert first == second
