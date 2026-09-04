"""Encounter generation and budgeting (spec section 12).

Combat encounters are built against the party's experience-point budget, so
"moderate" means moderate for *this* party rather than for an imaginary one.
The non-combat kinds -- social, exploration, puzzles, traps, mysteries --
carry structure (what succeeds, what fails, what it is worth) without
prescribing prose; the DM AI writes the words.

The generator is deterministic under a seeded `DiceEngine`, which is what lets
a whole session be replayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..dice import DiceEngine
from ..models.base import Difficulty
from ..models.character import Creature
from ..models.combat import Encounter, EncounterKind
from ..rules.base import RulesEngine

#: Multiplier applied to a group's raw experience value.  More bodies means a
#: harder fight than the sum of its parts, because they all act every round.
GROUP_MULTIPLIERS = [
    (1, 1.0),
    (2, 1.5),
    (3, 2.0),
    (7, 2.5),
    (11, 3.0),
    (15, 4.0),
]

#: How far over or under budget a generated fight may land.
BUDGET_TOLERANCE = 0.35

#: Non-combat encounter skeletons, keyed by kind.  Each is a shape the DM
#: fills in: the engine supplies the mechanics, never the fiction.
NON_COMBAT_TEMPLATES: dict[EncounterKind, dict] = {
    EncounterKind.SOCIAL: {
        "name": "A conversation with something at stake",
        "skills": ["persuasion", "insight", "deception", "intimidation"],
        "success": ["the other party concedes something they wanted to keep"],
        "failure": ["the door closes, politely or otherwise"],
    },
    EncounterKind.EXPLORATION: {
        "name": "Ground that does not want to be crossed",
        "skills": ["survival", "athletics", "perception"],
        "success": ["the party arrives with time and strength to spare"],
        "failure": ["the party arrives late, loud, or short a resource"],
    },
    EncounterKind.PUZZLE: {
        "name": "A lock that is not a lock",
        "skills": ["investigation", "arcana", "history"],
        "success": ["the way opens"],
        "failure": ["the way stays shut; another route must be found"],
    },
    EncounterKind.TRAP: {
        "name": "Something waiting to be triggered",
        "skills": ["perception", "investigation", "sleight_of_hand"],
        "success": ["spotted and disarmed before it mattered"],
        "failure": ["it goes off, and the noise carries"],
    },
    EncounterKind.MYSTERY: {
        "name": "A question with an inconvenient answer",
        "skills": ["investigation", "insight", "history"],
        "success": ["the party learns who benefits"],
        "failure": ["the trail goes cold and someone else moves first"],
    },
    EncounterKind.TRAVEL: {
        "name": "A day on the road",
        "skills": ["survival", "perception", "animal_handling"],
        "success": ["uneventful miles"],
        "failure": ["a delay, and whatever the delay attracts"],
    },
    EncounterKind.ENVIRONMENTAL: {
        "name": "The place itself is the problem",
        "skills": ["athletics", "acrobatics", "survival"],
        "success": ["the hazard is crossed"],
        "failure": ["the hazard takes its toll"],
    },
}


class EncounterError(ValueError):
    """The encounter cannot be built as described."""


@dataclass
class Budget:
    """The party's experience budget for one fight."""

    difficulty: Difficulty
    party_size: int
    threshold: int
    #: Raw monster experience, before the group multiplier.
    spent: int = 0
    #: Experience after the group multiplier -- what the budget is judged on.
    adjusted: int = 0
    monsters: list[str] = field(default_factory=list)

    @property
    def load(self) -> float:
        """1.0 is exactly on budget; 1.4 is a fight to remember."""
        return self.adjusted / self.threshold if self.threshold else 0.0

    def describe(self) -> str:
        return (
            f"{self.difficulty.value} for {self.party_size}: "
            f"{self.adjusted} adjusted XP against a {self.threshold} budget "
            f"({self.load:.2f}x)"
        )


def group_multiplier(count: int) -> float:
    """The encounter multiplier for a group of this size."""
    multiplier = 1.0
    for minimum, value in GROUP_MULTIPLIERS:
        if count >= minimum:
            multiplier = value
    return multiplier


def party_threshold(
    rules: RulesEngine, party: list[Creature], difficulty: Difficulty | str
) -> int:
    """Sum the per-character budgets for the whole party."""
    label = difficulty.value if isinstance(difficulty, Difficulty) else str(difficulty)
    if label == Difficulty.CUSTOM.value:
        label = Difficulty.MODERATE.value
    return sum(rules.xp_threshold(member.level, label) for member in party)


class EncounterGenerator:
    """Builds encounters that fit the party in front of it."""

    def __init__(self, rules: RulesEngine, dice: DiceEngine):
        self.rules = rules
        self.dice = dice

    # --- combat ------------------------------------------------------------

    def _catalogue(self) -> list[tuple[str, dict]]:
        """Every monster in the pack that has an experience value, cheapest first."""
        monsters = getattr(self.rules, "monsters", {})
        entries = [(key, block) for key, block in monsters.items() if block.get("xp")]
        if not entries:
            raise EncounterError(f"rules pack {self.rules.id} has no usable bestiary")
        return sorted(entries, key=lambda pair: pair[1]["xp"])

    def budget(
        self,
        party: list[Creature],
        difficulty: Difficulty | str = Difficulty.MODERATE,
        composition: dict[str, int] | None = None,
    ) -> Budget:
        """Score a proposed monster line-up against the party's budget."""
        if not party:
            raise EncounterError("an encounter needs a party to be measured against")
        threshold = party_threshold(self.rules, party, difficulty)
        budget = Budget(
            difficulty=Difficulty(difficulty) if isinstance(difficulty, str) else difficulty,
            party_size=len(party),
            threshold=threshold,
        )
        if not composition:
            return budget

        count = 0
        for key, quantity in composition.items():
            block = self.rules.monster(key)
            if block is None:
                raise EncounterError(f"no creature {key!r} in rules pack {self.rules.id}")
            budget.spent += int(block.get("xp", 0)) * quantity
            budget.monsters.extend([key] * quantity)
            count += quantity
        budget.adjusted = int(budget.spent * group_multiplier(count))
        return budget

    def compose(
        self,
        party: list[Creature],
        difficulty: Difficulty | str = Difficulty.MODERATE,
        *,
        theme: list[str] | None = None,
        max_monsters: int = 8,
    ) -> Budget:
        """Choose a monster line-up that lands close to the budget.

        The approach is deliberately simple and predictable: pick a keystone
        creature the budget can afford, then add cheaper company until the
        adjusted total is close enough.  Predictable beats clever here -- a DM
        needs to be able to look at the result and see why it was chosen.
        """
        threshold = party_threshold(self.rules, party, difficulty)
        catalogue = self._catalogue()
        if theme:
            themed = [(k, b) for k, b in catalogue if k in set(theme)]
            if themed:
                catalogue = themed

        affordable = [(k, b) for k, b in catalogue if b["xp"] <= threshold]
        keystone_pool = affordable or catalogue[:1]
        keystone_key, keystone = keystone_pool[
            self.dice.rng.randrange(len(keystone_pool))
            if len(keystone_pool) > 1
            else 0
        ]

        composition: dict[str, int] = {keystone_key: 1}
        count = 1

        cheap = [(k, b) for k, b in catalogue if b["xp"] <= max(1, keystone["xp"])]
        filler_key, filler = (cheap or catalogue)[0]

        while count < max_monsters:
            trial = dict(composition)
            trial[filler_key] = trial.get(filler_key, 0) + 1
            scored = self.budget(party, difficulty, trial)
            if scored.load > 1 + BUDGET_TOLERANCE:
                break
            composition = trial
            count += 1
            if scored.load >= 1 - BUDGET_TOLERANCE:
                break

        return self.budget(party, difficulty, composition)

    def combat_encounter(
        self,
        party: list[Creature],
        difficulty: Difficulty | str = Difficulty.MODERATE,
        *,
        name: str = "",
        location_id: str | None = None,
        theme: list[str] | None = None,
        composition: dict[str, int] | None = None,
    ) -> tuple[Encounter, Budget]:
        """Build a combat encounter and the budget that justifies it."""
        scored = (
            self.budget(party, difficulty, composition)
            if composition
            else self.compose(party, difficulty, theme=theme)
        )

        roster: dict[str, int] = {}
        for key in scored.monsters:
            roster[key] = roster.get(key, 0) + 1
        listing = ", ".join(
            f"{count}x {(self.rules.monster(key) or {}).get('name', key)}"
            for key, count in roster.items()
        )

        encounter = Encounter(
            kind=EncounterKind.COMBAT,
            name=name or listing,
            description=f"A fight: {listing}.",
            difficulty=scored.difficulty,
            location_id=location_id,
            success_conditions=["the opposition is defeated, driven off, or talked down"],
            failure_conditions=["the party is defeated, captured, or forced to withdraw"],
            rewards=[f"{scored.spent} XP"],
        )
        # The keys live on the encounter so a client can spawn the creatures
        # itself; ``creature_ids`` is filled in once they actually exist.
        encounter.__pydantic_extra__ = None
        encounter.description += f"  [roster: {roster}]"
        return encounter, scored

    # --- everything that is not a fight ------------------------------------

    def challenge_encounter(
        self,
        kind: EncounterKind,
        party: list[Creature],
        difficulty: Difficulty | str = Difficulty.MODERATE,
        *,
        name: str = "",
        description: str = "",
        location_id: str | None = None,
    ) -> Encounter:
        """A social, exploration, puzzle, trap, mystery or travel encounter.

        The engine's contribution is the difficulty class and the shape of
        success and failure; the fiction is the DM's job.
        """
        template = NON_COMBAT_TEMPLATES.get(kind)
        if template is None:
            raise EncounterError(f"{kind.value} encounters have no template; build it explicitly")

        label = difficulty.value if isinstance(difficulty, Difficulty) else str(difficulty)
        dc = self.difficulty_class(label)

        return Encounter(
            kind=kind,
            name=name or template["name"],
            description=description or f"{template['name']} (DC {dc}).",
            difficulty=Difficulty(label) if label in Difficulty._value2member_map_ else Difficulty.CUSTOM,
            location_id=location_id,
            success_conditions=list(template["success"]),
            failure_conditions=list(template["failure"]),
            rewards=[],
        )

    def difficulty_class(self, difficulty: str) -> int:
        """Map a difficulty label onto the pack's DC table."""
        getter = getattr(self.rules, "difficulty_dc", None)
        if getter is not None:
            return int(getter(difficulty))
        return {"easy": 10, "moderate": 15, "hard": 20, "deadly": 25}.get(difficulty, 15)

    def random_kind(self, *, allow_combat: bool = True) -> EncounterKind:
        """Roll for what kind of thing happens next."""
        pool = list(NON_COMBAT_TEMPLATES)
        if allow_combat:
            pool = [EncounterKind.COMBAT, *pool]
        return pool[self.dice.rng.randrange(len(pool))]


__all__ = [
    "BUDGET_TOLERANCE",
    "GROUP_MULTIPLIERS",
    "NON_COMBAT_TEMPLATES",
    "Budget",
    "EncounterError",
    "EncounterGenerator",
    "group_multiplier",
    "party_threshold",
]
