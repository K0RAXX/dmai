"""Layer B: the Dungeon Master AI (spec sections 8, 9, 14, 18).

The engine decides what happened; this layer decides how it is told. Every
piece of it is optional -- a table with no model configured still plays, using
the offline DM.
"""

from .dm_agent.agent import DungeonMaster
from .providers import AIProvider, ProviderError, load_provider

__all__ = ["AIProvider", "DungeonMaster", "ProviderError", "load_provider"]
