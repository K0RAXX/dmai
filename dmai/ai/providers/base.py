"""The model-provider seam (spec section 18).

Nothing above this file knows which model is answering.  A provider does two
things and no more:

* `complete` -- free text, for narration.
* `parse`    -- a schema-validated object, for the interpretation pass.

`parse` exists as its own method because the DM's first pass must return
*structure*, not prose.  A provider that cannot constrain output is expected to
implement `parse` by asking for JSON and validating it, and to raise
`ProviderError` rather than hand back a guess.

Credentials are never arguments here and never stored in a campaign.  They come
from the environment, which is the whole of the spec's "never hard-code API
keys".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

#: A message in the shape every provider is given it.  Roles are "user" and
#: "assistant"; the system prompt is passed separately because providers place
#: it differently.
Message = dict[str, str]


class ProviderError(RuntimeError):
    """The model could not be reached, or would not answer usefully."""


class ProviderRefusal(ProviderError):
    """The model declined to answer.

    Its own class because a DM must degrade rather than crash: a refusal on one
    grim scene should cost the table a paragraph of prose, not the campaign.
    """


@dataclass
class Completion:
    """One answer, plus what it cost to get it."""

    text: str = ""
    #: Populated by `parse`; None for a plain completion.
    parsed: BaseModel | None = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    #: Provider-specific extras, surfaced in diagnostics and nowhere else.
    meta: dict[str, Any] = field(default_factory=dict)


class AIProvider(ABC):
    """What the DM agent needs of a language model."""

    id: str = "abstract"
    name: str = "Abstract Provider"
    #: True when this provider can answer without a network or a key, which is
    #: what makes a table playable (and a test suite runnable) offline.
    offline: bool = False

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        max_tokens: int = 2048,
        cache_system: bool = True,
    ) -> Completion:
        """Free-text generation."""

    @abstractmethod
    def parse(
        self,
        *,
        system: str,
        messages: list[Message],
        schema: type[T],
        max_tokens: int = 2048,
        cache_system: bool = True,
    ) -> Completion:
        """Generation constrained to ``schema``.  `Completion.parsed` is an
        instance of it, or `ProviderError` is raised.  Never a partial object."""

    def health(self) -> str:
        """A one-line answer to "will this work right now?" for the CLI."""
        return f"{self.name} ready"


__all__ = [
    "AIProvider",
    "Completion",
    "Message",
    "ProviderError",
    "ProviderRefusal",
]
