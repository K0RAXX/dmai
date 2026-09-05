"""`Table` -- the whole SDK in one object.

Everything a host application needs to run a campaign, and nothing it does not.
The engine underneath is the same one the desktop client and the CLI drive; what
this module adds is a small, stable surface and one guarantee:

    **No network, ever.**

The DM is always `OfflineProvider`: it interprets by keyword and narrates by
restating what the engine resolved.  There is no provider argument, no model id
and no API key, because an SDK that *could* be pointed at a model is an SDK that
has to be audited for whether it was.  Nothing in this package imports a network
module -- `tests/test_offline.py` fails the build if that ever stops being true.

What you give up is prose quality.  What you get is a D&D engine that runs in a
sealed container, on a plane, inside someone else's test suite, deterministically
when seeded, at no cost per turn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from dmai.ai.dm_agent.agent import DungeonMaster
from dmai.ai.providers.offline import OfflineProvider
from dmai.engine.models.actions import PlayerAction
from dmai.engine.models.campaign import Campaign, CampaignSettings, StoryTone
from dmai.engine.models.dice import DiceResult, Outcome
from dmai.engine.models.events import GameEvent
from dmai.engine.quests.tracker import QuestStatus
from dmai.engine.session import GameSession
from dmai.persistence import CURRENT_SAVE_VERSION

from .errors import TableError
from .types import Entry, Hero, Mark, Quest, Roll, Scene, Turn

#: The bundle format `save()` writes.  Deliberately the same one
#: `CampaignStore.export` produces, so an SDK save can be opened with
#: `dmai import` and a CLI export can be opened with `Table.load`.
BUNDLE_FORMAT = "dmai-campaign"

#: How the chronicle labels an event, for callers that want to filter it.
ENTRY_KINDS = {
    "dm_narration": "narration",
    "player_action": "action",
    "npc_dialogue": "dialogue",
    "dice_rolled": "roll",
    "check_resolved": "roll",
    "save_resolved": "roll",
    "attack": "combat",
    "damage_dealt": "combat",
    "healing": "heal",
    "creature_died": "death",
    "item_gained": "loot",
    "quest_added": "quest",
    "quest_updated": "quest",
    "objective_completed": "quest",
    "discovery": "discovery",
    "location_entered": "travel",
    "checkpoint": "mark",
}


class Table:
    """One campaign, played offline.

    >>> table = Table.new("Ashes of Emberfall", seed=1234)
    >>> vale = table.add_hero("Vale", cls="fighter", level=2)
    >>> turn = table.act("I search the room")
    >>> turn.narration
    'investigation check: ...'
    """

    def __init__(self, session: GameSession):
        #: The engine session.  Public because an advanced caller may want the
        #: full engine; using it is supported, but nothing here depends on how.
        self.session = session
        self._dm = DungeonMaster(session, OfflineProvider())
        self._hero_id: str | None = None
        self._player_id: str | None = None

    # --- opening a table ---------------------------------------------------

    @classmethod
    def new(
        cls,
        name: str,
        *,
        premise: str = "",
        setting: str = "",
        tone: str = "heroic",
        rules: str = "srd51",
        seed: int | None = None,
        sandbox: bool = False,
    ) -> Table:
        """Found a campaign.

        `seed` fixes every die the campaign will ever roll, which is what makes
        a run reproducible -- the same seed and the same inputs give the same
        game, so this is usable as a fixture.
        """
        if not name or not name.strip():
            raise TableError("a campaign needs a name")
        try:
            story_tone = StoryTone(tone)
        except ValueError:
            allowed = ", ".join(t.value for t in StoryTone)
            raise TableError(f"unknown tone {tone!r}; expected one of: {allowed}") from None

        campaign = Campaign(
            name=name.strip(),
            premise=premise.strip(),
            setting=setting.strip(),
            settings=CampaignSettings(
                tone=story_tone,
                rules_pack=rules,
                rng_seed=seed,
                sandbox_mode=sandbox,
                # Recorded so a save opened by the CLI also opens offline.
                ai_provider="offline",
            ),
        )
        try:
            return cls(GameSession.create(campaign))
        except Exception as exc:  # a bad rules pack is the likely cause
            raise TableError(str(exc)) from exc

    @classmethod
    def load(cls, path: str | Path) -> Table:
        """Reopen a campaign from a bundle written by `save()`.

        The campaign is rebuilt by replaying its log, not by trusting a stored
        snapshot, so a loaded table is exactly the table that was saved.
        """
        source = Path(path)
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise TableError(f"no save at {source}") from None
        except (OSError, ValueError) as exc:
            raise TableError(f"{source} could not be read: {exc}") from exc

        if not isinstance(payload, dict) or payload.get("format") != BUNDLE_FORMAT:
            raise TableError(f"{source} is not a DungeonMaster campaign save")

        try:
            campaign = Campaign.model_validate(payload["campaign"])
            events = [GameEvent.model_validate(e) for e in payload.get("events", [])]
        except (KeyError, ValueError) as exc:
            raise TableError(f"{source} is damaged: {exc}") from exc

        return cls(GameSession.restore(campaign, events))

    def save(self, path: str | Path) -> Path:
        """Write the campaign to one file.

        Only the log is written.  A save can therefore never disagree with its
        own history, and the file is the same bundle format the CLI imports.
        """
        destination = Path(path)
        bundle = {
            "format": BUNDLE_FORMAT,
            "save_version": CURRENT_SAVE_VERSION,
            "campaign": self.session.campaign.model_dump(mode="json"),
            "events": [e.model_dump(mode="json") for e in self.session.store],
        }
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Written beside the target and moved, so an interrupted save
            # leaves the previous file intact rather than a half-written one.
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
            temporary.replace(destination)
        except OSError as exc:
            raise TableError(f"could not save to {destination}: {exc}") from exc
        return destination

    # --- the party ---------------------------------------------------------

    def add_hero(
        self,
        name: str,
        *,
        cls: str = "fighter",
        species: str = "human",
        level: int = 1,
        player: str = "",
    ) -> Hero:
        """Roll up a character and put them in the party.

        The first hero added becomes the one `act()` speaks for; `play_as`
        moves that seat.
        """
        if not name or not name.strip():
            raise TableError("a hero needs a name")
        if level < 1:
            raise TableError("level must be 1 or more")

        player_id = None
        if player.strip():
            seat = next(
                (
                    p
                    for p in self.session.state.players.values()
                    if p.name.lower() == player.strip().lower()
                ),
                None,
            )
            player_id = (
                seat
                or self.session.add_player(
                    player.strip(), is_host=not self.session.state.players
                )
            ).id

        try:
            character = self.session.add_character(
                name.strip(), species, cls, level, player_id=player_id
            )
        except (KeyError, ValueError) as exc:
            raise TableError(f"could not create {name!r}: {exc}") from exc

        if self._hero_id is None:
            self._hero_id = character.id
            self._player_id = player_id
        return _hero(character)

    @property
    def heroes(self) -> list[Hero]:
        """The party, in the order they joined."""
        return [_hero(c) for c in self.session.state.party.values()]

    @property
    def hero(self) -> Hero | None:
        """Whoever `act()` currently speaks for."""
        character = self.session.state.party.get(self._hero_id or "")
        return _hero(character) if character else None

    def play_as(self, hero: str | Hero) -> Hero:
        """Move the seat `act()` speaks from, by id or by name."""
        wanted = hero.id if isinstance(hero, Hero) else hero
        party = self.session.state.party

        character = party.get(wanted) or next(
            (c for c in party.values() if c.name.lower() == str(wanted).lower()), None
        )
        if character is None:
            raise TableError(f"{wanted!r} is not in the party")

        self._hero_id = character.id
        self._player_id = character.player_id
        return _hero(character)

    # --- playing -----------------------------------------------------------

    def act(self, text: str) -> Turn:
        """Take one action, through the DM's full reasoning loop.

        Interpret, resolve in the engine, narrate the result.  The narration is
        built from the events the engine appended, so it describes the log
        rather than authoring it.
        """
        if not text or not text.strip():
            raise TableError("say what you do")
        if self._hero_id is None and not self.session.state.party:
            raise TableError("the party is empty -- add a hero before acting")

        before = self.session.store.head
        action = PlayerAction(
            campaign_id=self.session.campaign.id,
            player_id=self._player_id,
            character_id=self._hero_id,
            text=text.strip(),
        )
        result = self._dm.take_turn(action)
        appended = self.session.store.since(before)

        interpretation = result.interpretation
        return Turn(
            text=text.strip(),
            narration=result.narration,
            rolls=tuple(_roll(r) for r in result.rolls),
            events=tuple(_entry(e) for e in appended if e.summary),
            intent=interpretation.intent.value if interpretation else "",
            rationale=interpretation.rationale if interpretation else "",
        )

    def roll(self, expression: str = "d20", *, reason: str = "") -> Roll:
        """Roll dice and put the result in the record."""
        try:
            result = self.session.roll(
                expression, reason=reason, actor_id=self._hero_id
            )
        except (ValueError, KeyError) as exc:
            raise TableError(f"{expression!r} is not a roll: {exc}") from exc
        return _roll(result)

    def narrate(self, text: str) -> Entry:
        """Put a line into the record as the DM."""
        if not text or not text.strip():
            raise TableError("nothing to narrate")
        return _entry(self.session.narrate(text.strip()))

    def scene(self) -> Scene:
        """Where the party is and what is happening around them."""
        state = self.session.state
        location_id = state.current_location_id or ""
        return Scene(
            location=state.location.name if state.location else "somewhere unmapped",
            description=state.location.description if state.location else "",
            time=str(state.world.time),
            weather=state.world.weather,
            npcs=tuple(
                n.name for n in self.session.atlas.npcs_at(self.session.journal, location_id)
            ),
            exits=tuple(
                loc.name
                for loc in self.session.atlas.neighbours(self.session.journal, location_id)
            ),
            in_combat=state.combat.active,
            combat_round=state.combat.round,
        )

    @property
    def quests(self) -> list[Quest]:
        """Every quest the table has picked up, active ones first."""
        journal = self.session.journal
        found = []
        for status in (QuestStatus.ACTIVE, QuestStatus.COMPLETED, QuestStatus.FAILED):
            for quest in self.session.quests.by_status(journal, status):
                found.append(
                    Quest(
                        title=quest.title,
                        summary=quest.summary,
                        status=status.value,
                        objectives=tuple(
                            (o.description, o.completed) for o in quest.objectives
                        ),
                    )
                )
        return found

    # --- the record --------------------------------------------------------

    def chronicle(self, limit: int | None = None) -> list[Entry]:
        """The narrative line of the log, oldest first."""
        events = [e for e in self.session.store if e.summary]
        if limit is not None:
            events = events[-limit:]
        return [_entry(e) for e in events]

    def recap(self) -> str:
        """A plain summary of the campaign so far, assembled from the log."""
        return self.session.recap()["narrative"]

    def briefing(self) -> dict[str, Any]:
        """Everything needed to pick this campaign back up, as plain data.

        Useful when the host application wants to hand the situation to its own
        model, or render a status screen without walking the log itself.
        """
        return self.session.briefing()

    # --- rewinding ---------------------------------------------------------

    def mark(self, label: str = "") -> Mark:
        """Set a point the campaign can be rewound to."""
        checkpoint = self.session.checkpoint(label.strip())
        return Mark(
            seq=checkpoint.event_seq,
            label=checkpoint.label,
            automatic=checkpoint.automatic,
        )

    @property
    def marks(self) -> list[Mark]:
        return [
            Mark(seq=c.event_seq, label=c.label, automatic=c.automatic)
            for c in self.session.checkpoints()
        ]

    def rewind(self, mark: Mark | int) -> None:
        """Undo everything after a mark.

        Rewinding truncates the log, so what comes back is not a snapshot but
        the campaign as it actually was at that point.
        """
        seq = mark.seq if isinstance(mark, Mark) else int(mark)
        if seq < 0:
            raise TableError("cannot rewind past the beginning")
        self.session.rollback(seq)
        # The seat may have been created after the mark; re-derive it.
        if self._hero_id not in self.session.state.party:
            party = list(self.session.state.party)
            self._hero_id = party[0] if party else None

    # --- dunder ------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.session.campaign.name

    @property
    def events(self) -> int:
        """How many events are on the record."""
        return self.session.store.head

    def __iter__(self) -> Iterator[Entry]:
        return iter(self.chronicle())

    def __len__(self) -> int:
        return self.events

    def __repr__(self) -> str:
        return (
            f"<Table {self.name!r}: {len(self.heroes)} in the party, "
            f"{self.events} events>"
        )


# --- converters ------------------------------------------------------------


def _hero(character: Any) -> Hero:
    return Hero(
        id=character.id,
        name=character.name,
        level=character.level,
        species=character.species,
        character_class=character.character_class,
        hp=character.hp.current,
        max_hp=character.hp.maximum,
        armor_class=character.armor_class,
        speed=character.speed,
        conditions=tuple(c.type.value for c in character.conditions),
        dead=character.dead,
    )


def _roll(result: DiceResult) -> Roll:
    success = None
    if result.outcome is not Outcome.NOT_APPLICABLE:
        success = result.outcome is Outcome.SUCCESS
    return Roll(
        expression=result.expression,
        total=result.total,
        dice=tuple(d.value for d in result.dice if not d.dropped),
        modifier=result.modifier,
        reason=result.reason,
        dc=result.dc,
        success=success,
    )


def _entry(event: GameEvent) -> Entry:
    return Entry(
        seq=event.seq,
        kind=ENTRY_KINDS.get(event.type.value, "system"),
        text=event.summary,
    )


__all__ = ["Table"]
