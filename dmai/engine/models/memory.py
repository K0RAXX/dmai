"""Structured memory.

The DM does not keep a raw transcript forever (spec section 8).  Important
facts are extracted into typed memories that can be retrieved by tier and
relevance when a session resumes.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from .base import Model, Visibility, new_id, utcnow


class MemoryTier(str, Enum):
    SHORT_TERM = "short_term"   # the current scene
    SESSION = "session"         # this sitting
    CAMPAIGN = "campaign"       # durable facts about the party and story
    WORLD = "world"             # things that happened off-screen


class MemoryKind(str, Enum):
    FACT = "fact"
    RELATIONSHIP = "relationship"
    PROMISE = "promise"
    DISCOVERY = "discovery"
    CONSEQUENCE = "consequence"
    PREFERENCE = "preference"   # how this table likes to play
    LORE = "lore"


class Memory(Model):
    id: str = Field(default_factory=lambda: new_id("mem"))
    tier: MemoryTier = MemoryTier.SESSION
    kind: MemoryKind = MemoryKind.FACT
    content: str
    #: Ids of characters, NPCs, locations or quests this touches.
    subjects: list[str] = Field(default_factory=list)
    #: 0-100; drives what gets loaded into a resume prompt first.
    importance: int = 50
    visibility: Visibility = Visibility.PUBLIC
    created_at: datetime = Field(default_factory=utcnow)
    #: Event that produced this memory, for provenance.
    source_event_id: str | None = None
