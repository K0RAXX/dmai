"""Character creation (spec section 6).

The builder turns a small, human-sized description -- a name, a species, a
class, a level -- into a fully derived `Character`: hit points, armour class,
proficiency bonus, saving throws and starting gear, all read from the active
rules pack rather than hard-coded here.

Everything is pure: the caller supplies the rules and, for rolled ability
scores, a seeded `DiceEngine`.  The same inputs always give the same sheet.
"""

from __future__ import annotations

from enum import Enum

from ..dice import DiceEngine
from ..models.base import Ability, new_id
from ..models.character import (
    Abilities,
    Character,
    Creature,
    CreatureKind,
    HitPoints,
    Item,
    ItemKind,
    Personality,
)
from ..rules.base import RulesEngine

#: The SRD standard array, highest first.
STANDARD_ARRAY = [15, 14, 13, 12, 10, 8]

#: Point-buy cost of each score, and the default budget.
POINT_BUY_COST = {8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9}
POINT_BUY_BUDGET = 27

#: Ability order used when nothing better is known, after the class primaries.
FALLBACK_PRIORITY = [
    Ability.CON,
    Ability.DEX,
    Ability.WIS,
    Ability.STR,
    Ability.CHA,
    Ability.INT,
]


class ScoreMethod(str, Enum):
    STANDARD = "standard"     # the standard array, assigned by class priority
    ROLLED = "rolled"         # 4d6 drop lowest, six times
    POINT_BUY = "point_buy"   # 27 points, 8-15 before species bonuses
    MANUAL = "manual"         # exactly what the caller passed


class CharacterError(ValueError):
    """The requested character cannot be built from this rules pack."""


def roll_ability_scores(dice: DiceEngine) -> list[int]:
    """Six scores, 4d6 keep the best three, highest first."""
    scores = []
    for _ in range(6):
        result = dice.roll("4d6", reason="ability score")
        values = sorted((d.value for d in result.dice), reverse=True)
        scores.append(sum(values[:3]))
    return sorted(scores, reverse=True)


def point_buy_cost(scores: dict[Ability, int]) -> int:
    """Total point-buy cost.  Raises if any score is outside the legal range."""
    total = 0
    for ability, score in scores.items():
        if score not in POINT_BUY_COST:
            raise CharacterError(
                f"{ability.value} is {score}; point buy allows 8-15 before species bonuses"
            )
        total += POINT_BUY_COST[score]
    return total


def _ability_priority(rules: RulesEngine, class_key: str) -> list[Ability]:
    """Which abilities this class wants first, from the rules pack."""
    klass = rules.character_class(class_key) or {}
    order: list[Ability] = []
    for name in klass.get("primary", []):
        try:
            ability = Ability(name)
        except ValueError:
            continue
        if ability not in order:
            order.append(ability)
    for ability in FALLBACK_PRIORITY:
        if ability not in order:
            order.append(ability)
    return order


def assign_scores(
    values: list[int], priority: list[Ability]
) -> dict[Ability, int]:
    """Hand out an ordered list of numbers to abilities in priority order."""
    if len(values) < 6:
        raise CharacterError(f"need six ability scores, got {len(values)}")
    return dict(zip(priority, sorted(values, reverse=True), strict=False))


def build_abilities(
    rules: RulesEngine,
    class_key: str,
    species_key: str,
    method: ScoreMethod = ScoreMethod.STANDARD,
    scores: dict[str, int] | None = None,
    dice: DiceEngine | None = None,
) -> Abilities:
    """Produce final ability scores, species bonuses included."""
    if method is ScoreMethod.MANUAL or scores:
        if not scores:
            raise CharacterError("manual ability scores require a scores mapping")
        chosen = {Ability(k): int(v) for k, v in scores.items()}
        if method is ScoreMethod.POINT_BUY:
            spent = point_buy_cost(chosen)
            if spent > POINT_BUY_BUDGET:
                raise CharacterError(
                    f"point buy spends {spent} points; the budget is {POINT_BUY_BUDGET}"
                )
    else:
        priority = _ability_priority(rules, class_key)
        if method is ScoreMethod.ROLLED:
            if dice is None:
                raise CharacterError("rolled ability scores require a DiceEngine")
            values = roll_ability_scores(dice)
        else:
            values = list(STANDARD_ARRAY)
        chosen = assign_scores(values, priority)

    final = {ability: chosen.get(ability, 10) for ability in Ability}

    species = rules.species(species_key) or {}
    for name, bonus in species.get("ability_bonuses", {}).items():
        try:
            ability = Ability(name)
        except ValueError:
            continue
        final[ability] = final[ability] + int(bonus)

    return Abilities(**{ability.value: score for ability, score in final.items()})


def _starting_equipment(rules: RulesEngine, class_key: str) -> list[Item]:
    """Turn the class's starting-equipment keys into inventory items.

    Weapons and armour come back equipped, because a fighter who has to be
    told to hold their own sword is a bad first impression.
    """
    klass = rules.character_class(class_key) or {}
    items: list[Item] = []
    for key in klass.get("starting_equipment", []):
        weapon = rules.weapon(key)
        if weapon is not None:
            items.append(
                Item(
                    name=weapon["name"],
                    kind=ItemKind.WEAPON,
                    weight=float(weapon.get("weight", 0)),
                    value_gp=float(weapon.get("cost_gp", 0)),
                    equipped=True,
                    properties={"key": key, **weapon},
                )
            )
            continue
        armor = rules.armor(key)
        if armor is not None:
            items.append(
                Item(
                    name=armor["name"],
                    kind=ItemKind.SHIELD if armor.get("type") == "shield" else ItemKind.ARMOR,
                    weight=float(armor.get("weight", 0)),
                    value_gp=float(armor.get("cost_gp", 0)),
                    equipped=True,
                    properties={"key": key, **armor},
                )
            )
            continue
        items.append(Item(name=key.replace("_", " ").title(), properties={"key": key}))
    return items


def _skill_choices(rules: RulesEngine, class_key: str, requested: list[str]) -> dict[str, int]:
    """Validate skill proficiencies against the class list.

    A skill the class cannot take is an error rather than a silent drop: a
    character sheet that quietly loses a proficiency is worse than a refusal.
    """
    klass = rules.character_class(class_key) or {}
    allowed = set(klass.get("skills", []))
    limit = int(klass.get("skill_count", 2))

    if not requested:
        requested = sorted(allowed)[:limit]
    if len(requested) > limit:
        raise CharacterError(
            f"{class_key} may choose {limit} skills; {len(requested)} were given"
        )
    unavailable = [s for s in requested if allowed and s not in allowed]
    if unavailable:
        raise CharacterError(
            f"{class_key} cannot take {', '.join(unavailable)}; "
            f"available: {', '.join(sorted(allowed))}"
        )
    return {skill: 1 for skill in requested}


def create_character(
    rules: RulesEngine,
    name: str,
    species: str = "human",
    character_class: str = "fighter",
    level: int = 1,
    *,
    method: ScoreMethod = ScoreMethod.STANDARD,
    scores: dict[str, int] | None = None,
    skills: list[str] | None = None,
    dice: DiceEngine | None = None,
    pronouns: str = "they/them",
    background: str = "",
    personality: Personality | None = None,
    player_id: str | None = None,
    character_id: str | None = None,
) -> Character:
    """Build a complete, playable character sheet."""
    if not name.strip():
        raise CharacterError("a character needs a name")
    if level < 1:
        raise CharacterError(f"level must be at least 1, got {level}")
    if rules.character_class(character_class) is None:
        raise CharacterError(f"unknown class for {rules.id}: {character_class}")
    if rules.species(species) is None:
        raise CharacterError(f"unknown species for {rules.id}: {species}")

    abilities = build_abilities(
        rules, character_class, species, method=method, scores=scores, dice=dice
    )
    klass = rules.character_class(character_class) or {}
    species_data = rules.species(species) or {}

    proficiency = rules.proficiency_bonus(level)
    con_modifier = abilities.modifier(Ability.CON)
    maximum_hp = rules.max_hit_points(character_class, level, con_modifier)

    saves = []
    for save_name in klass.get("saves", []):
        try:
            saves.append(Ability(save_name))
        except ValueError:
            continue

    character = Character(
        id=character_id or new_id(name.split()[0].lower() or "pc"),
        name=name.strip(),
        kind=CreatureKind.PLAYER,
        level=level,
        species=species_data.get("name", species),
        abilities=abilities,
        hp=HitPoints(current=maximum_hp, maximum=maximum_hp),
        speed=int(species_data.get("speed", 30)),
        proficiency_bonus=proficiency,
        skill_proficiencies=_skill_choices(rules, character_class, list(skills or [])),
        saving_throw_proficiencies=saves,
        inventory=_starting_equipment(rules, character_class),
        pronouns=pronouns,
        background=background,
        personality=personality or Personality(),
        player_id=player_id,
        **{"class": klass.get("name", character_class)},
    )
    # Armour class is derived from what is equipped, so it is computed after
    # the gear is in place rather than guessed during construction.
    character.armor_class = rules.armor_class(character)
    return character


def level_up(rules: RulesEngine, character: Character, class_key: str | None = None) -> dict:
    """Advance one level.  Returns the LEVEL_UP event payload, not a mutation.

    The engine records the new values as an event; the reducer applies them.
    Nothing here touches the character it was handed.
    """
    new_level = character.level + 1
    key = class_key or _class_key(rules, character)
    con_modifier = character.abilities.modifier(Ability.CON)
    maximum = rules.max_hit_points(key, new_level, con_modifier)
    gained = maximum - character.hp.maximum
    return {
        "level": new_level,
        "proficiency_bonus": rules.proficiency_bonus(new_level),
        "hp_maximum": maximum,
        "hp_current": character.hp.current + max(0, gained),
        "hp_gained": gained,
    }


def _class_key(rules: RulesEngine, creature: Creature) -> str:
    """Map a display class name ('Fighter') back to its pack key ('fighter')."""
    wanted = creature.character_class.strip().lower()
    if rules.character_class(wanted) is not None:
        return wanted
    for key in ("fighter", "wizard", "rogue", "cleric", "ranger", "barbarian"):
        klass = rules.character_class(key)
        if klass and klass.get("name", "").lower() == wanted:
            return key
    raise CharacterError(f"cannot map class {creature.character_class!r} to {rules.id}")


def export_character(character: Character) -> dict:
    """A portable sheet (spec section 6: import/export as JSON)."""
    return character.model_dump(mode="json", by_alias=True)


def import_character(payload: dict) -> Character:
    """Read a sheet back.  Validation errors surface rather than being patched."""
    return Character.model_validate(payload)


__all__ = [
    "POINT_BUY_BUDGET",
    "POINT_BUY_COST",
    "STANDARD_ARRAY",
    "CharacterError",
    "ScoreMethod",
    "build_abilities",
    "create_character",
    "export_character",
    "import_character",
    "level_up",
    "point_buy_cost",
    "roll_ability_scores",
]
