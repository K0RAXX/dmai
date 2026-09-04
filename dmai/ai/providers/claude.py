"""Claude, via the official Anthropic SDK.

Three deliberate choices:

* **The SDK is imported lazily.**  `dmai.ai` must import on a machine with no
  `anthropic` package and no key, because the offline provider has to work
  there.  The import error is turned into a `ProviderError` that says what to
  install.
* **Structured output for pass 1.**  The interpretation pass uses
  `client.messages.parse`, so `ActionInterpretation` comes back validated or not
  at all.  A malformed interpretation is retried by the agent, never guessed.
* **A refusal is not a crash.**  `stop_reason == "refusal"` raises
  `ProviderRefusal`, which the agent catches and answers from the offline
  narrator.  A grim scene costs the table a paragraph of prose, not the session.

Credentials come from the environment (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
or an `ant auth login` profile).  Nothing here reads or writes a key, and no key
is ever stored in a campaign save.
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

from .base import AIProvider, Completion, Message, ProviderError, ProviderRefusal

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5"

#: USD per million tokens, for the session cost readout.  Cache reads are
#: roughly a tenth of the input rate.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
CACHE_READ_DISCOUNT = 0.1


class ClaudeProvider(AIProvider):
    """The default model provider."""

    id = "claude"
    name = "Claude"
    offline = False

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        client: Any = None,
        narration_effort: str = "medium",
        api_key: str | None = None,
    ):
        self.model = model
        self.narration_effort = narration_effort
        self._client = client
        self._api_key = api_key
        self.name = f"Claude ({model})"

    @property
    def client(self) -> Any:
        """Build the SDK client on first use, not at import time."""
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on install
                raise ProviderError(
                    "the anthropic package is not installed; "
                    'run: pip install "dmai[ai]"'
                ) from exc
            # No key argument unless one was passed explicitly: the SDK resolves
            # ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or a stored auth profile.
            self._client = (
                anthropic.Anthropic(api_key=self._api_key)
                if self._api_key
                else anthropic.Anthropic()
            )
        return self._client

    # --- generation --------------------------------------------------------

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        max_tokens: int = 2048,
        cache_system: bool = True,
    ) -> Completion:
        response = self._call(
            self.client.messages.create,
            system=_system_blocks(system, cache_system),
            messages=messages,
            max_tokens=max_tokens,
            output_config={"effort": self.narration_effort},
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return self._completion(response, text=text)

    def parse(
        self,
        *,
        system: str,
        messages: list[Message],
        schema: type[T],
        max_tokens: int = 2048,
        cache_system: bool = True,
    ) -> Completion:
        response = self._call(
            self.client.messages.parse,
            system=_system_blocks(system, cache_system),
            messages=messages,
            max_tokens=max_tokens,
            output_format=schema,
        )
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise ProviderError(
                f"{self.name} returned no {schema.__name__}; the DM will fall back."
            )
        return self._completion(response, parsed=parsed)

    def health(self) -> str:
        return f"{self.name} configured (credentials resolved by the Anthropic SDK)"

    # --- internals ---------------------------------------------------------

    def _call(self, endpoint, **kwargs):
        """One request, with SDK exceptions translated into ProviderError.

        Caught most-specific first so a bad key, a rate limit and an outage stay
        distinguishable in the log rather than collapsing into "AI failed".
        """
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ProviderError("the anthropic package is not installed") from exc

        try:
            response = endpoint(model=self.model, **kwargs)
        except anthropic.AuthenticationError as exc:
            raise ProviderError(
                "Anthropic rejected the credentials; check ANTHROPIC_API_KEY "
                "or run: ant auth login"
            ) from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(f"model {self.model!r} is not available to this account") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("Anthropic rate limit reached; try again shortly") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"could not reach Anthropic: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Anthropic returned {exc.status_code}: {exc}") from exc

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise ProviderRefusal(f"the model declined this scene ({category})")
        return response

    def _completion(self, response: Any, *, text: str = "", parsed: BaseModel | None = None) -> Completion:
        usage = getattr(response, "usage", None)
        inputs = getattr(usage, "input_tokens", 0) or 0
        outputs = getattr(usage, "output_tokens", 0) or 0
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        return Completion(
            text=text,
            parsed=parsed,
            model=getattr(response, "model", self.model),
            input_tokens=inputs + cached,
            output_tokens=outputs,
            cost_usd=estimate_cost(self.model, inputs, outputs, cached),
            meta={"stop_reason": getattr(response, "stop_reason", None), "cached_tokens": cached},
        )


def _system_blocks(system: str, cache: bool) -> Any:
    """The system prompt, marked cacheable.

    The DM's instructions and the campaign's style are identical on every turn
    of a session, so caching them is the difference between paying for the
    persona once and paying for it per action.
    """
    if not cache:
        return system
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    """Approximate spend for one call, for the session's cost readout."""
    rate_in, rate_out = PRICING.get(model, PRICING[DEFAULT_MODEL])
    return (
        input_tokens * rate_in
        + cached_tokens * rate_in * CACHE_READ_DISCOUNT
        + output_tokens * rate_out
    ) / 1_000_000


__all__ = ["DEFAULT_MODEL", "PRICING", "ClaudeProvider", "estimate_cost"]
