"""Saving, loading, rollback and export (spec sections 10 and 16).

The property under test throughout is the one the spec calls *never lose
state*: what comes back off disk must be the campaign that went onto it.
"""

from __future__ import annotations

import json

import pytest

from dmai.engine.models import Campaign, CampaignSettings
from dmai.engine.session import GameSession
from dmai.persistence import CampaignStore, SaveError
from dmai.persistence.store import CURRENT_SAVE_VERSION, EVENTS_FILE, default_root


@pytest.fixture
def store(tmp_path) -> CampaignStore:
    return CampaignStore(tmp_path / "campaigns")


@pytest.fixture
def played(campaign: Campaign) -> GameSession:
    """A campaign with a bit of everything in it."""
    session = GameSession.create(campaign)
    player = session.add_player("James", is_host=True)
    vale = session.add_character("Vale", "human", "fighter", 2, player_id=player.id)
    inn = session.atlas.add_location(session.journal, "The Ash & Anchor")
    session.atlas.enter(session.journal, inn.id)
    marda = session.atlas.add_npc(session.journal, "Marda Quill", location_id=inn.id)
    session.npc_says(marda.id, "The caravan never came back.")
    quest = session.quests.add(session.journal, "The Cinder Road", objectives=["Find it"])
    session.quests.accept(session.journal, quest.id)
    session.spawn("goblin", 2)
    session.world.advance_time(session.journal, 120)
    session.inventory.give(session.journal, vale.id, "longsword", reason="loot")
    return session


def test_a_saved_campaign_loads_back_identical(store, played):
    store.save(played)

    reloaded = store.load(played.campaign.id)

    assert reloaded.store.head == played.store.head
    assert reloaded.state.model_dump(mode="json") == played.state.model_dump(mode="json")


def test_the_state_is_never_written_only_the_log(store, played):
    directory = store.save(played)

    files = sorted(p.name for p in directory.iterdir())
    assert files == ["campaign.json", "events.jsonl"]


def test_saving_twice_appends_rather_than_rewriting(store, played):
    store.save(played)
    first = (store.path_for(played.campaign.id) / EVENTS_FILE).read_text(encoding="utf-8")

    played.narrate("The fire gutters.")
    store.save(played)
    second = (store.path_for(played.campaign.id) / EVENTS_FILE).read_text(encoding="utf-8")

    assert second.startswith(first)
    assert len(second.splitlines()) == len(first.splitlines()) + 1


def test_a_rollback_is_persisted_as_a_shorter_log(store, played):
    mark = played.checkpoint("before the goblins")
    played.spawn("goblin", 4)
    store.save(played)

    played.rollback_to_checkpoint(mark)
    store.save(played)

    reloaded = store.load(played.campaign.id)
    assert reloaded.store.head == mark.event_seq
    assert reloaded.state.bestiary == played.state.bestiary


def test_a_campaign_resumes_where_it_left_off(store, played):
    store.save(played)
    before = played.briefing()

    resumed = store.load(played.campaign.id)

    assert resumed.briefing() == before
    assert resumed.state.location.name == "The Ash & Anchor"


def test_play_continues_after_a_reload(store, played):
    store.save(played)

    resumed = store.load(played.campaign.id)
    resumed.narrate("Morning comes.")
    store.save(resumed)

    assert store.load(played.campaign.id).store.head == resumed.store.head


def test_the_campaign_list_needs_no_replay(store, played):
    store.save(played)

    summaries = store.list_campaigns()

    assert [s.name for s in summaries] == [played.campaign.name]
    assert summaries[0].events == played.store.head


def test_a_broken_save_does_not_hide_the_working_ones(store, played, campaign):
    store.save(played)
    other = GameSession.create(
        Campaign(name="Second Table", settings=CampaignSettings(rng_seed=2))
    )
    store.save(other)
    (store.path_for(other.campaign.id) / "campaign.json").write_text("{ not json", encoding="utf-8")

    assert [s.name for s in store.list_campaigns()] == [played.campaign.name]


def test_a_truncated_final_line_replays_to_the_moment_before(store, played):
    store.save(played)
    path = store.path_for(played.campaign.id) / EVENTS_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 99, "type": "dm_nar')  # a crash mid-append

    reloaded = store.load(played.campaign.id)

    assert reloaded.store.head == played.store.head


def test_loading_a_campaign_that_is_not_there_is_an_error(store):
    with pytest.raises(SaveError, match="no campaign saved"):
        store.load("camp-nothing")


def test_a_campaign_id_cannot_climb_out_of_the_store(store):
    for hostile in ("../elsewhere", "a/b", "", ".hidden"):
        with pytest.raises(SaveError, match="unsafe campaign id"):
            store.path_for(hostile)


def test_a_save_from_a_newer_build_is_refused_not_guessed_at(store, played):
    store.save(played)
    path = store.path_for(played.campaign.id) / "campaign.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["save_version"] = CURRENT_SAVE_VERSION + 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SaveError, match="newer build"):
        store.load(played.campaign.id)


def test_export_and_import_move_a_campaign_whole(store, played, tmp_path):
    store.save(played)

    bundle = store.export(played.campaign.id, tmp_path / "emberfall.json")
    imported = store.import_bundle(bundle, new_id="camp-imported")

    copy = store.load("camp-imported")
    assert imported.id == "camp-imported"
    assert copy.store.head == played.store.head
    assert copy.transcript(is_dm=True) == played.transcript(is_dm=True)


def test_importing_the_same_bundle_twice_makes_two_tables(store, played, tmp_path):
    store.save(played)
    bundle = store.export(played.campaign.id, tmp_path / "b.json")

    store.import_bundle(bundle, new_id="camp-table-a")
    store.import_bundle(bundle, new_id="camp-table-b")

    a = store.load("camp-table-a")
    b = store.load("camp-table-b")
    a.narrate("Table A goes left.")

    assert a.store.head != b.store.head


def test_a_file_that_is_not_a_bundle_is_refused(store, tmp_path):
    stray = tmp_path / "notes.json"
    stray.write_text('{"format": "something else"}', encoding="utf-8")

    with pytest.raises(SaveError, match="not a DungeonMaster AI campaign bundle"):
        store.import_bundle(stray)


def test_deleting_a_campaign_removes_it(store, played):
    store.save(played)

    store.delete(played.campaign.id)

    assert not store.exists(played.campaign.id)
    assert store.list_campaigns() == []


def test_the_default_root_follows_dmai_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DMAI_HOME", str(tmp_path / "portable"))

    assert default_root() == tmp_path / "portable" / "campaigns"
