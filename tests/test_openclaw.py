"""The OpenClaw adapter (spec sections 19 and 20).

The two properties worth defending here are the ones the spec calls out: the
bot exposes a real programmatic interface rather than driving a GUI, and it
"must never mix state between campaigns".
"""

from __future__ import annotations

import io
import json

import pytest

from dmai.integrations.openclaw import OPERATIONS, OpenClawAdapter, OpenClawError
from dmai.integrations.openclaw import bot as protocol
from dmai.persistence import CampaignStore


@pytest.fixture
def bot(tmp_path) -> OpenClawAdapter:
    return OpenClawAdapter(CampaignStore(tmp_path / "campaigns"))


@pytest.fixture
def table(bot: OpenClawAdapter) -> dict:
    """One campaign, one seated player, one character."""
    campaign = bot.create_campaign(
        "Party Alpha", premise="A caravan vanished.", settings={"rng_seed": 5}
    )
    seat = bot.join(campaign["campaign_id"], "James", external_id="discord:111", is_host=True)
    character = bot.create_character(
        campaign["campaign_id"], "Vale", character_class="fighter", level=2,
        external_id="discord:111",
    )
    return {"campaign": campaign, "seat": seat, "character": character}


def test_every_operation_the_spec_names_exists(bot):
    for operation in OPERATIONS:
        assert callable(getattr(bot, operation)), f"missing operation: {operation}"


def test_a_campaign_can_be_run_without_a_gui_or_a_model(bot, table):
    campaign_id = table["campaign"]["campaign_id"]

    reply = bot.handle_message("discord:111", "I search the common room for a struggle")

    assert reply["campaign_id"] == campaign_id
    assert reply["narration"]
    assert reply["rolls"]
    assert reply["explanation"]  # a player-facing reason, not model reasoning


def test_a_message_is_routed_by_chat_identity(bot, table):
    assert bot.campaign_for("discord:111") == table["campaign"]["campaign_id"]

    with pytest.raises(OpenClawError, match="not seated"):
        bot.campaign_for("discord:nobody")


def test_two_tables_never_mix_state(bot, table):
    alpha = table["campaign"]["campaign_id"]
    beta = bot.create_campaign("Party Beta", settings={"rng_seed": 6})["campaign_id"]
    bot.join(beta, "Dana", external_id="discord:222", is_host=True)
    bot.create_character(beta, "Nim", species="elf", character_class="rogue",
                         external_id="discord:222")

    bot.handle_message("discord:111", "I search the common room")
    bot.handle_message("discord:222", "I sneak toward the tower")

    alpha_state = bot.get_game_state(alpha)
    beta_state = bot.get_game_state(beta)

    assert [c["name"] for c in alpha_state["briefing"]["who"]] == ["Vale"]
    assert [c["name"] for c in beta_state["briefing"]["who"]] == ["Nim"]
    assert "sneak toward the tower" not in " ".join(alpha_state["transcript"])
    assert "search the common room" not in " ".join(beta_state["transcript"])


def test_a_player_at_two_tables_must_name_one(bot, table):
    beta = bot.create_campaign("Party Beta", settings={"rng_seed": 6})["campaign_id"]
    bot.join(beta, "James", external_id="discord:111")

    with pytest.raises(OpenClawError, match="seated at several campaigns"):
        bot.campaign_for("discord:111")

    # Naming the campaign resolves it.
    assert bot.submit_player_action(beta, "I look around", external_id="discord:111")


def test_state_is_persisted_after_every_message(bot, table, tmp_path):
    campaign_id = table["campaign"]["campaign_id"]
    bot.handle_message("discord:111", "I search the common room")

    fresh = OpenClawAdapter(CampaignStore(tmp_path / "campaigns"))
    reloaded = fresh.get_game_state(campaign_id)

    assert "search the common room" in " ".join(reloaded["transcript"])
    assert [c["name"] for c in reloaded["briefing"]["who"]] == ["Vale"]


def test_a_reopened_campaign_continues_rather_than_restarting(bot, table, tmp_path):
    campaign_id = table["campaign"]["campaign_id"]
    bot.handle_message("discord:111", "I search the common room")
    events = bot.get_game_state(campaign_id)["events"]

    fresh = OpenClawAdapter(CampaignStore(tmp_path / "campaigns"))
    fresh.handle_message("discord:111", "I look behind the bar")

    assert fresh.get_game_state(campaign_id)["events"] > events


def test_a_character_sheet_comes_back_as_plain_json(bot, table):
    campaign_id = table["campaign"]["campaign_id"]
    sheet = bot.get_character_sheet(campaign_id, table["character"]["id"])

    assert sheet["name"] == "Vale"
    assert sheet["hp"]["maximum"] > 0
    assert isinstance(sheet, dict)


def test_asking_for_a_character_that_is_not_there_is_an_error(bot, table):
    with pytest.raises(OpenClawError, match="no character"):
        bot.get_character_sheet(table["campaign"]["campaign_id"], "pc-nobody")


def test_a_private_message_reaches_only_its_seat(bot, table):
    campaign_id = table["campaign"]["campaign_id"]
    dana = bot.join(campaign_id, "Dana", external_id="discord:222")

    bot.send_private_message(campaign_id, table["seat"]["player_id"], "You see the tripwire.")

    james_view = bot.get_game_state(campaign_id, player_id=table["seat"]["player_id"])
    dana_view = bot.get_game_state(campaign_id, player_id=dana["player_id"])

    assert "You see the tripwire." in james_view["transcript"]
    assert "You see the tripwire." not in dana_view["transcript"]


def test_combat_can_be_run_headlessly(bot, table):
    campaign_id = table["campaign"]["campaign_id"]
    session = bot._session(campaign_id)
    goblins = session.spawn("goblin", 2)
    bot.store.save(session)

    combat = bot.start_combat(
        campaign_id, [table["character"]["id"], *[g.id for g in goblins]]
    )
    assert combat["active"] is True
    assert len(combat["order"]) == 3

    turn = bot.advance_turn(campaign_id)
    assert turn["current"] is not None

    ended = bot.end_combat(campaign_id, "The goblins break and run.")
    assert ended["active"] is False


def test_dice_can_be_rolled_through_the_adapter(bot, table):
    result = bot.roll_dice(table["campaign"]["campaign_id"], "1d20+3", reason="initiative")

    assert 4 <= result["total"] <= 23
    assert result["expression"] == "1d20+3"


def test_the_quest_state_hides_dm_only_objectives(bot, table):
    from dmai.engine.models import Visibility

    campaign_id = table["campaign"]["campaign_id"]
    session = bot._session(campaign_id)
    quest = session.quests.add(session.journal, "The Cinder Road", objectives=["Find it"])
    session.quests.add_objective(
        session.journal, quest.id, "Betray the guild", visibility=Visibility.DM_ONLY
    )
    bot.store.save(session)

    state = bot.get_quest_state(campaign_id)

    descriptions = [o["description"] for o in state["quests"][0]["objectives"]]
    assert descriptions == ["Find it"]


def test_an_unknown_campaign_is_a_clean_error(bot):
    with pytest.raises(OpenClawError, match="no campaign saved"):
        bot.load_campaign("camp-nothing")


def test_campaigns_can_be_listed_and_closed(bot, table):
    campaign_id = table["campaign"]["campaign_id"]

    assert [c["campaign_id"] for c in bot.list_campaigns()] == [campaign_id]

    bot.close_campaign(campaign_id)

    # Closing frees memory but loses nothing: the table reopens from its log.
    assert bot.get_game_state(campaign_id)["briefing"]["who"]


def test_resuming_tells_the_table_where_they_are(bot, table):
    campaign_id = table["campaign"]["campaign_id"]
    session = bot._session(campaign_id)
    inn = session.atlas.add_location(session.journal, "The Ash & Anchor")
    session.atlas.enter(session.journal, inn.id)
    bot.store.save(session)

    assert bot.resume(campaign_id)["campaign_id"] == campaign_id


# --- the line protocol -----------------------------------------------------
#
# What OpenClaw actually talks to: one JSON object per line in, one out. The
# properties worth defending are that a bad message never stops the bot, and
# that a chat transport cannot reach anything not on the allowlist.



def _serve(bot: OpenClawAdapter, *requests: dict | str) -> list[dict]:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in requests]
    out = io.StringIO()
    protocol.serve(bot, stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_the_protocol_describes_what_it_accepts(bot):
    described = protocol.describe()

    assert described["protocol"] == protocol.PROTOCOL_VERSION
    assert "submit_player_action" in described["operations"]
    assert protocol.MESSAGE_OP in described["shorthand"]


def test_a_campaign_can_be_created_over_the_pipe(bot):
    [reply] = _serve(bot, {"op": "create_campaign", "name": "Piped Table"})

    assert reply["ok"] is True
    assert reply["result"]["name"] == "Piped Table"


def test_arguments_may_be_nested_or_flat(bot):
    nested, flat = _serve(
        bot,
        {"op": "create_campaign", "args": {"name": "Nested"}},
        {"op": "create_campaign", "name": "Flat"},
    )

    assert nested["result"]["name"] == "Nested"
    assert flat["result"]["name"] == "Flat"


def test_a_request_id_is_echoed_so_replies_can_be_matched(bot):
    [reply] = _serve(bot, {"op": "list_campaigns", "id": "abc-123"})

    assert reply["id"] == "abc-123"


def test_message_is_shorthand_for_routing_by_chat_identity(bot, table):
    [reply] = _serve(
        bot, {"op": "message", "external_id": "discord:111", "text": "I look around"}
    )

    assert reply["ok"] is True
    assert reply["op"] == "handle_message"
    assert reply["result"]["narration"]


def test_one_bad_message_does_not_stop_the_bot(bot):
    replies = _serve(
        bot,
        "this is not json",
        {"op": "nope"},
        {"op": "create_campaign", "name": "Still Serving"},
    )

    assert [r["ok"] for r in replies] == [False, False, True]
    assert replies[-1]["result"]["name"] == "Still Serving"


def test_an_engine_error_comes_back_as_a_reply_not_a_crash(bot):
    [reply] = _serve(bot, {"op": "get_character_sheet", "campaign_id": "camp-x", "character_id": "y"})

    assert reply["ok"] is False
    assert "no campaign saved" in reply["error"]


def test_wrong_arguments_are_reported_against_the_operation(bot):
    [reply] = _serve(bot, {"op": "roll_dice", "campaign_id": "camp-x"})

    assert reply["ok"] is False
    assert reply["error"].startswith("roll_dice:")


def test_a_transport_cannot_reach_past_the_allowlist(bot, table):
    """The caller is carrying text from strangers; dispatch is not getattr."""
    for hostile in ("_session", "_agent", "store", "provider", "__class__"):
        [reply] = _serve(bot, {"op": hostile})
        assert reply["ok"] is False
        assert "unknown operation" in reply["error"]


def test_dispatch_refuses_an_operation_that_is_not_listed(bot):
    with pytest.raises(OpenClawError, match="unknown operation"):
        bot.dispatch("close_campaign_and_delete_everything", {})


def test_blank_lines_are_ignored(bot):
    replies = _serve(bot, "", "   ", {"op": "list_campaigns"})

    assert len(replies) == 1


def test_a_json_array_is_not_a_request(bot):
    [reply] = _serve(bot, "[1, 2, 3]")

    assert reply["ok"] is False
    assert "must be a JSON object" in reply["error"]


def test_every_reply_is_one_line_of_json(bot, table):
    """A pipe reader splits on newlines, so a reply must never contain one."""
    out = io.StringIO()
    protocol.serve(
        bot,
        stdin=io.StringIO(
            json.dumps({"op": "message", "external_id": "discord:111", "text": "I search the room"})
            + "\n"
        ),
        stdout=out,
    )

    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["ok"] is True
