"""The CLI, exercised the way a player uses it.

The point of these tests is not the terminal output -- it is that a client can
drive the whole engine through one narrow surface: create, play, save, quit,
reopen, and find the campaign exactly as it was left.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from dmai.cli.main import app
from dmai.engine.models.events import EventType
from dmai.persistence import CampaignStore


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private campaign directory, so tests never touch a real save."""
    monkeypatch.setenv("DMAI_HOME", str(tmp_path))
    return tmp_path


def run(runner: CliRunner, *args: str, stdin: str | None = None):
    result = runner.invoke(app, list(args), input=stdin)
    assert result.exit_code == 0, f"{args} failed:\n{result.output}\n{result.exception}"
    return result


def test_a_campaign_can_be_created_and_listed(runner, home):
    run(runner, "new", "Ashes of Emberfall", "-p", "A caravan vanished.", "--seed", "7")

    listing = run(runner, "list")

    assert "Ashes" in listing.output
    assert CampaignStore(home / "campaigns").list_campaigns()[0].name == "Ashes of Emberfall"


def test_characters_are_rolled_up_and_persisted(runner, home):
    run(runner, "new", "Emberfall", "--seed", "7")
    run(runner, "character", "add", "Emberfall", "Vale", "--class", "fighter", "--player", "James")

    sheet = run(runner, "character", "sheet", "Emberfall", "Vale")

    assert "Vale" in sheet.output
    assert "Fighter" in sheet.output

    store = CampaignStore(home / "campaigns")
    session = store.load(store.list_campaigns()[0].id)
    assert [c.name for c in session.state.party.values()] == ["Vale"]


def test_a_campaign_is_found_by_name_not_just_id(runner, home):
    run(runner, "new", "The Long Winter", "--seed", "1")

    assert "Long Winter" in run(runner, "status", "Long Winter").output


def test_an_unknown_campaign_is_a_clean_error(runner, home):
    result = runner.invoke(app, ["status", "Nothing At All"])

    assert result.exit_code != 0
    assert "no campaign matching" in result.output


def test_a_played_session_survives_quitting_and_reopening(runner, home):
    run(runner, "new", "Emberfall", "--seed", "7")
    run(runner, "character", "add", "Emberfall", "Vale", "--player", "James")

    run(
        runner,
        "play",
        "Emberfall",
        "--as",
        "James",
        stdin="/narrate The inn is warm.\n/roll 1d20\nI ask about the caravan\n/quit\n",
    )

    log = run(runner, "log", "Emberfall")

    assert "The inn is warm." in log.output
    assert "I ask about the caravan" in log.output


def test_a_players_line_is_recorded_once_not_twice(runner, home):
    """`player_says` logs, and `take_turn` logs -- doing both duplicated it."""
    run(runner, "new", "Emberfall", "--seed", "7")
    run(runner, "character", "add", "Emberfall", "Vale", "--player", "James")
    run(
        runner,
        "play",
        "Emberfall",
        "--as",
        "James",
        stdin="I ask about the caravan\n/quit\n",
    )

    store = CampaignStore(home / "campaigns")
    session = store.load(store.list_campaigns()[0].id)
    spoken = [
        event
        for event in session.store
        if event.type is EventType.PLAYER_ACTION
        and event.data.get("text") == "I ask about the caravan"
    ]

    assert len(spoken) == 1
    # And one action id, not two events sharing one.
    assert len({event.data["action_id"] for event in spoken}) == 1


def test_a_checkpoint_can_be_rolled_back_to_from_the_table(runner, home):
    run(runner, "new", "Emberfall", "--seed", "7")
    run(runner, "character", "add", "Emberfall", "Vale")
    run(runner, "play", "Emberfall", stdin="/checkpoint here\n/quit\n")

    store = CampaignStore(home / "campaigns")
    campaign_id = store.list_campaigns()[0].id
    mark = store.load(campaign_id).checkpoints()[0]

    run(
        runner,
        "play",
        "Emberfall",
        stdin=f"/narrate A mistake.\n/rollback {mark.event_seq}\n/quit\n",
    )

    log = run(runner, "log", "Emberfall").output
    assert "A mistake." not in log
    assert store.load(campaign_id).checkpoints()  # the mark survived its own rollback


def test_a_private_whisper_never_reaches_another_seat(runner, home):
    run(runner, "new", "Emberfall", "--seed", "7")
    run(runner, "character", "add", "Emberfall", "Vale", "--player", "James")
    run(runner, "character", "add", "Emberfall", "Nim", "--player", "Dana")

    store = CampaignStore(home / "campaigns")
    session = store.load(store.list_campaigns()[0].id)
    james = next(p for p in session.state.players.values() if p.name == "James")
    session.whisper(james.id, "You spot the tripwire.")
    store.save(session)

    assert "tripwire" in run(runner, "log", "Emberfall", "--as", "James").output
    assert "tripwire" not in run(runner, "log", "Emberfall", "--as", "Dana").output


def test_export_and_import_round_trip_through_the_cli(runner, home, tmp_path):
    run(runner, "new", "Emberfall", "--seed", "7")
    run(runner, "character", "add", "Emberfall", "Vale")
    bundle = tmp_path / "bundle.json"

    run(runner, "export", "Emberfall", str(bundle))
    run(runner, "import", str(bundle), "--as", "camp-copy")

    store = CampaignStore(home / "campaigns")
    assert store.load("camp-copy").state.party
    assert len(store.list_campaigns()) == 2


def test_an_unknown_rules_pack_is_reported_not_silently_swapped(runner, home):
    result = runner.invoke(app, ["new", "Broken", "--rules", "not-a-real-pack"])

    assert result.exit_code != 0
