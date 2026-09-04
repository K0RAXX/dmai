"""What an enemy does on its turn (spec section 11).

The spec is explicit that enemies have goals beyond attacking until dead: a
bandit may flee, a guard may call for reinforcements, a wolf may protect the
pack, a captain may negotiate.  This module makes that a decision the engine
can reach without a language model, so a fight is playable -- and testable --
with no provider configured at all.

The DM AI is welcome to override any of it.  This is the floor, not the
ceiling: it exists so combat never stalls waiting for narration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..models.character import Creature, CreatureKind
from ..state import Journal
from .engine import AttackOutcome, CombatEngine


class Tactic(str, Enum):
    ATTACK = "attack"
    FLEE = "flee"
    SURRENDER = "surrender"
    NEGOTIATE = "negotiate"
    PROTECT = "protect"
    CALL_FOR_HELP = "call_for_help"
    #: Nothing useful to do: no reachable target, no way out.
    WAIT = "wait"


#: Below this fraction of maximum hit points a creature starts thinking about
#: its own skin rather than the fight.
WOUNDED_FRACTION = 0.35

#: An intelligence score at or above this can bargain instead of bleeding.
NEGOTIATION_INTELLIGENCE = 8


@dataclass
class Decision:
    """One enemy's intent, with a reason a DM can narrate."""

    tactic: Tactic
    actor_id: str
    target_id: str | None = None
    reason: str = ""
    #: Filled in by `take_turn` when the decision came to blows.
    outcome: AttackOutcome | None = None

    def describe(self) -> str:
        return f"{self.tactic.value}{f' -> {self.target_id}' if self.target_id else ''}: {self.reason}"


def _wounded(creature: Creature) -> bool:
    if not creature.hp.maximum:
        return False
    return creature.hp.current / creature.hp.maximum <= WOUNDED_FRACTION


def _has_goal(creature: Creature, *words: str) -> bool:
    text = " ".join([*creature.goals, creature.tactics]).lower()
    return any(word in text for word in words)


def choose_target(
    engine: CombatEngine, journal: Journal, actor_id: str
) -> str | None:
    """Pick whom to attack.

    Monsters are not tacticians with full information, but they can see who is
    bleeding: the weakest reachable enemy is chosen, which produces the focus
    fire that makes a fight feel dangerous without needing a model to say so.
    """
    actor = journal.state.creature(actor_id)
    if actor is None:
        return None
    want_players = actor.kind is not CreatureKind.PLAYER

    candidates = []
    for combatant in engine.active_combatants(journal):
        if combatant.creature_id == actor_id:
            continue
        if combatant.is_player is not want_players:
            continue
        creature = journal.state.creature(combatant.creature_id)
        if creature is None:
            continue
        candidates.append(creature)

    if not candidates:
        return None
    return min(candidates, key=lambda c: (c.hp.current, c.armor_class, c.name)).id


def decide(engine: CombatEngine, journal: Journal, actor_id: str) -> Decision:
    """Decide what this creature does with its turn.

    The order of these tests is the interesting part.  Self-preservation is
    checked before aggression, and a creature protecting something checks
    that before either -- a mother bear does not flee her cubs.
    """
    actor = journal.state.creature(actor_id)
    if actor is None:
        return Decision(Tactic.WAIT, actor_id, reason="creature not found")

    players, enemies = engine.sides(journal)
    allies = [c for c in enemies if c.creature_id != actor_id]
    target_id = choose_target(engine, journal, actor_id)

    if target_id is None:
        return Decision(Tactic.WAIT, actor_id, reason="nothing left to fight")

    # Something worth dying for overrides everything else.
    if _has_goal(actor, "protect", "guard", "defend") and allies:
        weakest = min(
            (journal.state.creature(c.creature_id) for c in allies),
            key=lambda c: (c.hp.current if c else 0),
            default=None,
        )
        if weakest is not None and _wounded(weakest):
            return Decision(
                Tactic.PROTECT,
                actor_id,
                target_id,
                reason=f"stands over {weakest.name}",
            )

    if _wounded(actor):
        holds = engine.morale_check(journal, actor_id)
        if not holds:
            intelligent = actor.abilities.intelligence >= NEGOTIATION_INTELLIGENCE
            outnumbered = not allies and len(players) > 1

            if intelligent and _has_goal(actor, "escape", "flee", "survive", "never be captured"):
                return Decision(Tactic.FLEE, actor_id, reason="badly hurt and looking for a way out")
            if intelligent and outnumbered:
                return Decision(
                    Tactic.SURRENDER, actor_id, reason="surrounded, wounded, and out of ideas"
                )
            if intelligent:
                return Decision(
                    Tactic.NEGOTIATE, actor_id, target_id, reason="would rather talk than bleed"
                )
            return Decision(Tactic.FLEE, actor_id, reason="wounded and frightened")

    if _has_goal(actor, "reinforce", "call", "alarm", "obey") and allies and _wounded(actor):
        return Decision(Tactic.CALL_FOR_HELP, actor_id, reason="shouts for help")

    return Decision(Tactic.ATTACK, actor_id, target_id, reason="presses the attack")


def take_turn(engine: CombatEngine, journal: Journal, actor_id: str) -> Decision:
    """Decide and act.  Returns what was decided, for the narration layer.

    PROTECT, NEGOTIATE and CALL_FOR_HELP still attack -- an enemy shouting for
    the guard is not standing idle -- but the decision is returned so the DM
    can say what it looked like.
    """
    decision = decide(engine, journal, actor_id)

    if decision.tactic is Tactic.FLEE:
        engine.flee(journal, actor_id, reason=decision.reason)
    elif decision.tactic is Tactic.SURRENDER:
        engine.surrender(journal, actor_id, terms=decision.reason)
    elif decision.target_id is not None and decision.tactic in {
        Tactic.ATTACK,
        Tactic.PROTECT,
        Tactic.NEGOTIATE,
        Tactic.CALL_FOR_HELP,
    }:
        decision.outcome = engine.attack(journal, actor_id, decision.target_id)

    return decision


__all__ = [
    "NEGOTIATION_INTELLIGENCE",
    "WOUNDED_FRACTION",
    "Decision",
    "Tactic",
    "choose_target",
    "decide",
    "take_turn",
]
