"""Deterministic, auditable dice.

Two rules govern this module:

1. Every roll returns a `DiceResult` carrying the individual dice, so any
   number that reaches a player can be traced back to physical d20s.
2. The random source is injectable and seedable, so the entire game engine is
   reproducible under test.

The DM AI never produces a number.  It produces a `DiceRequest`; this module
produces the result.  See spec section 7.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from .models.dice import DiceRequest, DiceResult, DieRoll, Outcome, RollMode

#: A term in a dice expression: '2d6', 'd20', '-1d4', '+3', '-2', 'd%'.
_TERM = re.compile(
    r"""
    (?P<sign>[+-])?\s*
    (?:
        (?P<count>\d+)?\s*[dD]\s*(?P<sides>\d+|%)   # dice term
      | (?P<flat>\d+)                               # constant term
    )
    """,
    re.VERBOSE,
)

MAX_DICE = 1000
MAX_SIDES = 1000


class DiceError(ValueError):
    """Raised for an expression the engine refuses to roll."""


@dataclass(frozen=True)
class Term:
    """One parsed component of an expression."""

    sign: int
    count: int          # 0 for a constant
    sides: int          # 0 for a constant
    flat: int = 0

    @property
    def is_dice(self) -> bool:
        return self.count > 0


def parse(expression: str) -> list[Term]:
    """Parse '2d6+3' into its terms.  Raises DiceError on anything unsupported."""
    text = expression.strip().replace(" ", "")
    if not text:
        raise DiceError("empty dice expression")

    terms: list[Term] = []
    pos = 0
    while pos < len(text):
        match = _TERM.match(text, pos)
        if match is None or match.end() == pos:
            raise DiceError(f"could not parse {expression!r} at position {pos}")
        pos = match.end()

        sign = -1 if match.group("sign") == "-" else 1

        if match.group("flat") is not None:
            terms.append(Term(sign=sign, count=0, sides=0, flat=int(match.group("flat"))))
            continue

        raw_sides = match.group("sides")
        sides = 100 if raw_sides == "%" else int(raw_sides)
        count = int(match.group("count") or 1)

        if count < 1 or count > MAX_DICE:
            raise DiceError(f"dice count out of range in {expression!r}: {count}")
        if sides < 2 or sides > MAX_SIDES:
            raise DiceError(f"die size out of range in {expression!r}: d{sides}")

        terms.append(Term(sign=sign, count=count, sides=sides))

    if not terms:
        raise DiceError(f"no terms found in {expression!r}")
    return terms


class DiceEngine:
    """Rolls dice.  Seed it to make a whole campaign reproducible."""

    def __init__(self, seed: int | None = None, rng: random.Random | None = None):
        self.rng = rng or random.Random(seed)
        self._seed = seed

    def reseed(self, seed: int) -> None:
        self.rng.seed(seed)
        self._seed = seed

    def _roll_die(self, sides: int) -> int:
        return self.rng.randint(1, sides)

    def roll(
        self,
        expression: str,
        reason: str = "roll",
        mode: RollMode = RollMode.NORMAL,
        dc: int | None = None,
        actor_id: str | None = None,
        target_id: str | None = None,
    ) -> DiceResult:
        """Roll an expression and return the full audit trail."""
        terms = parse(expression)
        dice: list[DieRoll] = []
        modifier = 0
        total = 0

        # Advantage applies to the first single d20 in the expression.
        advantage_applied = mode is RollMode.NORMAL

        for term in terms:
            if not term.is_dice:
                modifier += term.sign * term.flat
                total += term.sign * term.flat
                continue

            rolled: list[DieRoll] = []

            if not advantage_applied and term.sides == 20 and term.count == 1:
                pair = [self._roll_die(20), self._roll_die(20)]
                keep = max(pair) if mode is RollMode.ADVANTAGE else min(pair)
                kept_once = False
                for value in pair:
                    # Keep exactly one die matching the target value.
                    if value == keep and not kept_once:
                        rolled.append(DieRoll(sides=20, value=value, dropped=False))
                        kept_once = True
                    else:
                        rolled.append(DieRoll(sides=20, value=value, dropped=True))
                advantage_applied = True
            else:
                for _ in range(term.count):
                    rolled.append(
                        DieRoll(sides=term.sides, value=self._roll_die(term.sides))
                    )

            dice.extend(rolled)
            total += term.sign * sum(d.value for d in rolled if not d.dropped)

        result = DiceResult(
            expression=expression,
            reason=reason,
            mode=mode,
            dice=dice,
            modifier=modifier,
            total=total,
            dc=dc,
            actor_id=actor_id,
            target_id=target_id,
        )
        result.outcome = self._judge(result)
        return result

    def resolve(self, request: DiceRequest) -> DiceResult:
        """Roll a request produced by the DM layer."""
        return self.roll(
            request.expression,
            reason=request.reason,
            mode=request.mode,
            dc=request.dc,
            actor_id=request.actor_id,
            target_id=request.target_id,
        )

    @staticmethod
    def _judge(result: DiceResult) -> Outcome:
        """Natural 20/1 on a single d20 are critical regardless of the DC."""
        d20s = [d for d in result.kept_dice if d.sides == 20]
        if len(d20s) == 1:
            if d20s[0].value == 20:
                return Outcome.CRITICAL_SUCCESS
            if d20s[0].value == 1:
                return Outcome.CRITICAL_FAILURE

        if result.dc is None:
            return Outcome.NOT_APPLICABLE
        return Outcome.SUCCESS if result.total >= result.dc else Outcome.FAILURE


def average(expression: str) -> float:
    """Expected value of an expression, used for encounter budgeting."""
    total = 0.0
    for term in parse(expression):
        if term.is_dice:
            total += term.sign * term.count * (term.sides + 1) / 2
        else:
            total += term.sign * term.flat
    return total
