"""The SDK's public surface.

These tests are written the way a host application would use the package: only
through what `dmai_sdk` exports.  Nothing here reaches into `table.session`,
because the point of the suite is to pin the promise, not the implementation.
"""

from __future__ import annotations

import json

import pytest

from dmai_sdk import Entry, Hero, Mark, Roll, Table, TableError, Turn


@pytest.fixture
def table() -> Table:
    """A seeded table with one hero -- deterministic, so assertions can be exact."""
    table = Table.new("Ashes of Emberfall", premise="A caravan vanished.", seed=1234)
    table.add_hero("Vale", cls="fighter", level=2)
    return table


# --- opening ---------------------------------------------------------------


def test_a_new_table_starts_empty_but_real():
    table = Table.new("Ashes of Emberfall")
    assert table.name == "Ashes of Emberfall"
    assert table.heroes == []
    assert table.events > 0  # the campaign's own creation is on the record


def test_a_campaign_needs_a_name():
    with pytest.raises(TableError, match="needs a name"):
        Table.new("   ")


def test_an_unknown_tone_says_what_is_allowed():
    with pytest.raises(TableError, match="unknown tone"):
        Table.new("Ashes", tone="baroque")


def test_a_seeded_table_is_reproducible():
    """The same seed and the same inputs give the same game."""
    def play() -> list[int]:
        table = Table.new("Ashes of Emberfall", seed=99)
        table.add_hero("Vale", cls="fighter", level=2)
        return [table.roll("d20").total for _ in range(6)]

    assert play() == play()


def test_different_seeds_diverge():
    def play(seed: int) -> list[int]:
        table = Table.new("Ashes", seed=seed)
        table.add_hero("Vale")
        return [table.roll("d20").total for _ in range(8)]

    assert play(1) != play(2)


# --- the party -------------------------------------------------------------


def test_a_hero_joins_the_party(table):
    party = table.heroes
    assert len(party) == 1

    vale = party[0]
    assert isinstance(vale, Hero)
    assert vale.name == "Vale"
    assert vale.level == 2
    # The rules pack's display name, not the key that was passed in.
    assert vale.character_class == "Fighter"
    assert vale.species == "Human"
    assert vale.hp == vale.max_hp
    assert not vale.bloodied


def test_a_hero_needs_a_name(table):
    with pytest.raises(TableError, match="needs a name"):
        table.add_hero("")


def test_a_hero_needs_a_real_level(table):
    with pytest.raises(TableError, match="level must be 1 or more"):
        table.add_hero("Vale", level=0)


def test_an_unknown_class_is_refused_clearly(table):
    with pytest.raises(TableError, match="could not create"):
        table.add_hero("Mordak", cls="necromancer")


def test_the_first_hero_takes_the_seat(table):
    assert table.hero is not None
    assert table.hero.name == "Vale"


def test_the_seat_can_move_by_name_or_by_hero(table):
    rook = table.add_hero("Rook", cls="rogue")

    assert table.play_as("Rook").name == "Rook"
    assert table.hero.name == "Rook"

    assert table.play_as(rook).id == rook.id
    with pytest.raises(TableError, match="not in the party"):
        table.play_as("Nobody")


# --- playing ---------------------------------------------------------------


def test_an_action_returns_a_narrated_turn(table):
    turn = table.act("I search the ruts for tracks")

    assert isinstance(turn, Turn)
    assert turn.text == "I search the ruts for tracks"
    assert turn.narration
    assert turn.intent  # the DM decided what kind of action this was


def test_an_action_puts_the_players_line_in_the_record_once(table):
    table.act("I search the ruts for tracks")

    actions = [e for e in table.chronicle() if e.kind == "action"]
    assert len(actions) == 1
    assert actions[0].text.endswith("I search the ruts for tracks")


def test_an_empty_action_is_refused(table):
    with pytest.raises(TableError, match="say what you do"):
        table.act("   ")


def test_acting_with_an_empty_party_is_refused():
    table = Table.new("Ashes of Emberfall", seed=1)
    with pytest.raises(TableError, match="party is empty"):
        table.act("I look around")


def test_a_roll_reports_its_parts(table):
    roll = table.roll("2d6+3", reason="Foraging")

    assert isinstance(roll, Roll)
    assert roll.modifier == 3
    assert len(roll.dice) == 2
    assert roll.total == sum(roll.dice) + 3
    assert "Foraging" in str(roll)


def test_a_nonsense_roll_is_refused(table):
    with pytest.raises(TableError):
        table.roll("two coins")


def test_narration_goes_into_the_record(table):
    entry = table.narrate("Smoke hangs over the Cinder Road.")

    assert isinstance(entry, Entry)
    assert entry.kind == "narration"
    assert entry.text in [e.text for e in table.chronicle()]


def test_empty_narration_is_refused(table):
    with pytest.raises(TableError, match="nothing to narrate"):
        table.narrate("  ")


# --- reading the table -----------------------------------------------------


def test_the_scene_describes_where_the_party_is(table):
    scene = table.scene()
    assert scene.location
    assert scene.time
    assert scene.in_combat is False


def test_the_chronicle_can_be_limited(table):
    for index in range(8):
        table.narrate(f"Line {index}.")

    assert len(table.chronicle(limit=3)) == 3
    assert table.chronicle(limit=3)[-1].text == "Line 7."


def test_a_table_is_iterable_and_sized(table):
    table.narrate("Smoke hangs over the Cinder Road.")
    assert len(table) == table.events
    assert any(entry.text.startswith("Smoke") for entry in table)


def test_the_recap_summarises_the_campaign(table):
    table.act("I search the ruts for tracks")
    recap = table.recap()
    assert isinstance(recap, str) and recap


def test_the_briefing_is_plain_data(table):
    briefing = table.briefing()
    json.dumps(briefing)  # raises if a model leaked through
    assert "who" in briefing and "where" in briefing


def test_repr_says_what_the_table_is(table):
    assert "Ashes of Emberfall" in repr(table)
    assert "1 in the party" in repr(table)


# --- marks and rewinding ---------------------------------------------------


def test_a_mark_can_be_rewound_to(table):
    table.narrate("Before the treeline.")
    mark = table.mark("Before the treeline")
    assert isinstance(mark, Mark)

    table.narrate("Into the trees.")
    assert "Into the trees." in [e.text for e in table.chronicle()]

    table.rewind(mark)
    texts = [e.text for e in table.chronicle()]
    assert "Into the trees." not in texts
    assert "Before the treeline." in texts


def test_marks_are_listed(table):
    table.mark("Before the treeline")
    assert "Before the treeline" in [m.label for m in table.marks]


def test_rewinding_past_the_beginning_is_refused(table):
    with pytest.raises(TableError, match="past the beginning"):
        table.rewind(-1)


# --- saving ----------------------------------------------------------------


def test_a_save_round_trips_through_the_log(table, tmp_path):
    table.act("I search the ruts for tracks")
    table.narrate("Smoke hangs over the Cinder Road.")
    before = [e.text for e in table.chronicle()]

    path = table.save(tmp_path / "emberfall.dmai")
    reopened = Table.load(path)

    assert reopened.name == table.name
    assert [e.text for e in reopened.chronicle()] == before
    assert [h.name for h in reopened.heroes] == [h.name for h in table.heroes]


def test_a_reopened_table_keeps_playing(table, tmp_path):
    path = table.save(tmp_path / "emberfall.dmai")
    reopened = Table.load(path)

    reopened.play_as("Vale")
    turn = reopened.act("I search the ruts for tracks")
    assert turn.narration


def test_saving_creates_missing_directories(table, tmp_path):
    path = table.save(tmp_path / "saves" / "deep" / "emberfall.dmai")
    assert path.is_file()


def test_a_missing_save_says_so(tmp_path):
    with pytest.raises(TableError, match="no save at"):
        Table.load(tmp_path / "nothing.dmai")


def test_a_file_that_is_not_a_save_is_refused(tmp_path):
    junk = tmp_path / "notes.json"
    junk.write_text('{"hello": "world"}', encoding="utf-8")

    with pytest.raises(TableError, match="not a DungeonMaster campaign save"):
        Table.load(junk)


def test_a_damaged_save_is_refused(tmp_path):
    broken = tmp_path / "broken.dmai"
    broken.write_text('{"format": "dmai-campaign", "campaign": {}}', encoding="utf-8")

    with pytest.raises(TableError, match="damaged"):
        Table.load(broken)


def test_the_save_is_the_format_the_cli_imports(table, tmp_path):
    """An SDK save must open in the CLI, and vice versa."""
    path = table.save(tmp_path / "emberfall.dmai")
    bundle = json.loads(path.read_text(encoding="utf-8"))

    assert bundle["format"] == "dmai-campaign"
    assert "campaign" in bundle and "events" in bundle
    assert bundle["events"], "a save with no events would replay to nothing"
