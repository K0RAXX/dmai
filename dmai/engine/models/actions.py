"""Player input and the structured interpretation the DM AI produces.

`ActionInterpretation` is the schema the language model must return in pass 1
of the reasoning loop.  It describes *what the player is trying to do* and
*what must be rolled* -- never what the result was.  The engine decides that.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .base import Ability, Model, Visibility, new_id
from .dice import DiceRequest, DiceResult


class IntentKind(str, Enum):
    ATTACK = "attack"
    CAST_SPELL = "cast_spell"
    SKILL_CHECK = "skill_check"
    SAVING_THROW = "saving_throw"
    MOVE = "move"
    TALK = "talk"
    SEARCH = "search"
    USE_ITEM = "use_item"
    REST = "rest"
    #: Pure roleplay or a question; no mechanics required.
    FREEFORM = "freeform"
    #: Out-of-character question about rules or state.
    META = "meta"


class CheckRequest(Model):
    """A rule resolution the DM has determined is required."""

    kind: IntentKind
    actor_id: str
    target_id: str | None = None
    ability: Ability | None = None
    skill: str | None = None
    dc: int | None = None
    #: Set when the DM wants a specific expression (damage dice, healing).
    expression: str | None = None
    reason: str = ""


class PlayerAction(Model):
    """Raw natural-language input from a player."""

    id: str = Field(default_factory=lambda: new_id("act"))
    campaign_id: str
    player_id: str | None = None
    character_id: str | None = None
    text: str


class ActionInterpretation(Model):
    """Pass 1 output.  Schema-validated; a malformed one is retried, not guessed."""

    intent: IntentKind
    #: One-line restatement of what the character is doing.
    summary: str = ""
    actor_id: str | None = None
    target_ids: list[str] = Field(default_factory=list)
    checks: list[CheckRequest] = Field(default_factory=list)
    #: Parts of the game state the narration pass will need.
    state_queries: list[str] = Field(default_factory=list)
    #: True when the action changes the world, not just the conversation.
    mutates_state: bool = True
    #: Why the DM decided a check was needed -- shown as a short explanation,
    #: never as raw chain-of-thought (spec section 17).
    rationale: str = ""


class ActionResult(Model):
    """Everything the engine produced for one player action."""

    action_id: str
    interpretation: ActionInterpretation | None = None
    rolls: list[DiceResult] = Field(default_factory=list)
    #: Ids of events appended while resolving this action.
    event_ids: list[str] = Field(default_factory=list)
    narration: str = ""
    visibility: Visibility = Visibility.PUBLIC
    audience_id: str | None = None
    #: Populated when the AI provider was used; surfaced in diagnostics.
    cost_usd: float = 0.0
    error: str | None = None


__all__ = [
    "IntentKind",
    "CheckRequest",
    "PlayerAction",
    "ActionInterpretation",
    "ActionResult",
    "DiceRequest",
    "DiceResult",
]
