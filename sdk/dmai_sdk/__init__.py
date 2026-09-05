"""DungeonMaster SDK -- an offline D&D engine you can embed.

A complete tabletop RPG engine: dice, characters, skill checks, combat with
monster tactics, inventory, encounter budgets, quests and consequences, a world
that moves on its own, and a Dungeon Master that ties them together.  All of it
runs in-process, with no network, no API key and no model.

    from dmai_sdk import Table

    table = Table.new("Ashes of Emberfall", seed=1234)
    table.add_hero("Vale", cls="fighter", level=2)

    turn = table.act("I search the room")
    print(turn.narration)
    print(turn.rolls)

    table.save("emberfall.dmai")

Seeded tables are deterministic, which makes this usable as a test fixture as
well as a game.  See README.md for the whole surface.
"""

from __future__ import annotations

from .errors import TableError
from .table import Table
from .types import Entry, Hero, Mark, Quest, Roll, Scene, Turn

__version__ = "0.1.0"

__all__ = [
    "Entry",
    "Hero",
    "Mark",
    "Quest",
    "Roll",
    "Scene",
    "Table",
    "TableError",
    "Turn",
    "__version__",
]
