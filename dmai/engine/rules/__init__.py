"""Rules adapters, and the registry that finds one by name.

A campaign stores the *id* of its ruleset, not an object, so a save is portable
between builds.  `load_rules` turns that id back into an adapter, and
`register_rules` lets a third-party pack join the lookup without this module
knowing anything about it.
"""

from __future__ import annotations

from pathlib import Path

from .base import RulesEngine
from .srd51 import DEFAULT_PACK_ROOT, RulesPackError, Srd51Rules

_REGISTRY: dict[str, type[RulesEngine]] = {Srd51Rules.id: Srd51Rules}


def register_rules(adapter: type[RulesEngine]) -> type[RulesEngine]:
    """Make a rules adapter loadable by its id.  Usable as a decorator."""
    _REGISTRY[adapter.id] = adapter
    return adapter


def available_rules() -> list[str]:
    return sorted(_REGISTRY)


def load_rules(pack_id: str = "srd51", pack_dir: Path | str | None = None) -> RulesEngine:
    """Build the adapter a campaign asked for.

    An unknown id is an error rather than a silent fall back to the SRD: a
    campaign built on rules that are not installed would resolve every check
    subtly wrong, which is worse than refusing to open it.
    """
    adapter = _REGISTRY.get(pack_id)
    if adapter is None:
        raise RulesPackError(
            f"unknown rules pack {pack_id!r}; installed: {', '.join(available_rules())}"
        )
    return adapter(pack_dir)  # type: ignore[call-arg]


__all__ = [
    "DEFAULT_PACK_ROOT",
    "RulesEngine",
    "RulesPackError",
    "Srd51Rules",
    "available_rules",
    "load_rules",
    "register_rules",
]
