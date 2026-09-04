"""Dice request/result schemas.

The DM AI may *request* a roll; only the engine may *produce* a result.
Every result carries its individual dice so any roll can be audited.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .base import Model


class RollMode(str, Enum):
    NORMAL = "normal"
    ADVANTAGE = "advantage"
    DISADVANTAGE = "disadvantage"


class Outcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CRITICAL_SUCCESS = "critical_success"
    CRITICAL_FAILURE = "critical_failure"
    #: Rolls with no target number (damage, healing, a wandering-monster roll).
    NOT_APPLICABLE = "n/a"


class DiceRequest(Model):
    """A roll the engine has been asked to make."""

    expression: str = Field(description="e.g. '1d20+5', '2d6+3', '4d8', 'd%'")
    reason: str = Field(description="Human-readable, shown in the log: 'Perception check'")
    mode: RollMode = RollMode.NORMAL
    #: Difficulty class / target number.  ``None`` for damage and flat rolls.
    dc: int | None = None
    actor_id: str | None = None
    target_id: str | None = None


class DieRoll(Model):
    """One physical die."""

    sides: int
    value: int
    #: True when this die was rolled but discarded by advantage/disadvantage.
    dropped: bool = False


class DiceResult(Model):
    """The auditable outcome of a roll.  Never a bare integer."""

    expression: str
    reason: str
    mode: RollMode = RollMode.NORMAL
    dice: list[DieRoll] = Field(default_factory=list)
    modifier: int = 0
    total: int
    dc: int | None = None
    outcome: Outcome = Outcome.NOT_APPLICABLE
    actor_id: str | None = None
    target_id: str | None = None

    @property
    def kept_dice(self) -> list[DieRoll]:
        return [d for d in self.dice if not d.dropped]

    @property
    def natural(self) -> int:
        """Sum of the dice before modifiers."""
        return sum(d.value for d in self.kept_dice)

    def describe(self) -> str:
        """The block shown in the narrative log (spec section 7)."""
        faces = ", ".join(
            f"{d.value}{'*' if d.dropped else ''}" for d in self.dice
        )
        lines = [
            self.reason.upper(),
            f"{self.expression}: {faces}",
            f"Modifier: {self.modifier:+d}",
            f"Total: {self.total}",
        ]
        if self.dc is not None:
            lines.append(f"DC: {self.dc}")
        if self.outcome is not Outcome.NOT_APPLICABLE:
            lines.append(self.outcome.value.replace("_", " ").upper())
        return "\n".join(lines)
