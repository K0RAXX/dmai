"""Instantiating creatures from a rules pack's bestiary.

A stat block is a template; a `Creature` is a thing standing in the room with
its own hit points and its own name.  This module makes one from the other,
including the "Goblin 2" numbering that keeps a fight readable.
"""

from __future__ import annotations

from ..dice import DiceEngine
from ..models.base import Ability, DamageType, new_id
from ..models.character import (
    Abilities,
    Creature,
    CreatureKind,
    HitPoints,
    Personality,
)
from ..models.world import NPC
from ..rules.base import RulesEngine


class BestiaryError(ValueError):
    """The requested creature is not in the active rules pack."""


def spawn_monster(
    rules: RulesEngine,
    key: str,
    *,
    name: str | None = None,
    dice: DiceEngine | None = None,
    creature_id: str | None = None,
) -> Creature:
    """Build one monster from its stat block.

    Pass a `DiceEngine` to roll hit points from the stat block's hit dice;
    without one the block's average is used, which is what a DM does when
    they want a predictable fight.
    """
    block = rules.monster(key)
    if block is None:
        raise BestiaryError(f"no creature {key!r} in rules pack {rules.id}")

    if dice is not None and block.get("hp"):
        maximum = max(1, dice.roll(block["hp"], reason=f"{key} hit points").total)
    else:
        maximum = int(block.get("hp_average", 1))

    return Creature(
        id=creature_id or new_id(key.replace("_", "-")),
        name=name or block.get("name", key.title()),
        kind=CreatureKind.MONSTER,
        species=block.get("name", key),
        level=1,
        abilities=Abilities(**block.get("abilities", {})),
        hp=HitPoints(current=maximum, maximum=maximum),
        armor_class=int(block.get("ac", 10)),
        speed=int(block.get("speed", 30)),
        proficiency_bonus=int(block.get("proficiency_bonus", 2)),
        skill_proficiencies=dict(block.get("skill_proficiencies", {})),
        saving_throw_proficiencies=[
            Ability(a) for a in block.get("saves", []) if a in Ability._value2member_map_
        ],
        resistances=[DamageType(d) for d in block.get("resistances", [])],
        vulnerabilities=[DamageType(d) for d in block.get("vulnerabilities", [])],
        immunities=[DamageType(d) for d in block.get("immunities", [])],
        attacks=list(block.get("attacks", [])),
        morale=int(block.get("morale", 50)),
        goals=list(block.get("goals", [])),
        tactics=block.get("tactics", ""),
    )


def spawn_group(
    rules: RulesEngine,
    key: str,
    count: int,
    *,
    dice: DiceEngine | None = None,
) -> list[Creature]:
    """Spawn ``count`` of a creature, numbered when there is more than one.

    Names matter here: "the wounded Goblin 3" is a sentence a DM can say, and
    a bare list of identical goblins is not.
    """
    if count < 1:
        raise BestiaryError(f"count must be at least 1, got {count}")
    block = rules.monster(key)
    if block is None:
        raise BestiaryError(f"no creature {key!r} in rules pack {rules.id}")

    base = block.get("name", key.title())
    return [
        spawn_monster(
            rules,
            key,
            name=base if count == 1 else f"{base} {index + 1}",
            dice=dice,
        )
        for index in range(count)
    ]


def spawn_npc(
    rules: RulesEngine,
    name: str,
    *,
    role: str = "",
    template: str = "guard",
    location_id: str | None = None,
    personality: Personality | None = None,
    dialogue_style: str = "",
    knowledge: list[str] | None = None,
    npc_id: str | None = None,
    dice: DiceEngine | None = None,
) -> NPC:
    """A named NPC built on a stat-block template.

    The template supplies the numbers so a bartender who ends up in a brawl
    has real statistics; everything above them is characterisation.
    """
    base = spawn_monster(rules, template, name=name, dice=dice)
    return NPC(
        id=npc_id or new_id(name.split()[0].lower() or "npc"),
        name=name,
        kind=CreatureKind.NPC,
        species=base.species,
        abilities=base.abilities,
        hp=base.hp,
        armor_class=base.armor_class,
        speed=base.speed,
        proficiency_bonus=base.proficiency_bonus,
        skill_proficiencies=base.skill_proficiencies,
        attacks=base.attacks,
        morale=base.morale,
        role=role,
        personality=personality or Personality(),
        dialogue_style=dialogue_style,
        knowledge=list(knowledge or []),
        location_id=location_id,
    )


__all__ = ["BestiaryError", "spawn_group", "spawn_monster", "spawn_npc"]
