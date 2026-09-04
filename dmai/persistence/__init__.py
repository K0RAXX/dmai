"""Saving and loading campaigns.

The engine never writes to disk; this package does, and it does it by storing
the event log rather than the state, so a save always replays to exactly the
game it came from.
"""

from .store import (
    CURRENT_SAVE_VERSION,
    CampaignStore,
    CampaignSummary,
    SaveError,
    default_root,
    read_events,
)

__all__ = [
    "CURRENT_SAVE_VERSION",
    "CampaignStore",
    "CampaignSummary",
    "SaveError",
    "default_root",
    "read_events",
]
