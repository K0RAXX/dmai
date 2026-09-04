"""The OpenClaw adapter: a headless, multi-table Dungeon Master."""

from .adapter import CALLABLE_OPERATIONS, OPERATIONS, OpenClawAdapter, OpenClawError
from .bot import describe, handle, serve

__all__ = [
    "CALLABLE_OPERATIONS",
    "OPERATIONS",
    "OpenClawAdapter",
    "OpenClawError",
    "describe",
    "handle",
    "serve",
]
