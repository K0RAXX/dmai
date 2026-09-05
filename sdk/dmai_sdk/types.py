"""The values the SDK hands back.

Plain frozen dataclasses on purpose.  The engine speaks pydantic, but an SDK's
public surface is a promise, and a promise made of another library's models is
a promise about that library's version too.  These types are flat, printable,
and cheap to assert on in someone else's test suite.

Nothing here is constructed by hand from outside: `Table` builds them from
engine state, so a value in one of these fields came from the event log.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Roll:
    """One resolved roll: what was rolled, and what it came to."""

    expression: str
    total: int
    #: The individual dice faces, before modifiers.
    dice: tuple[int, ...] = ()
    modifier: int = 0
    reason: str = ""
    #: Set when the roll was made against a difficulty class.
    dc: int | None = None
    success: bool | None = None

    def __str__(self) -> str:
        head = f"{self.expression} -> {self.total}"
        if self.reason:
            head = f"{self.reason}: {head}"
        if self.dc is None:
            return head
        return f"{head} vs DC {self.dc} ({'success' if self.success else 'failure'})"

    __repr__ = __str__


@dataclass(frozen=True)
class Hero:
    """A member of the party, as the outside world needs to see them."""

    id: str
    name: str
    level: int
    #: The rules pack's display names ("Half-Orc", "Fighter"), not the lowercase
    #: keys `add_hero` takes -- these are meant to be shown, not matched on.
    species: str
    character_class: str
    hp: int
    max_hp: int
    armor_class: int
    speed: int
    conditions: tuple[str, ...] = ()
    dead: bool = False

    @property
    def bloodied(self) -> bool:
        """Below half hit points -- the usual threshold for "in trouble"."""
        return self.hp * 2 <= self.max_hp

    def __str__(self) -> str:
        return (
            f"{self.name}, level {self.level} {self.species} {self.character_class} "
            f"({self.hp}/{self.max_hp} hp, AC {self.armor_class})"
        )

    __repr__ = __str__


@dataclass(frozen=True)
class Entry:
    """One line of the record, as the log wrote it."""

    seq: int
    kind: str
    text: str

    def __str__(self) -> str:
        return self.text

    __repr__ = __str__


@dataclass(frozen=True)
class Turn:
    """The result of one player action.

    `narration` is prose; `rolls` and `events` are what the engine actually
    resolved.  The narration is generated *from* those facts, so if the two
    ever disagree the facts are the ones to trust.
    """

    text: str
    narration: str
    rolls: tuple[Roll, ...] = ()
    events: tuple[Entry, ...] = ()
    #: What the DM understood the player to be doing.
    intent: str = ""
    rationale: str = ""

    def __str__(self) -> str:
        return self.narration or self.text

    __repr__ = __str__


@dataclass(frozen=True)
class Scene:
    """Where the party is, and what is going on around them."""

    location: str
    description: str = ""
    time: str = ""
    weather: str = ""
    npcs: tuple[str, ...] = ()
    exits: tuple[str, ...] = ()
    in_combat: bool = False
    combat_round: int = 0

    def __str__(self) -> str:
        parts = [self.location]
        if self.time:
            parts.append(self.time)
        if self.in_combat:
            parts.append(f"round {self.combat_round}")
        return " -- ".join(parts)

    __repr__ = __str__


@dataclass(frozen=True)
class Mark:
    """A point in the record that `Table.rewind` can return to."""

    seq: int
    label: str
    automatic: bool = False

    def __str__(self) -> str:
        return f"{self.label or 'unmarked'} (event {self.seq})"

    __repr__ = __str__


@dataclass(frozen=True)
class Quest:
    """One quest and its objectives."""

    title: str
    summary: str = ""
    status: str = "active"
    objectives: tuple[tuple[str, bool], ...] = field(default_factory=tuple)

    def __str__(self) -> str:
        return f"{self.title} [{self.status}]"

    __repr__ = __str__


__all__ = ["Entry", "Hero", "Mark", "Quest", "Roll", "Scene", "Turn"]
