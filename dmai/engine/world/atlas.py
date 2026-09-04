"""The map, the people on it, and the powers behind them (spec sections 8, 15).

Where the combat engine owns *this fight*, the atlas owns *everything that is
still true when the fight is over*: places, the routes between them, the NPCs
standing in them, and the factions whose plans run underneath.

Locations, NPCs and factions are each written to the log whole, because the
reducers for `LOCATION_ADDED`, `NPC_ADDED` and `FACTION_ADDED` replace the
record they name.  Re-recording an amended copy is therefore both the way to
create one of these and the way to edit one, and replay stays exact either way.
"""

from __future__ import annotations

from collections import deque

from ..models.base import Visibility
from ..models.character import Creature, CreatureKind
from ..models.events import EventType
from ..models.world import NPC, Attitude, Faction, Location, Secret
from ..state import Journal

#: Hostile to allied, in order.  `shift_attitude` walks this ladder rather than
#: jumping, so one rude remark cannot turn an ally into an enemy.
ATTITUDE_LADDER: list[Attitude] = [
    Attitude.HOSTILE,
    Attitude.UNFRIENDLY,
    Attitude.INDIFFERENT,
    Attitude.FRIENDLY,
    Attitude.ALLIED,
]

#: Faction standing runs -100 (open war) to +100 (sworn allies).
STANDING_RANGE = (-100, 100)


class AtlasError(ValueError):
    """The world was asked about a place, person or power that is not there."""


class Atlas:
    """The write path for `GameState.world`, plus the queries clients need."""

    # --- places ------------------------------------------------------------

    def add_location(
        self,
        journal: Journal,
        name: str,
        *,
        kind: str = "place",
        description: str = "",
        parent_id: str | None = None,
        connect_to: list[str] | None = None,
        features: list[str] | None = None,
        secrets: list[Secret] | None = None,
        discovered: bool = False,
        notes: str = "",
    ) -> Location:
        """Put a place on the map, optionally wiring it to its neighbours."""
        location = Location(
            name=name,
            kind=kind,
            description=description,
            parent_id=parent_id,
            features=features or [],
            secrets=secrets or [],
            discovered=discovered,
            notes=notes,
        )
        journal.record(
            EventType.LOCATION_ADDED,
            target_id=location.id,
            summary=f"{name} is added to the map.",
            location=location.model_dump(mode="json"),
            visibility=Visibility.DM_ONLY if not discovered else Visibility.PUBLIC,
        )
        for neighbour_id in connect_to or []:
            self.connect(journal, location.id, neighbour_id)
        return journal.state.world.locations[location.id]

    def connect(
        self, journal: Journal, from_id: str, to_id: str, *, one_way: bool = False
    ) -> None:
        """Open a route.  Two-way by default, because most doors are."""
        origin = self._location(journal, from_id)
        destination = self._location(journal, to_id)
        self._link(journal, origin, to_id)
        if not one_way:
            self._link(journal, destination, from_id)

    def enter(
        self,
        journal: Journal,
        location_id: str,
        *,
        summary: str = "",
    ) -> Location:
        """Move the party.  Entering a place also discovers it."""
        location = self._location(journal, location_id)
        journal.record(
            EventType.LOCATION_ENTERED,
            target_id=location_id,
            summary=summary or f"The party arrives at {location.name}.",
            location_id=location_id,
            location_name=location.name,
        )
        return journal.state.world.locations[location_id]

    def discover(self, journal: Journal, location_id: str) -> Location:
        """Mark a place known without going there -- a map, a rumour, a name."""
        location = self._location(journal, location_id)
        if not location.discovered:
            journal.record(
                EventType.LOCATION_DISCOVERED,
                target_id=location_id,
                summary=f"The party learns of {location.name}.",
                location_id=location_id,
            )
        return journal.state.world.locations[location_id]

    def describe(self, journal: Journal, location_id: str, description: str) -> Location:
        """Amend a place's description -- the DM improvised a detail worth keeping."""
        location = self._location(journal, location_id).model_copy(deep=True)
        location.description = description
        self._write_location(journal, location, f"{location.name} is described.")
        return journal.state.world.locations[location_id]

    def add_feature(self, journal: Journal, location_id: str, feature: str) -> Location:
        location = self._location(journal, location_id).model_copy(deep=True)
        if feature not in location.features:
            location.features = [*location.features, feature]
            self._write_location(journal, location, f"{location.name}: {feature}")
        return journal.state.world.locations[location_id]

    # --- people ------------------------------------------------------------

    def add_npc(
        self,
        journal: Journal,
        npc: NPC | str,
        *,
        location_id: str | None = None,
        role: str = "",
        faction_id: str | None = None,
        **fields,
    ) -> NPC:
        """Place a person in the world.  Accepts a built NPC or just a name."""
        if isinstance(npc, str):
            npc = NPC(name=npc, role=role, **fields)
        elif role or fields:
            npc = npc.model_copy(update={"role": role or npc.role, **fields})
        if location_id is not None:
            self._location(journal, location_id)  # fail early on a bad room
            npc = npc.model_copy(update={"location_id": location_id})
        if faction_id is not None:
            npc = npc.model_copy(update={"faction_id": faction_id})

        journal.record(
            EventType.NPC_ADDED,
            target_id=npc.id,
            summary=f"{npc.name} enters the campaign.",
            npc=npc.model_dump(mode="json"),
            visibility=Visibility.DM_ONLY,
        )
        if faction_id:
            self._enrol(journal, faction_id, npc.id)
        return journal.state.world.npcs[npc.id]

    def move_npc(self, journal: Journal, npc_id: str, location_id: str | None) -> NPC:
        """People do not stay where the DM left them."""
        npc = self._npc(journal, npc_id)
        if location_id is not None:
            self._location(journal, location_id)
        previous = npc.location_id

        if previous and previous in journal.state.world.locations:
            room = journal.state.world.locations[previous].model_copy(deep=True)
            room.npc_ids = [i for i in room.npc_ids if i != npc_id]
            self._write_location(journal, room, f"{npc.name} leaves {room.name}.")

        moved = npc.model_copy(deep=True)
        moved.location_id = location_id
        self._write_npc(journal, moved, f"{npc.name} moves.")
        return journal.state.world.npcs[npc_id]

    def meet(
        self, journal: Journal, npc_id: str, character_ids: list[str] | None = None
    ) -> NPC:
        """First contact.  Logs the meeting and reveals the NPC to the table."""
        npc = self._npc(journal, npc_id)
        journal.record(
            EventType.NPC_MET,
            target_id=npc_id,
            summary=f"The party meets {npc.name}"
            + (f", {npc.role}." if npc.role else "."),
            npc_id=npc_id,
            npc_name=npc.name,
            character_ids=character_ids or [],
        )
        return npc

    def set_attitude(
        self,
        journal: Journal,
        npc_id: str,
        character_id: str,
        attitude: Attitude,
        *,
        reason: str = "",
    ) -> Attitude:
        npc = self._npc(journal, npc_id)
        journal.record(
            EventType.NPC_ATTITUDE_CHANGED,
            actor_id=npc_id,
            target_id=character_id,
            summary=(
                f"{npc.name} is now {attitude.value} toward the party"
                f"{f' ({reason})' if reason else ''}."
            ),
            npc_id=npc_id,
            character_id=character_id,
            attitude=attitude.value,
            reason=reason,
        )
        return journal.state.world.npcs[npc_id].attitude_toward(character_id)

    def shift_attitude(
        self, journal: Journal, npc_id: str, character_id: str, steps: int, *, reason: str = ""
    ) -> Attitude:
        """Nudge an NPC along the attitude ladder, clamped at both ends."""
        npc = self._npc(journal, npc_id)
        current = npc.attitude_toward(character_id)
        index = ATTITUDE_LADDER.index(current) + steps
        index = max(0, min(len(ATTITUDE_LADDER) - 1, index))
        target = ATTITUDE_LADDER[index]
        if target is current:
            return current
        return self.set_attitude(journal, npc_id, character_id, target, reason=reason)

    def reveal(self, journal: Journal, npc_id: str, secret_id: str, to_id: str) -> Secret:
        """An NPC lets something slip.  The secret stays logged as DM-only.

        What the party *learned* is a discovery, recorded by the quest tracker;
        this only records that this NPC has now told this character.
        """
        npc = self._npc(journal, npc_id).model_copy(deep=True)
        secret = next((s for s in npc.secrets if s.id == secret_id), None)
        if secret is None:
            raise AtlasError(f"{npc.name} is not keeping a secret {secret_id}")
        if to_id not in secret.known_by:
            secret.known_by = [*secret.known_by, to_id]
            self._write_npc(journal, npc, f"{npc.name} reveals something.")
        return secret

    # --- powers ------------------------------------------------------------

    def add_faction(
        self,
        journal: Journal,
        faction: Faction | str,
        *,
        description: str = "",
        goals: list[str] | None = None,
        agenda: list[str] | None = None,
        **fields,
    ) -> Faction:
        if isinstance(faction, str):
            faction = Faction(
                name=faction,
                description=description,
                goals=goals or [],
                agenda=agenda or [],
                **fields,
            )
        journal.record(
            EventType.FACTION_ADDED,
            target_id=faction.id,
            summary=f"{faction.name} is a power in this world.",
            faction=faction.model_dump(mode="json"),
            visibility=Visibility.DM_ONLY,
        )
        return journal.state.world.factions[faction.id]

    def adjust_standing(
        self, journal: Journal, faction_id: str, delta: int, *, reason: str = ""
    ) -> int:
        """Change how a faction feels about the party.  Clamped to +/-100."""
        faction = self._faction(journal, faction_id).model_copy(deep=True)
        low, high = STANDING_RANGE
        faction.party_standing = max(low, min(high, faction.party_standing + delta))
        direction = "rises" if delta > 0 else "falls"
        self._write_faction(
            journal,
            faction,
            f"Standing with {faction.name} {direction} to {faction.party_standing}"
            + (f" ({reason})." if reason else "."),
        )
        return journal.state.world.factions[faction_id].party_standing

    def set_relation(
        self, journal: Journal, faction_id: str, other_id: str, standing: int
    ) -> Faction:
        """Set how two factions regard each other.  Written on both sides."""
        low, high = STANDING_RANGE
        standing = max(low, min(high, standing))
        for a, b in ((faction_id, other_id), (other_id, faction_id)):
            faction = self._faction(journal, a).model_copy(deep=True)
            faction.relations = {**faction.relations, b: standing}
            self._write_faction(journal, faction, f"{faction.name} relations change.")
        return journal.state.world.factions[faction_id]

    # --- queries -----------------------------------------------------------

    @staticmethod
    def neighbours(journal: Journal, location_id: str) -> list[Location]:
        location = journal.state.world.locations.get(location_id)
        if location is None:
            return []
        return [
            journal.state.world.locations[i]
            for i in location.connections
            if i in journal.state.world.locations
        ]

    @staticmethod
    def route(journal: Journal, from_id: str, to_id: str) -> list[str] | None:
        """Shortest path along known connections, or None if there is none.

        Breadth-first, so the first path found is the shortest, and the walk is
        deterministic because connections keep insertion order.
        """
        locations = journal.state.world.locations
        if from_id not in locations or to_id not in locations:
            return None
        if from_id == to_id:
            return [from_id]

        frontier: deque[list[str]] = deque([[from_id]])
        seen = {from_id}
        while frontier:
            path = frontier.popleft()
            for step in locations[path[-1]].connections:
                if step in seen or step not in locations:
                    continue
                if step == to_id:
                    return [*path, step]
                seen.add(step)
                frontier.append([*path, step])
        return None

    @staticmethod
    def npcs_at(journal: Journal, location_id: str) -> list[NPC]:
        return [
            npc
            for npc in journal.state.world.npcs.values()
            if npc.location_id == location_id
        ]

    @staticmethod
    def present(journal: Journal) -> list[Creature]:
        """Everyone in the room with the party: NPCs here, plus the bestiary."""
        here = journal.state.current_location_id
        return [
            *(n for n in journal.state.world.npcs.values() if n.location_id == here),
            *(c for c in journal.state.bestiary.values() if not c.dead),
        ]

    @staticmethod
    def party(journal: Journal) -> list[Creature]:
        return [c for c in journal.state.party.values() if c.kind is CreatureKind.PLAYER]

    # --- internals ---------------------------------------------------------

    def _link(self, journal: Journal, location: Location, other_id: str) -> None:
        if other_id in location.connections:
            return
        updated = location.model_copy(deep=True)
        updated.connections = [*updated.connections, other_id]
        other = journal.state.world.locations[other_id]
        self._write_location(
            journal, updated, f"{updated.name} connects to {other.name}."
        )

    def _enrol(self, journal: Journal, faction_id: str, npc_id: str) -> None:
        faction = self._faction(journal, faction_id).model_copy(deep=True)
        if npc_id in faction.member_ids:
            return
        faction.member_ids = [*faction.member_ids, npc_id]
        self._write_faction(journal, faction, f"{faction.name} gains a member.")

    @staticmethod
    def _location(journal: Journal, location_id: str) -> Location:
        location = journal.state.world.locations.get(location_id)
        if location is None:
            raise AtlasError(f"no location {location_id} in this world")
        return location

    @staticmethod
    def _npc(journal: Journal, npc_id: str) -> NPC:
        npc = journal.state.world.npcs.get(npc_id)
        if npc is None:
            raise AtlasError(f"no NPC {npc_id} in this world")
        return npc

    @staticmethod
    def _faction(journal: Journal, faction_id: str) -> Faction:
        faction = journal.state.world.factions.get(faction_id)
        if faction is None:
            raise AtlasError(f"no faction {faction_id} in this world")
        return faction

    @staticmethod
    def _write_location(journal: Journal, location: Location, summary: str) -> None:
        journal.record(
            EventType.LOCATION_ADDED,
            target_id=location.id,
            summary=summary,
            location=location.model_dump(mode="json"),
            visibility=Visibility.DM_ONLY,
        )

    @staticmethod
    def _write_npc(journal: Journal, npc: NPC, summary: str) -> None:
        journal.record(
            EventType.NPC_ADDED,
            target_id=npc.id,
            summary=summary,
            npc=npc.model_dump(mode="json"),
            visibility=Visibility.DM_ONLY,
        )

    @staticmethod
    def _write_faction(journal: Journal, faction: Faction, summary: str) -> None:
        journal.record(
            EventType.FACTION_ADDED,
            target_id=faction.id,
            summary=summary,
            faction=faction.model_dump(mode="json"),
            visibility=Visibility.DM_ONLY,
        )


__all__ = ["ATTITUDE_LADDER", "STANDING_RANGE", "Atlas", "AtlasError"]
