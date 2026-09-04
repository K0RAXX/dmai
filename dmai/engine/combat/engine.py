"""The combat subsystem (spec section 11).

Everything here writes through a `Journal`, so a fight leaves behind a
complete, replayable record: the initiative rolls, every attack roll against a
stated armour class, every point of damage, every death save.

Three decisions worth knowing about:

* **Nothing is mutated directly.**  Damage is computed on a throwaway copy of
  the target using the ruleset's own resistance logic, and the *resulting* hit
  points are written into the event.  Replay is therefore exact.
* **Zero hit points is not death for a player character.**  Players fall
  unconscious and start making death saves; monsters and NPCs die.  Massive
  damage kills either outright, as the SRD says.
* **The turn order is the source of truth for whose turn it is,** and it is
  advanced only by `advance_turn`, which also expires conditions and forces
  death saves.  No other code moves the pointer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..dice import DiceEngine
from ..models.base import (
    Ability,
    Condition,
    ConditionType,
    DamageType,
    Visibility,
    new_id,
)
from ..models.character import Creature, CreatureKind
from ..models.combat import Combatant, CombatState
from ..models.dice import DiceResult, Outcome, RollMode
from ..models.events import EventType, GameEvent
from ..rules.base import RulesEngine
from ..state import Journal

#: Conditions that take a creature out of the fight entirely.
INCAPACITATING = {
    ConditionType.UNCONSCIOUS,
    ConditionType.PARALYZED,
    ConditionType.PETRIFIED,
    ConditionType.STUNNED,
    ConditionType.INCAPACITATED,
}

#: Conditions that give attackers advantage against the sufferer.
GIVES_ATTACKER_ADVANTAGE = {
    ConditionType.BLINDED,
    ConditionType.PARALYZED,
    ConditionType.PETRIFIED,
    ConditionType.RESTRAINED,
    ConditionType.STUNNED,
    ConditionType.UNCONSCIOUS,
    ConditionType.PRONE,
}

DEATH_SAVE_DC = 10


class CombatError(RuntimeError):
    """The fight was asked to do something the rules do not allow."""


@dataclass
class AttackOutcome:
    """What one attack did, in the order a DM would say it out loud."""

    attacker_id: str
    target_id: str
    attack_roll: DiceResult
    hit: bool
    critical: bool = False
    damage_roll: DiceResult | None = None
    damage_dealt: int = 0
    damage_type: DamageType | None = None
    target_dropped: bool = False
    target_died: bool = False
    events: list[GameEvent] = field(default_factory=list)

    def describe(self) -> str:
        lines = [self.attack_roll.describe()]
        if not self.hit:
            lines.append("MISS")
            return "\n".join(lines)
        lines.append("CRITICAL HIT" if self.critical else "HIT")
        if self.damage_roll is not None:
            lines.append(self.damage_roll.describe())
        lines.append(f"Damage dealt: {self.damage_dealt}")
        if self.target_died:
            lines.append("TARGET SLAIN")
        elif self.target_dropped:
            lines.append("TARGET DOWN")
        return "\n".join(lines)


class CombatEngine:
    """Runs fights.  Holds no state of its own beyond the rules and the dice."""

    def __init__(self, rules: RulesEngine, dice: DiceEngine):
        self.rules = rules
        self.dice = dice

    # --- helpers -----------------------------------------------------------

    def _require(self, journal: Journal, creature_id: str) -> Creature:
        creature = journal.state.creature(creature_id)
        if creature is None:
            raise CombatError(f"no such creature in this campaign: {creature_id}")
        return creature

    @staticmethod
    def can_act(creature: Creature) -> bool:
        """Alive, conscious, and not held by an incapacitating condition."""
        if not creature.can_act:
            return False
        return not any(c.type in INCAPACITATING for c in creature.conditions)

    def _combatant(self, combat: CombatState, creature_id: str) -> Combatant | None:
        return next((c for c in combat.order if c.creature_id == creature_id), None)

    def active_combatants(self, journal: Journal) -> list[Combatant]:
        """Everyone still in the fight and able to take a turn."""
        out = []
        for combatant in journal.state.combat.order:
            if not combatant.active:
                continue
            creature = journal.state.creature(combatant.creature_id)
            if creature is not None and self.can_act(creature):
                out.append(combatant)
        return out

    def sides(self, journal: Journal) -> tuple[list[Combatant], list[Combatant]]:
        """(players, opposition) among those still able to fight."""
        live = self.active_combatants(journal)
        return (
            [c for c in live if c.is_player],
            [c for c in live if not c.is_player],
        )

    def is_over(self, journal: Journal) -> bool:
        """A fight ends when one side can no longer act."""
        players, enemies = self.sides(journal)
        return not players or not enemies

    # --- starting and ending ----------------------------------------------

    def start(
        self,
        journal: Journal,
        participant_ids: list[str],
        *,
        encounter_id: str | None = None,
        surprised_ids: list[str] | None = None,
    ) -> CombatState:
        """Roll initiative and open the fight.

        Initiative is 1d20 + dexterity modifier, and ties break on the
        dexterity modifier itself so the order is deterministic under a seed.
        """
        if journal.state.combat.active:
            raise CombatError("combat is already running; end it before starting another")
        if len(participant_ids) < 2:
            raise CombatError("a fight needs at least two participants")

        surprised = set(surprised_ids or [])
        rolls: list[tuple[Combatant, DiceResult]] = []

        for creature_id in participant_ids:
            creature = self._require(journal, creature_id)
            dexterity = creature.abilities.modifier(Ability.DEX)
            result = self.dice.roll(
                f"1d20{dexterity:+d}" if dexterity else "1d20",
                reason=f"{creature.name} initiative",
                actor_id=creature_id,
            )
            rolls.append(
                (
                    Combatant(
                        creature_id=creature_id,
                        initiative=result.total,
                        initiative_tiebreak=dexterity,
                        is_player=creature.kind is CreatureKind.PLAYER,
                        movement_remaining=creature.speed,
                        surprised=creature_id in surprised,
                    ),
                    result,
                )
            )

        order = [
            combatant
            for combatant, _ in sorted(
                rolls,
                key=lambda pair: (
                    pair[0].initiative,
                    pair[0].initiative_tiebreak,
                    pair[0].is_player,
                ),
                reverse=True,
            )
        ]

        combat = CombatState(
            id=new_id("combat"),
            active=True,
            round=1,
            turn_index=0,
            order=order,
            encounter_id=encounter_id,
        )

        journal.record(
            EventType.COMBAT_STARTED,
            summary="Combat begins.",
            combat=combat.model_dump(mode="json"),
            encounter_id=encounter_id,
            initiative=[
                {
                    "creature_id": combatant.creature_id,
                    "name": self._require(journal, combatant.creature_id).name,
                    "total": combatant.initiative,
                    "roll": result.model_dump(mode="json"),
                }
                for combatant, result in rolls
            ],
        )
        self._begin_turn(journal)
        return journal.state.combat

    def end(self, journal: Journal, reason: str = "", clear_bestiary: bool = True) -> None:
        """Close the fight and, by default, sweep away the slain."""
        combat = journal.state.combat.model_copy(deep=True)
        combat.active = False
        journal.record(
            EventType.COMBAT_ENDED,
            summary=reason or "Combat ends.",
            reason=reason,
            combat=combat.model_dump(mode="json"),
            clear_bestiary=clear_bestiary,
            rounds=combat.round,
        )

    # --- turn order --------------------------------------------------------

    def _begin_turn(self, journal: Journal, combat: CombatState | None = None) -> Combatant | None:
        """Open a combatant's turn: refresh their action economy and log it.

        ``combat`` is the turn-order state to open, with the pointer already
        moved; the TURN_STARTED event carries it, so moving the pointer and
        starting the turn are one atomic entry in the log rather than two.

        A surprised creature loses this turn instead of taking it, which is
        recorded rather than silently skipped so the log explains the gap.
        """
        combat = (combat or journal.state.combat).model_copy(deep=True)
        if not combat.active or not combat.order:
            return None

        combatant = combat.order[combat.turn_index % len(combat.order)]
        creature = journal.state.creature(combatant.creature_id)

        combatant.has_acted = False
        combatant.action_used = False
        combatant.bonus_action_used = False
        combatant.reaction_used = False
        combatant.movement_remaining = creature.speed if creature else 30

        skipped = ""
        if combatant.surprised:
            combatant.surprised = False
            skipped = "surprised"
        elif creature is not None and not self.can_act(creature):
            skipped = "unable to act"

        journal.record(
            EventType.TURN_STARTED,
            actor_id=combatant.creature_id,
            summary=(
                f"{creature.name if creature else combatant.creature_id}: "
                f"turn skipped ({skipped})."
                if skipped
                else f"{creature.name if creature else combatant.creature_id}'s turn."
            ),
            combat=combat.model_dump(mode="json"),
            round=combat.round,
            skipped=skipped,
        )

        # A dying character makes a death save at the start of their turn.
        if creature is not None and creature.is_dying and creature.kind is CreatureKind.PLAYER:
            if not creature.death_saves.stable:
                self.death_save(journal, combatant.creature_id)

        if skipped:
            return self.advance_turn(journal)
        return combatant

    def current(self, journal: Journal) -> Creature | None:
        combatant = journal.state.combat.current
        if combatant is None:
            return None
        return journal.state.creature(combatant.creature_id)

    def advance_turn(self, journal: Journal) -> Combatant | None:
        """End this turn and open the next one, wrapping into a new round.

        Returns the combatant now on turn, or ``None`` if the fight is over.
        """
        combat = journal.state.combat.model_copy(deep=True)
        if not combat.active or not combat.order:
            return None

        ending = combat.order[combat.turn_index % len(combat.order)]
        ending.has_acted = True
        journal.record(
            EventType.TURN_ENDED,
            actor_id=ending.creature_id,
            combat=combat.model_dump(mode="json"),
        )
        self._expire_conditions(journal, ending.creature_id)

        if self.is_over(journal):
            self.end(journal, reason="One side can no longer fight.")
            return None

        combat = journal.state.combat.model_copy(deep=True)
        size = len(combat.order)
        start = combat.turn_index

        # Walk forward to the next combatant who can still take a turn.  Each
        # time the walk passes the top of the order, a round has elapsed --
        # which is how a fight where half the order is unconscious still
        # counts rounds correctly.
        for step in range(1, size + 1):
            index = (start + step) % size
            candidate = combat.order[index]
            creature = journal.state.creature(candidate.creature_id)
            if not candidate.active or creature is None or creature.dead:
                continue
            combat.turn_index = index
            combat.round += (start + step) // size
            break
        else:
            self.end(journal, reason="No combatant can act.")
            return None

        return self._begin_turn(journal, combat)

    def _expire_conditions(self, journal: Journal, creature_id: str) -> None:
        """Tick down timed conditions at the end of a creature's turn."""
        creature = journal.state.creature(creature_id)
        if creature is None:
            return
        for condition in list(creature.conditions):
            if condition.duration_rounds is None:
                continue
            remaining = condition.duration_rounds - 1
            if remaining <= 0:
                journal.record(
                    EventType.CONDITION_REMOVED,
                    target_id=creature_id,
                    creature_id=creature_id,
                    condition_type=condition.type.value,
                    summary=f"{creature.name} is no longer {condition.type.value}.",
                )
            else:
                refreshed = condition.model_copy(update={"duration_rounds": remaining})
                journal.record(
                    EventType.CONDITION_ADDED,
                    target_id=creature_id,
                    creature_id=creature_id,
                    condition=refreshed.model_dump(mode="json"),
                )

    # --- attacks -----------------------------------------------------------

    def attack_mode(self, attacker: Creature, target: Creature) -> RollMode:
        """Advantage from the target's condition; disadvantage from the attacker's."""
        advantage = any(c.type in GIVES_ATTACKER_ADVANTAGE for c in target.conditions)
        disadvantage = any(
            c.type in {ConditionType.BLINDED, ConditionType.POISONED, ConditionType.FRIGHTENED}
            for c in attacker.conditions
        ) or attacker.has_condition(ConditionType.PRONE)
        if advantage and not disadvantage:
            return RollMode.ADVANTAGE
        if disadvantage and not advantage:
            return RollMode.DISADVANTAGE
        return RollMode.NORMAL

    def attack(
        self,
        journal: Journal,
        attacker_id: str,
        target_id: str,
        weapon_key: str | None = None,
        *,
        mode: RollMode | None = None,
        spend_action: bool = True,
    ) -> AttackOutcome:
        """Resolve one attack: roll, compare to armour class, deal damage."""
        attacker = self._require(journal, attacker_id)
        target = self._require(journal, target_id)

        if not self.can_act(attacker):
            raise CombatError(f"{attacker.name} cannot act")
        if target.dead:
            raise CombatError(f"{target.name} is already dead")

        if weapon_key is None:
            weapon_key = self._default_weapon(attacker)

        bonus = self.rules.attack_bonus(attacker, weapon_key)
        armor_class = self.rules.armor_class(target)
        roll_mode = mode or self.attack_mode(attacker, target)

        attack_roll = self.dice.roll(
            f"1d20{bonus:+d}" if bonus else "1d20",
            reason=f"{attacker.name} attacks {target.name}",
            mode=roll_mode,
            dc=armor_class,
            actor_id=attacker_id,
            target_id=target_id,
        )

        critical = attack_roll.outcome is Outcome.CRITICAL_SUCCESS
        auto_miss = attack_roll.outcome is Outcome.CRITICAL_FAILURE
        hit = critical or (not auto_miss and attack_roll.total >= armor_class)

        outcome = AttackOutcome(
            attacker_id=attacker_id,
            target_id=target_id,
            attack_roll=attack_roll,
            hit=hit,
            critical=critical,
        )
        outcome.events.append(
            journal.record(
                EventType.ATTACK,
                actor_id=attacker_id,
                target_id=target_id,
                summary=(
                    f"{attacker.name} attacks {target.name} with "
                    f"{self._weapon_name(attacker, weapon_key)}: "
                    f"{'critical hit' if critical else 'hit' if hit else 'miss'}."
                ),
                weapon=weapon_key,
                armor_class=armor_class,
                hit=hit,
                critical=critical,
                roll=attack_roll.model_dump(mode="json"),
            )
        )

        if spend_action:
            self._spend_action(journal, attacker_id)

        if not hit:
            return outcome

        expression, damage_type = self.rules.damage_expression(
            attacker, weapon_key, critical=critical
        )
        damage_roll = self.dice.roll(
            expression,
            reason=f"{self._weapon_name(attacker, weapon_key)} damage",
            actor_id=attacker_id,
            target_id=target_id,
        )
        outcome.damage_roll = damage_roll
        outcome.damage_type = damage_type

        dealt, events, dropped, died = self.deal_damage(
            journal,
            target_id,
            max(0, damage_roll.total),
            damage_type,
            source_id=attacker_id,
        )
        outcome.damage_dealt = dealt
        outcome.events.extend(events)
        outcome.target_dropped = dropped
        outcome.target_died = died
        return outcome

    def _default_weapon(self, creature: Creature) -> str | None:
        """What this creature swings when nobody said.

        Stat-block attacks win (a wolf bites), then an equipped weapon, then
        fists.
        """
        if creature.attacks:
            return f"@{creature.attacks[0].get('name', '')}"
        for item in creature.inventory:
            if item.equipped and item.kind.value == "weapon":
                key = item.properties.get("key")
                if key:
                    return str(key)
        return "unarmed"

    def _weapon_name(self, creature: Creature, weapon_key: str | None) -> str:
        if weapon_key and weapon_key.startswith("@"):
            return weapon_key[1:]
        weapon = self.rules.weapon(weapon_key) if weapon_key else None
        return weapon["name"] if weapon else "a bare fist"

    def _spend_action(self, journal: Journal, creature_id: str) -> None:
        combat = journal.state.combat
        if not combat.active:
            return
        updated = combat.model_copy(deep=True)
        combatant = self._combatant(updated, creature_id)
        if combatant is None:
            return
        combatant.action_used = True
        journal.record(
            EventType.COMBAT_UPDATED,
            actor_id=creature_id,
            combat=updated.model_dump(mode="json"),
            action_used=True,
        )

    # --- damage, healing, death -------------------------------------------

    def deal_damage(
        self,
        journal: Journal,
        target_id: str,
        amount: int,
        damage_type: DamageType,
        *,
        source_id: str | None = None,
        reason: str = "",
    ) -> tuple[int, list[GameEvent], bool, bool]:
        """Apply damage.  Returns (dealt, events, dropped, died).

        The ruleset's resistance maths is run against a throwaway copy so the
        event can carry the resulting hit points rather than a delta.
        """
        target = self._require(journal, target_id)
        probe = target.model_copy(deep=True)
        dealt = self.rules.apply_damage(probe, amount, damage_type)

        events = [
            journal.record(
                EventType.DAMAGE_DEALT,
                actor_id=source_id,
                target_id=target_id,
                creature_id=target_id,
                summary=f"{target.name} takes {dealt} {damage_type.value} damage.",
                amount=dealt,
                requested=amount,
                damage_type=damage_type.value,
                reason=reason,
                hp_current=probe.hp.current,
                hp_temporary=probe.hp.temporary,
            )
        ]

        if probe.hp.current > 0:
            return dealt, events, False, False

        # Overkill: damage past zero equal to the maximum is instant death.
        overkill = dealt - (target.hp.current + target.hp.temporary)
        instant = overkill >= target.hp.maximum
        is_player = target.kind is CreatureKind.PLAYER

        if instant or not is_player:
            events.append(self._kill(journal, target_id, source_id=source_id))
            return dealt, events, True, True

        events.append(
            journal.record(
                EventType.CONDITION_ADDED,
                target_id=target_id,
                creature_id=target_id,
                summary=f"{target.name} falls unconscious and begins dying.",
                condition=Condition(
                    type=ConditionType.UNCONSCIOUS, source="zero hit points"
                ).model_dump(mode="json"),
            )
        )
        events.append(
            journal.record(
                EventType.DEATH_SAVE,
                target_id=target_id,
                creature_id=target_id,
                successes=0,
                failures=0,
                stable=False,
                summary=f"{target.name} is dying.",
            )
        )
        return dealt, events, True, False

    def _kill(self, journal: Journal, creature_id: str, source_id: str | None = None) -> GameEvent:
        creature = self._require(journal, creature_id)
        event = journal.record(
            EventType.CREATURE_DIED,
            actor_id=source_id,
            target_id=creature_id,
            creature_id=creature_id,
            summary=f"{creature.name} dies.",
        )
        self._retire_from_order(journal, creature_id)
        return event

    def _retire_from_order(self, journal: Journal, creature_id: str) -> None:
        """A dead combatant stops taking turns but stays in the log."""
        combat = journal.state.combat
        if not combat.active:
            return
        updated = combat.model_copy(deep=True)
        combatant = self._combatant(updated, creature_id)
        if combatant is None:
            return
        combatant.has_acted = True
        journal.record(
            EventType.COMBAT_UPDATED,
            target_id=creature_id,
            combat=updated.model_dump(mode="json"),
            retired=True,
        )

    def heal(
        self,
        journal: Journal,
        target_id: str,
        amount: int,
        *,
        source_id: str | None = None,
        reason: str = "healing",
    ) -> int:
        """Restore hit points.  A healed dying character wakes up."""
        target = self._require(journal, target_id)
        if target.dead:
            raise CombatError(f"{target.name} is dead; healing will not help")

        probe = target.model_copy(deep=True)
        restored = self.rules.heal(probe, amount)
        was_dying = target.is_dying

        journal.record(
            EventType.HEALING,
            actor_id=source_id,
            target_id=target_id,
            creature_id=target_id,
            summary=f"{target.name} recovers {restored} hit points.",
            amount=restored,
            reason=reason,
            hp_current=probe.hp.current,
            hp_temporary=probe.hp.temporary,
            death_saves_reset=True,
        )

        if was_dying and probe.hp.current > 0:
            journal.record(
                EventType.CONDITION_REMOVED,
                target_id=target_id,
                creature_id=target_id,
                condition_type=ConditionType.UNCONSCIOUS.value,
                summary=f"{target.name} regains consciousness.",
            )
        return restored

    def death_save(self, journal: Journal, creature_id: str) -> DiceResult:
        """One death saving throw.  Three of either ends the question.

        A natural 20 puts the character back on their feet with one hit point;
        a natural 1 counts as two failures.
        """
        creature = self._require(journal, creature_id)
        if not creature.is_dying:
            raise CombatError(f"{creature.name} is not dying")

        result = self.dice.roll(
            "1d20",
            reason=f"{creature.name} death saving throw",
            dc=DEATH_SAVE_DC,
            actor_id=creature_id,
        )
        successes = creature.death_saves.successes
        failures = creature.death_saves.failures

        if result.outcome is Outcome.CRITICAL_SUCCESS:
            journal.record(
                EventType.DEATH_SAVE,
                target_id=creature_id,
                creature_id=creature_id,
                successes=0,
                failures=0,
                stable=False,
                roll=result.model_dump(mode="json"),
                summary=f"{creature.name} rallies on a natural 20.",
            )
            self.heal(journal, creature_id, 1, reason="rallied on a natural 20")
            return result

        if result.outcome is Outcome.CRITICAL_FAILURE:
            failures += 2
        elif result.total >= DEATH_SAVE_DC:
            successes += 1
        else:
            failures += 1

        stable = successes >= 3
        journal.record(
            EventType.DEATH_SAVE,
            target_id=creature_id,
            creature_id=creature_id,
            successes=min(successes, 3),
            failures=min(failures, 3),
            stable=stable,
            roll=result.model_dump(mode="json"),
            summary=(
                f"{creature.name} stabilises."
                if stable
                else f"{creature.name}: {successes} successes, {failures} failures."
            ),
        )
        if failures >= 3:
            self._kill(journal, creature_id)
        return result

    # --- checks and saves --------------------------------------------------

    def saving_throw(
        self,
        journal: Journal,
        creature_id: str,
        ability: Ability,
        dc: int,
        *,
        reason: str = "",
        mode: RollMode = RollMode.NORMAL,
    ) -> DiceResult:
        """A saving throw, logged as a save rather than a bare roll."""
        creature = self._require(journal, creature_id)
        bonus = self.rules.save_modifier(creature, ability)
        result = self.dice.roll(
            f"1d20{bonus:+d}" if bonus else "1d20",
            reason=reason or f"{creature.name} {ability.value} save",
            mode=mode,
            dc=dc,
            actor_id=creature_id,
        )
        journal.record(
            EventType.SAVE_RESOLVED,
            actor_id=creature_id,
            summary=result.describe(),
            ability=ability.value,
            roll=result.model_dump(mode="json"),
        )
        return result

    # --- morale and disengagement -----------------------------------------

    def morale_check(self, journal: Journal, creature_id: str) -> bool:
        """Does this creature keep fighting?  (Spec section 11.)

        Enemies are not damage sponges: a bandit whose friends are dead and
        whose own blood is on the floor would rather be elsewhere.  A high
        `morale` resists that; a roll under the adjusted score holds the line.
        """
        creature = self._require(journal, creature_id)
        if creature.kind is CreatureKind.PLAYER:
            return True

        morale = creature.morale
        if creature.hp.maximum:
            wounded = 1 - (creature.hp.current / creature.hp.maximum)
            morale -= int(wounded * 40)

        players, enemies = self.sides(journal)
        if enemies and len(enemies) == 1:
            morale -= 15
        if players and enemies and len(players) > len(enemies) * 2:
            morale -= 15

        result = self.dice.roll(
            "d%", reason=f"{creature.name} morale", dc=max(1, morale), actor_id=creature_id
        )
        holds = result.total <= max(1, morale)
        journal.record(
            EventType.DICE_ROLLED,
            actor_id=creature_id,
            # Players should feel a bandit waver, not read the die that decided it.
            visibility=Visibility.DM_ONLY,
            summary=f"{creature.name} morale: {'holds' if holds else 'breaks'}.",
            roll=result.model_dump(mode="json"),
            adjusted_morale=max(1, morale),
            holds=holds,
        )
        return holds

    def flee(self, journal: Journal, creature_id: str, reason: str = "") -> None:
        creature = self._require(journal, creature_id)
        updated = journal.state.combat.model_copy(deep=True)
        combatant = self._combatant(updated, creature_id)
        if combatant is None:
            raise CombatError(f"{creature.name} is not in this fight")
        combatant.fled = True
        journal.record(
            EventType.ENEMY_FLED,
            actor_id=creature_id,
            summary=f"{creature.name} flees{f': {reason}' if reason else '.'}",
            combat=updated.model_dump(mode="json"),
            reason=reason,
        )

    def surrender(self, journal: Journal, creature_id: str, terms: str = "") -> None:
        creature = self._require(journal, creature_id)
        updated = journal.state.combat.model_copy(deep=True)
        combatant = self._combatant(updated, creature_id)
        if combatant is None:
            raise CombatError(f"{creature.name} is not in this fight")
        combatant.surrendered = True
        journal.record(
            EventType.ENEMY_SURRENDERED,
            actor_id=creature_id,
            summary=f"{creature.name} surrenders{f': {terms}' if terms else '.'}",
            combat=updated.model_dump(mode="json"),
            terms=terms,
        )


__all__ = ["DEATH_SAVE_DC", "AttackOutcome", "CombatEngine", "CombatError"]
