"""The desktop client, tested without a window.

`DesktopBridge` is deliberately free of pywebview, so everything the window can
do is exercised here headlessly.  What these tests are really protecting is the
bridge's contract with the page:

* every operation answers `{"ok": ...}` and never raises across the boundary;
* `view()` is a complete redraw payload, so the UI never has to patch a panel;
* the player's line lands in the chronicle exactly once.
"""

from __future__ import annotations

import json

import pytest

from dmai.desktop.bridge import CHRONICLE_LIMIT, DesktopBridge


@pytest.fixture
def bridge(tmp_path):
    return DesktopBridge(tmp_path)


@pytest.fixture
def table(bridge):
    """A bridge with a campaign open and one character in the party."""
    bridge.create_campaign(
        {
            "name": "Ashes of Emberfall",
            "premise": "A caravan vanished on the Cinder Road.",
            "player": "James",
            "seed": 1234,
        }
    )
    bridge.add_character({"name": "Vale", "character_class": "fighter", "level": 2})
    return bridge


# --- the contract ----------------------------------------------------------


def test_every_operation_answers_rather_than_raising(bridge):
    """A failure is a message the UI can show, never an exception."""
    result = bridge.open_campaign("no-such-campaign")
    assert result["ok"] is False
    assert "no campaign matching" in result["error"]


def test_operations_on_a_closed_table_say_so(bridge):
    for call in (bridge.view(), bridge.act("I look around"), bridge.recap()):
        assert call["ok"] is False
        assert call["error"] == "no campaign is open"


def test_results_are_json_serialisable(table):
    """Everything crossing the bridge must survive the webview boundary."""
    for payload in (table.view(), table.options(), table.list_campaigns()):
        json.dumps(payload)  # raises if a pydantic model or a Path leaked out


# --- the library -----------------------------------------------------------


def test_a_new_campaign_is_saved_and_listed(bridge):
    created = bridge.create_campaign({"name": "Ashes of Emberfall"})
    assert created["ok"] is True

    listed = bridge.list_campaigns()["campaigns"]
    assert [c["name"] for c in listed] == ["Ashes of Emberfall"]
    assert listed[0]["id"] == created["campaign_id"]


def test_a_campaign_needs_a_name(bridge):
    assert bridge.create_campaign({"name": "   "})["error"] == "a campaign needs a name"


def test_a_campaign_opens_by_name_as_well_as_by_id(bridge):
    bridge.create_campaign({"name": "Ashes of Emberfall"})
    bridge.close_campaign()

    assert bridge.open_campaign("ashes of emberfall")["ok"] is True
    assert bridge.view()["campaign"]["name"] == "Ashes of Emberfall"


def test_an_ambiguous_name_is_refused_rather_than_guessed(bridge):
    bridge.create_campaign({"name": "Ashes of Emberfall"})
    bridge.create_campaign({"name": "Ashes of Winter"})
    bridge.close_campaign()

    result = bridge.open_campaign("Ashes")
    assert result["ok"] is False
    assert "matches several campaigns" in result["error"]


def test_a_campaign_survives_being_closed_and_reopened(table):
    table.act("I search the common room")
    campaign_id = table.view()["campaign"]["id"]
    events = table.view()["campaign"]["events"]
    table.close_campaign()

    reopened = DesktopBridge(table.store.root)
    reopened.open_campaign(campaign_id)
    view = reopened.view()

    assert view["campaign"]["events"] >= events
    assert [c["name"] for c in view["party"]] == ["Vale"]


def test_deleting_a_campaign_removes_it_from_the_shelf(bridge):
    created = bridge.create_campaign({"name": "Ashes of Emberfall"})
    bridge.delete_campaign(created["campaign_id"])
    assert bridge.list_campaigns()["campaigns"] == []


def test_export_and_import_round_trip(table, tmp_path):
    campaign_id = table.view()["campaign"]["id"]
    destination = tmp_path / "bundle.json"

    assert table.export_campaign(campaign_id, str(destination))["ok"] is True
    assert destination.is_file()

    imported = table.import_campaign(str(destination))
    assert imported["ok"] is True
    assert imported["name"] == "Ashes of Emberfall"
    # The campaign is already here, so it is imported alongside rather than
    # over the top of the save the player already has.
    assert imported["renamed"] is True
    assert imported["campaign_id"] != campaign_id
    assert len(table.list_campaigns()["campaigns"]) == 2


def test_importing_into_a_fresh_store_keeps_the_original_id(table, tmp_path):
    """Restoring your own export should give you the campaign back, not a copy."""
    campaign_id = table.view()["campaign"]["id"]
    bundle = tmp_path / "bundle.json"
    table.export_campaign(campaign_id, str(bundle))

    elsewhere = DesktopBridge(tmp_path / "other-store")
    imported = elsewhere.import_campaign(str(bundle))

    assert imported["campaign_id"] == campaign_id
    assert imported["renamed"] is False


def test_importing_something_that_is_not_a_bundle_is_refused(bridge, tmp_path):
    junk = tmp_path / "notes.json"
    junk.write_text('{"hello": "world"}', encoding="utf-8")

    result = bridge.import_campaign(str(junk))
    assert result["ok"] is False
    assert "not a DungeonMaster AI campaign bundle" in result["error"]


# --- the table -------------------------------------------------------------


def test_the_creation_form_offers_the_real_rules_pack(bridge):
    options = bridge.options()
    assert "fighter" in [c["key"] for c in options["classes"]]
    assert "half-orc" in [s["key"] for s in options["species"]]
    assert "srd51" in options["rules"]
    assert "offline" in options["providers"]


def test_a_character_joins_the_party_and_becomes_the_active_seat(bridge):
    bridge.create_campaign({"name": "Ashes of Emberfall", "player": "James"})
    added = bridge.add_character({"name": "Vale", "character_class": "fighter"})

    view = bridge.view()
    assert [c["name"] for c in view["party"]] == ["Vale"]
    assert view["active_character_id"] == added["character_id"]
    assert view["party"][0]["player"] == "James"


def test_a_character_needs_a_name(table):
    assert table.add_character({"name": ""})["error"] == "a character needs a name"


def test_the_player_line_is_recorded_exactly_once(table):
    """`take_turn` logs the action itself, so the bridge must not log it too."""
    table.act("I search the ruts for tracks")

    actions = [e for e in table.view()["chronicle"] if e["kind"] == "action"]
    assert len(actions) == 1
    assert actions[0]["text"].endswith("I search the ruts for tracks")


def test_an_action_returns_its_rolls_and_diagnostics(table):
    result = table.act("I search the ruts for tracks")

    assert result["ok"] is True
    assert result["diagnostics"]["provider_id"] == "offline"
    # The offline DM resolves a check, so the turn produced a real roll.
    assert all("total" in roll for roll in result["rolls"])


def test_an_empty_action_is_refused(table):
    assert table.act("   ")["error"] == "say what you do"


def test_playing_as_another_character_moves_the_seat(table):
    second = table.add_character({"name": "Rook", "character_class": "rogue"})
    assert table.play_as(second["character_id"])["ok"] is True
    assert table.view()["active_character_id"] == second["character_id"]


def test_playing_as_someone_outside_the_party_is_refused(table):
    assert table.play_as("creature-nobody")["error"] == "that character is not in the party"


def test_a_roll_is_logged_where_the_table_can_see_it(table):
    result = table.roll("2d6+3")

    assert result["ok"] is True
    assert 5 <= result["total"] <= 15
    assert any(e["kind"] == "roll" for e in table.view()["chronicle"])


def test_narration_goes_into_the_record(table):
    table.narrate("Smoke hangs over the Cinder Road.")
    narrations = [e for e in table.view()["chronicle"] if e["kind"] == "narration"]
    assert "Smoke hangs over the Cinder Road." in [e["text"] for e in narrations]


def test_empty_narration_is_refused(table):
    assert table.narrate("  ")["error"] == "nothing to narrate"


# --- the view --------------------------------------------------------------


def test_the_view_carries_every_panel(table):
    view = table.view()
    for key in (
        "campaign", "party", "chronicle", "combat",
        "quests", "where", "when", "dm", "checkpoints",
    ):
        assert key in view, f"the window cannot draw without {key!r}"


def test_hit_points_carry_a_fraction_the_wound_track_can_use(table):
    hero = table.view()["party"][0]
    assert hero["hp"]["fraction"] == 1.0
    assert hero["hp"]["current"] == hero["hp"]["maximum"]


def test_the_chronicle_is_capped_so_the_dom_stays_small(table):
    for index in range(12):
        table.narrate(f"Line {index}.")
    assert len(table.view()["chronicle"]) <= CHRONICLE_LIMIT


def test_combat_is_absent_until_a_fight_starts(table):
    combat = table.view()["combat"]
    assert combat["active"] is False
    assert combat["order"] == []


def test_a_sheet_reports_ability_modifiers(table):
    character_id = table.view()["party"][0]["id"]
    sheet = table.sheet(character_id)["sheet"]

    assert set(sheet["abilities"]) == {
        "strength", "dexterity", "constitution",
        "intelligence", "wisdom", "charisma",
    }
    for ability in sheet["abilities"].values():
        assert ability["modifier"] == (ability["score"] - 10) // 2


def test_an_unknown_sheet_is_refused(table):
    assert table.sheet("creature-nobody")["error"] == "no such character"


# --- checkpoints -----------------------------------------------------------


def test_a_mark_can_be_set_and_rewound_to(table):
    table.narrate("Before the treeline.")
    mark = table.checkpoint("Before the treeline")
    assert mark["ok"] is True

    table.narrate("Into the trees.")
    assert "Into the trees." in [e["text"] for e in table.view()["chronicle"]]

    assert table.rollback(mark["seq"])["ok"] is True
    texts = [e["text"] for e in table.view()["chronicle"]]
    assert "Into the trees." not in texts
    assert "Before the treeline." in texts


def test_marks_are_listed_in_the_view(table):
    table.checkpoint("Before the treeline")
    labels = [m["label"] for m in table.view()["checkpoints"]]
    assert "Before the treeline" in labels


# --- the recap -------------------------------------------------------------


def test_the_recap_summarises_this_sitting(table):
    table.act("I search the ruts for tracks")
    recap = table.recap()["recap"]

    assert recap["campaign"] == "Ashes of Emberfall"
    assert isinstance(recap["narrative"], str) and recap["narrative"]
