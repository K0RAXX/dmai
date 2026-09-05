# DungeonMaster SDK

An offline D&D engine you can embed. Dice, characters, skill checks, combat with
monster tactics, inventory, encounter budgets, quests and consequences, a world
that moves on its own, and a Dungeon Master that ties them together — all of it
in-process, with **no network, no API key and no model**.

```python
from dmai_sdk import Table

table = Table.new("Ashes of Emberfall", seed=1234)
table.add_hero("Vale", cls="fighter", level=2)

turn = table.act("I search the room")
print(turn.narration)      # 'investigation check: 14 -- against DC 13 -- success.'
print(turn.rolls)          # [investigation check: 1d20-1 -> 14 vs DC 13 (success)]

table.save("emberfall.dmai")
```

## Install

```bash
pip install dmai-sdk
```

Python 3.11+. The only dependencies are `pydantic` and the `dmai` engine.

## The offline guarantee

This is the package's whole reason to exist, so it is tested as behaviour
rather than asserted in a README: `tests/test_offline.py` replaces
`socket.socket` with a trap and plays a full campaign — creating heroes, taking
turns, rolling, marking, rewinding, saving and reloading — through it. A single
outbound connection anywhere beneath `Table` fails the build. A second test
walks every module's AST and rejects an import of anything that could leave the
process, so a network call on an error path cannot hide from the happy path.

There is no provider argument, no model id and no key. The DM is always the
offline one: it interprets by keyword and narrates by restating what the engine
resolved. What you give up is prose quality. What you get is a D&D engine that
runs in a sealed container, on a plane, in CI, deterministically when seeded, at
no cost per turn.

## Determinism

A seeded table fixes every die it will ever roll, so the same seed and the same
inputs give the same game:

```python
def play():
    table = Table.new("Ashes", seed=99)
    table.add_hero("Vale", cls="fighter", level=2)
    return [table.roll("d20").total for _ in range(6)]

assert play() == play()
```

That makes the SDK usable as a test fixture, not only as a game.

## The surface

### Opening a table

| | |
|---|---|
| `Table.new(name, *, premise, setting, tone, rules, seed, sandbox)` | Found a campaign. |
| `Table.load(path)` | Reopen a save, by replaying its log. |
| `table.save(path)` | Write the campaign to one file. |

### The party

| | |
|---|---|
| `table.add_hero(name, *, cls, species, level, player)` | Roll up a character. |
| `table.heroes` | The party, as `Hero` values. |
| `table.hero` | Whoever `act()` speaks for. |
| `table.play_as(hero)` | Move that seat, by name, id or `Hero`. |

### Playing

| | |
|---|---|
| `table.act(text)` | One action, through the DM's full loop. Returns a `Turn`. |
| `table.roll(expr, *, reason)` | Roll dice into the record. Returns a `Roll`. |
| `table.narrate(text)` | Put a line in the record as the DM. |
| `table.scene()` | Where the party is, and what is going on. |
| `table.quests` | Quests and their objectives. |

### The record

| | |
|---|---|
| `table.chronicle(limit=None)` | The narrative line of the log. |
| `table.recap()` | A plain summary, assembled from the log. |
| `table.briefing()` | Everything needed to resume, as plain data. |
| `table.events`, `len(table)`, `iter(table)` | Size and iteration. |

### Rewinding

| | |
|---|---|
| `table.mark(label)` | Set a point to come back to. |
| `table.marks` | Every mark, oldest first. |
| `table.rewind(mark)` | Undo everything after it. |

## How state works

Nothing edits game state directly. Every change is an event appended to a log,
and the state is the fold of that log. Three consequences a host application can
rely on:

- **A save cannot disagree with its own history.** `save()` writes only the log,
  and `load()` replays it. There is no snapshot to drift.
- **Rewinding is truncation, not restoration.** `rewind()` returns the campaign
  as it actually was, because the events after the mark stop existing.
- **Results are absolute, not relative.** A mutating event carries `hp_current: 4`,
  never `damage: 3`, which is what makes replaying one twice safe.

## Errors

Everything a caller can get wrong raises `TableError` with a message written for
a person:

```python
from dmai_sdk import Table, TableError

try:
    table.add_hero("Mordak", cls="necromancer")
except TableError as exc:
    print(exc)   # could not create 'Mordak': unknown class for srd51: necromancer
```

Engine exceptions are translated at the boundary rather than allowed through, so
you catch one type rather than several from packages you never imported.

## Saves are interchangeable

`table.save()` writes the same `dmai-campaign` bundle the command-line tool
produces, so a save made here opens with `dmai import`, and an export made there
opens with `Table.load`.

## Running the tests

```bash
pip install -e ".[dev]"
python -m pytest
```

## Licence

Rules content comes from the System Reference Document 5.1 by Wizards of the
Coast LLC, used under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/legalcode).
