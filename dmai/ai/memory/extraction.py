"""Structured memory (spec section 8).

The spec is explicit: *do not store every conversational token forever*.  So the
DM does not carry a transcript.  It carries typed `Memory` records extracted
from the event log, scored, and retrieved by relevance when a scene needs them.

Extraction here is rule-based rather than model-based, on purpose.  What matters
about an event -- that a promise was made, that an NPC turned hostile, that the
party learned a fact -- is already typed in the log, so reading it back costs
nothing, cannot hallucinate, and works with no provider configured.  A model can
add colour to a memory later; it is not needed to notice one.

Tiers age downward: the current scene is SHORT_TERM, this sitting is SESSION,
durable facts are CAMPAIGN, and things that happened off-screen are WORLD.
"""

from __future__ import annotations

from dmai.engine.models.base import Visibility
from dmai.engine.models.events import EventType, GameEvent
from dmai.engine.models.memory import Memory, MemoryKind, MemoryTier

#: Event type -> (kind, tier, importance).  An event type absent from this map
#: is not memorable: dice rolls, turn markers and movement are noise a week
#: later, and keeping them would crowd out what matters.
MEMORABLE: dict[EventType, tuple[MemoryKind, MemoryTier, int]] = {
    EventType.DISCOVERY: (MemoryKind.DISCOVERY, MemoryTier.CAMPAIGN, 70),
    EventType.STORY_DECISION: (MemoryKind.FACT, MemoryTier.CAMPAIGN, 80),
    EventType.CONSEQUENCE_ADDED: (MemoryKind.PROMISE, MemoryTier.CAMPAIGN, 75),
    EventType.CONSEQUENCE_RESOLVED: (MemoryKind.CONSEQUENCE, MemoryTier.CAMPAIGN, 65),
    EventType.QUEST_ADDED: (MemoryKind.FACT, MemoryTier.CAMPAIGN, 60),
    EventType.OBJECTIVE_COMPLETED: (MemoryKind.FACT, MemoryTier.SESSION, 50),
    EventType.NPC_MET: (MemoryKind.RELATIONSHIP, MemoryTier.CAMPAIGN, 55),
    EventType.NPC_ATTITUDE_CHANGED: (MemoryKind.RELATIONSHIP, MemoryTier.CAMPAIGN, 65),
    EventType.CREATURE_DIED: (MemoryKind.FACT, MemoryTier.SESSION, 60),
    EventType.LEVEL_UP: (MemoryKind.FACT, MemoryTier.CAMPAIGN, 55),
    EventType.WORLD_EVENT: (MemoryKind.LORE, MemoryTier.WORLD, 50),
    EventType.LOCATION_ENTERED: (MemoryKind.FACT, MemoryTier.SESSION, 30),
    EventType.COMBAT_ENDED: (MemoryKind.FACT, MemoryTier.SESSION, 45),
}

#: A player character dying is the most memorable thing that can happen.
DEATH_IMPORTANCE = 95


def extract(events: list[GameEvent], *, party_ids: set[str] | None = None) -> list[Memory]:
    """Turn a stretch of log into the memories worth keeping.

    Events the table never saw stay out: a memory the DM can quote is a memory
    the DM can leak, and DM-only knowledge belongs in the world state, not here.
    """
    party = party_ids or set()
    memories: list[Memory] = []

    for event in events:
        entry = MEMORABLE.get(event.type)
        if entry is None or not event.summary:
            continue
        if event.visibility is Visibility.DM_ONLY:
            continue

        kind, tier, importance = entry
        if event.type is EventType.CREATURE_DIED and (event.target_id in party):
            importance = DEATH_IMPORTANCE

        memories.append(
            Memory(
                tier=tier,
                kind=kind,
                content=event.summary,
                subjects=[i for i in (event.actor_id, event.target_id) if i],
                importance=importance,
                visibility=event.visibility,
                source_event_id=event.id,
            )
        )
    return memories


def recall(
    memories: list[Memory],
    *,
    subjects: set[str] | None = None,
    limit: int = 12,
    player_id: str | None = None,
) -> list[Memory]:
    """The memories most worth putting in front of the DM right now.

    Scored on importance, tier durability, and whether the memory is about
    someone in the scene -- so an NPC the party is talking to pulls up what they
    promised that NPC three sessions ago.
    """
    here = subjects or set()

    def score(memory: Memory) -> tuple[int, int]:
        relevance = 40 if here & set(memory.subjects) else 0
        durability = {
            MemoryTier.CAMPAIGN: 20,
            MemoryTier.WORLD: 10,
            MemoryTier.SESSION: 5,
            MemoryTier.SHORT_TERM: 0,
        }[memory.tier]
        return (memory.importance + relevance + durability, memory.importance)

    visible = [m for m in memories if _visible_to(m, player_id)]
    return sorted(visible, key=score, reverse=True)[:limit]


def _visible_to(memory: Memory, player_id: str | None) -> bool:
    """DM-only memories never reach a prompt that produces player-facing text."""
    if memory.visibility is Visibility.PUBLIC:
        return True
    return memory.visibility is Visibility.PRIVATE and player_id is not None


__all__ = ["DEATH_IMPORTANCE", "MEMORABLE", "extract", "recall"]
