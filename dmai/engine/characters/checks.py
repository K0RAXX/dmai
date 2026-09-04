"""Ability checks, skill checks and contests (spec section 9, step 5).

The DM AI decides *that* a check is needed and how hard it is; this module
decides what the dice say.  Keeping the two apart is what stops a language
model from quietly deciding that the rogue succeeded.
"""

from __future__ import annotations

from ..dice import DiceEngine
from ..models.actions import CheckRequest, IntentKind
from ..models.base import SKILL_ABILITIES, Ability, ConditionType
from ..models.dice import DiceResult, Outcome, RollMode
from ..models.events import EventType
from ..rules.base import RulesEngine
from ..state import Journal

#: Conditions that make a physical check harder.
PHYSICAL_DISADVANTAGE = {
    ConditionType.POISONED,
    ConditionType.FRIGHTENED,
    ConditionType.RESTRAINED,
    ConditionType.PRONE,
}

#: Conditions that make anything relying on sight harder.
SIGHT_DISADVANTAGE = {ConditionType.BLINDED}
SIGHT_SKILLS = {"perception", "investigation", "sleight_of_hand", "stealth"}


class CheckError(ValueError):
    """The check cannot be resolved as described."""


class CheckResolver:
    """Rolls the checks the DM asks for, against the active ruleset."""

    def __init__(self, rules: RulesEngine, dice: DiceEngine):
        self.rules = rules
        self.dice = dice

    def modifier_for(self, creature, *, skill: str | None, ability: Ability | None) -> tuple[int, str]:
        """The bonus and a label, resolving a skill to its governing ability."""
        if skill:
            if skill not in SKILL_ABILITIES:
                raise CheckError(f"unknown skill: {skill}")
            return self.rules.skill_modifier(creature, skill), skill.replace("_", " ")
        if ability:
            return creature.abilities.modifier(ability), ability.value
        raise CheckError("a check needs either a skill or an ability")

    def condition_mode(self, creature, skill: str | None, ability: Ability | None) -> RollMode:
        """Disadvantage the conditions imply, so the DM need not remember."""
        types = {c.type for c in creature.conditions}
        if types & SIGHT_DISADVANTAGE and (skill in SIGHT_SKILLS or ability is Ability.WIS):
            return RollMode.DISADVANTAGE
        physical = ability in {Ability.STR, Ability.DEX} or skill in {
            "athletics", "acrobatics", "stealth", "sleight_of_hand",
        }
        if types & PHYSICAL_DISADVANTAGE and physical:
            return RollMode.DISADVANTAGE
        if ConditionType.EXHAUSTION in types:
            return RollMode.DISADVANTAGE
        return RollMode.NORMAL

    def check(
        self,
        journal: Journal,
        creature_id: str,
        *,
        skill: str | None = None,
        ability: Ability | None = None,
        dc: int | None = 15,
        reason: str = "",
        mode: RollMode | None = None,
        visibility=None,
        audience_id: str | None = None,
    ) -> DiceResult:
        """Resolve one check and log it."""
        creature = journal.state.creature(creature_id)
        if creature is None:
            raise CheckError(f"no such creature: {creature_id}")

        bonus, label = self.modifier_for(creature, skill=skill, ability=ability)
        roll_mode = mode or self.condition_mode(creature, skill, ability)

        result = self.dice.roll(
            f"1d20{bonus:+d}" if bonus else "1d20",
            reason=reason or f"{creature.name} {label} check",
            mode=roll_mode,
            dc=dc,
            actor_id=creature_id,
        )

        kwargs = {}
        if visibility is not None:
            kwargs["visibility"] = visibility
        journal.record(
            EventType.CHECK_RESOLVED,
            actor_id=creature_id,
            summary=result.describe(),
            audience_id=audience_id,
            skill=skill,
            ability=ability.value if ability else None,
            dc=dc,
            roll=result.model_dump(mode="json"),
            **kwargs,
        )
        return result

    def passive(self, creature, skill: str) -> int:
        """No roll: what this character notices without trying."""
        return 10 + self.rules.skill_modifier(creature, skill)

    def contest(
        self,
        journal: Journal,
        first_id: str,
        second_id: str,
        *,
        first_skill: str,
        second_skill: str,
        reason: str = "",
    ) -> tuple[DiceResult, DiceResult, str | None]:
        """Two creatures roll against each other; ties favour the defender.

        Returns both rolls and the winner's id (``None`` on a tie), so the
        narration layer can describe a near-miss as a near-miss.
        """
        # The first roll sets the target number, so it has no DC of its own.
        first = self.check(
            journal, first_id, skill=first_skill, dc=None, reason=reason or "contest"
        )
        second = self.check(
            journal, second_id, skill=second_skill, dc=first.total, reason=reason or "contest"
        )
        if second.total >= first.total:
            return first, second, second_id
        return first, second, first_id

    def resolve_request(self, journal: Journal, request: CheckRequest) -> DiceResult:
        """Resolve a `CheckRequest` produced by the DM AI's interpretation pass."""
        if request.kind is IntentKind.SAVING_THROW:
            if request.ability is None:
                raise CheckError("a saving throw needs an ability")
            creature = journal.state.creature(request.actor_id)
            if creature is None:
                raise CheckError(f"no such creature: {request.actor_id}")
            bonus = self.rules.save_modifier(creature, request.ability)
            result = self.dice.roll(
                f"1d20{bonus:+d}" if bonus else "1d20",
                reason=request.reason or f"{creature.name} {request.ability.value} save",
                dc=request.dc,
                actor_id=request.actor_id,
            )
            journal.record(
                EventType.SAVE_RESOLVED,
                actor_id=request.actor_id,
                summary=result.describe(),
                ability=request.ability.value,
                roll=result.model_dump(mode="json"),
            )
            return result

        if request.expression:
            result = self.dice.roll(
                request.expression,
                reason=request.reason or "roll",
                dc=request.dc,
                actor_id=request.actor_id,
                target_id=request.target_id,
            )
            journal.record(
                EventType.DICE_ROLLED,
                actor_id=request.actor_id,
                target_id=request.target_id,
                summary=result.describe(),
                roll=result.model_dump(mode="json"),
            )
            return result

        return self.check(
            journal,
            request.actor_id,
            skill=request.skill,
            ability=request.ability,
            dc=request.dc if request.dc is not None else 15,
            reason=request.reason,
        )


def succeeded(result: DiceResult) -> bool:
    """One place to ask 'did that work', so criticals are never mishandled."""
    return result.outcome in {Outcome.SUCCESS, Outcome.CRITICAL_SUCCESS}


__all__ = ["CheckError", "CheckResolver", "succeeded"]
