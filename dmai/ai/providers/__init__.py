"""Model providers, and the registry that finds one by name (spec section 18).

A campaign stores a provider *id*, never a client and never a credential, so a
save opens on a machine configured differently from the one that wrote it.
`load_provider` turns that id back into an object; unknown ids fall back to the
offline DM rather than failing, because a table that cannot reach a model should
still be able to play.
"""

from __future__ import annotations

from .base import (
    AIProvider,
    Completion,
    Message,
    ProviderError,
    ProviderRefusal,
)
from .claude import ClaudeProvider
from .offline import OfflineProvider

_REGISTRY: dict[str, type[AIProvider]] = {
    OfflineProvider.id: OfflineProvider,
    ClaudeProvider.id: ClaudeProvider,
}


def register_provider(provider: type[AIProvider]) -> type[AIProvider]:
    """Make a provider loadable by its id.  Usable as a decorator."""
    _REGISTRY[provider.id] = provider
    return provider


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def load_provider(provider_id: str = "offline", **kwargs) -> AIProvider:
    """Build a provider by id.

    Unlike a rules pack -- where the wrong ruleset silently corrupts every
    check, so an unknown id is refused -- an unknown *provider* costs only prose
    quality, and the game is better served by falling back than by refusing to
    open.
    """
    provider = _REGISTRY.get(provider_id)
    if provider is None:
        return OfflineProvider()
    return provider(**kwargs)


__all__ = [
    "AIProvider",
    "ClaudeProvider",
    "Completion",
    "Message",
    "OfflineProvider",
    "ProviderError",
    "ProviderRefusal",
    "available_providers",
    "load_provider",
    "register_provider",
]
