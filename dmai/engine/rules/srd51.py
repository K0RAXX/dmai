"""SRD 5.1 rules implementation.

The rules themselves live in `rules_packs/srd51/*.json`.  This class only
knows how to interpret that data, which is what lets a user drop in their own
pack without touching Python.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..models.base import SKILL_ABILITIES, Ability, DamageType
from ..models.character import Creature
from .base import RulesEngine

DEFAULT_PACK_ROOT = Path(__file__).resolve().parents[3] / "rules_packs"

_DIFFICULTY_INDEX = {"easy": 0, "moderate": 1, "medium": 1, "hard": 2, "deadly": 3}
_BODY_ARMOR_TYPES = {"light", "medium", "heavy"}


class RulesPackError(RuntimeError):
    """A rules pack is missing or malformed."""


class Srd51Rules(RulesEngine):
    id = "srd51"
    name = "SRD 5.1"

    def __init__(self, pack_dir: Path | str | None = None):
        self.pack_dir = Path(pack_dir) if pack_dir else DEFAULT_PACK_ROOT / "srd51"
        if not self.pack_dir.is_dir():
            raise RulesPackError(f"rules pack not found: {self.pack_dir}")

        self.pack = self._load("pack.json")
        self.classes = self._load("classes.json")
        self.species_data = self._load("species.json")
        self.weapons = self._load("weapons.json")
        self.armors = self._load("armor.json")
        self.monsters = self._load("monsters.json")

        self.name = self.pack.get("name", self.name)
        self.attribution = self.pack.get("attribution", "")

    def _load(self, filename: str) -> dict:
        path = self.pack_dir / filename
        if not path.is_file():
            raise RulesPackError(f"rules pack {self.pack_dir.name} is missing {filename}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RulesPackError(f"{path} is not valid JSON: {exc}") from exc

    # --- core maths --------------------------------------------------------

    def ability_modifier(self, score: int) -> int:
        return (score - 10) // 2

    def proficiency_bonus(self, level: int) -> int:
        table = self.pack["proficiency_by_level"]
        return table[min(max(level, 1), len(table)) - 1]

    def skill_modifier(self, creature: Creature, skill: str) -> int:
        ability = SKILL_ABILITIES.get(skill)
        if ability is None:
            raise KeyError(f"unknown skill: {skill}")
        multiplier = creature.skill_proficiencies.get(skill, 0)
        return creature.abilities.modifier(ability) + multiplier * creature.proficiency_bonus

    def save_modifier(self, creature: Creature, ability: Ability) -> int:
        base = creature.abilities.modifier(ability)
        if ability in creature.saving_throw_proficiencies:
            base += creature.proficiency_bonus
        return base

    def passive_score(self, creature: Creature, skill: str) -> int:
        """Passive perception and friends: 10 + modifier."""
        return 10 + self.skill_modifier(creature, skill)

    def armor_class(self, creature: Creature) -> int:
        """Recompute AC from equipped gear.

        Monsters carry a flat AC from their stat block and no equipment, so
        with nothing equipped the stored value is returned untouched.
        """
        equipped = [item for item in creature.inventory if item.equipped]
        if not equipped:
            return creature.armor_class

        body = next(
            (i for i in equipped if i.properties.get("type") in _BODY_ARMOR_TYPES), None
        )
        shield_bonus = sum(
            int(i.properties.get("ac_bonus", 0))
            for i in equipped
            if i.properties.get("type") == "shield"
        )
        dex = creature.abilities.modifier(Ability.DEX)

        if body is None:
            return 10 + dex + shield_bonus

        cap = body.properties.get("dex_cap")
        if cap is not None:
            dex = min(dex, int(cap))
        return int(body.properties.get("base_ac", 10)) + dex + shield_bonus

    def max_hit_points(self, class_key: str, level: int, con_modifier: int) -> int:
        """Fixed-average hit points: the full die at level 1, the average after."""
        klass = self.classes.get(class_key)
        die = int(klass["hit_die"]) if klass else 8
        average = die // 2 + 1
        total = die + con_modifier + (level - 1) * (average + con_modifier)
        return max(1, total)

    # --- combat ------------------------------------------------------------

    def _weapon_ability(self, creature: Creature, weapon: dict | None) -> Ability:
        """Which ability governs this weapon: ranged uses DEX, finesse the better."""
        if weapon is None:
            return Ability.STR
        properties = weapon.get("properties", [])
        if "ranged" in properties:
            return Ability.DEX
        if "finesse" in properties:
            dex = creature.abilities.modifier(Ability.DEX)
            strength = creature.abilities.modifier(Ability.STR)
            if dex > strength:
                return Ability.DEX
        return Ability.STR

    @staticmethod
    def _stat_block_attack(creature: Creature, name: str) -> dict | None:
        """Find a named attack on a monster stat block."""
        for attack in creature.attacks:
            if attack.get("name", "").lower() == name.lower():
                return attack
        return None

    def attack_bonus(self, creature: Creature, weapon_key: str | None = None) -> int:
        """Stat-block attacks carry an explicit bonus; characters compute one.

        A key prefixed with an at-sign names an attack on the stat block,
        for example "@Bite".
        """
        if weapon_key and weapon_key.startswith("@"):
            attack = self._stat_block_attack(creature, weapon_key[1:])
            if attack is not None:
                return int(attack.get("bonus", 0))

        weapon = self.weapons.get(weapon_key) if weapon_key else None
        ability = self._weapon_ability(creature, weapon)
        return creature.abilities.modifier(ability) + creature.proficiency_bonus

    def damage_expression(
        self, creature: Creature, weapon_key: str | None = None, critical: bool = False
    ) -> tuple[str, DamageType]:
        """The damage dice for an attack, already including the ability modifier."""
        if weapon_key and weapon_key.startswith("@"):
            attack = self._stat_block_attack(creature, weapon_key[1:])
            if attack is not None:
                dice = attack["damage"]
                return (
                    self.critical_dice(dice) if critical else dice,
                    DamageType(attack.get("damage_type", "bludgeoning")),
                )

        weapon = (self.weapons.get(weapon_key) if weapon_key else None) or self.weapons["unarmed"]
        modifier = creature.abilities.modifier(self._weapon_ability(creature, weapon))
        dice = weapon["damage"]
        expression = self.critical_dice(dice) if critical else dice
        if modifier:
            expression = f"{expression}{modifier:+d}"
        return expression, DamageType(weapon["damage_type"])

    @staticmethod
    def critical_dice(expression: str) -> str:
        """A critical hit doubles the dice, never the modifier."""

        def double(match: re.Match) -> str:
            count = int(match.group(1) or 1)
            return f"{count * 2}d{match.group(2)}"

        return re.sub(r"(\d*)d(\d+)", double, expression)

    def apply_damage(self, creature: Creature, amount: int, damage_type: DamageType) -> int:
        """Apply damage through temporary hit points and resistances.

        Returns the amount actually dealt after immunity, resistance and
        vulnerability, which is what the event log records.
        """
        if amount <= 0:
            return 0
        if damage_type in creature.immunities:
            return 0
        if damage_type in creature.resistances:
            amount //= 2
        if damage_type in creature.vulnerabilities:
            amount *= 2

        absorbed = min(creature.hp.temporary, amount)
        creature.hp.temporary -= absorbed
        creature.hp.current = max(0, creature.hp.current - (amount - absorbed))
        return amount

    def heal(self, creature: Creature, amount: int) -> int:
        """Heal up to the maximum.  Returns the hit points actually restored."""
        if amount <= 0:
            return 0
        before = creature.hp.current
        creature.hp.current = min(creature.hp.maximum, creature.hp.current + amount)
        if creature.hp.current > 0:
            creature.death_saves.reset()
        return creature.hp.current - before

    # --- content lookup ----------------------------------------------------

    def weapon(self, key: str) -> dict | None:
        return self.weapons.get(key)

    def armor(self, key: str) -> dict | None:
        return self.armors.get(key)

    def monster(self, key: str) -> dict | None:
        return self.monsters.get(key)

    def species(self, key: str) -> dict | None:
        return self.species_data.get(key)

    def character_class(self, key: str) -> dict | None:
        return self.classes.get(key)

    # --- encounter budgeting ----------------------------------------------

    def xp_threshold(self, level: int, difficulty: str) -> int:
        """Party XP budget for one character at this level and difficulty."""
        level = min(max(level, 1), 20)
        index = _DIFFICULTY_INDEX.get(difficulty.lower(), 1)
        return int(self.pack["xp_thresholds"][str(level)][index])

    def difficulty_dc(self, label: str) -> int:
        return int(self.pack["difficulty_dc"].get(label, 15))
