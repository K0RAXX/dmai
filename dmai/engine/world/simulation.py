"""The world keeps moving when the party stops (spec sections 15 and 29).

Sandbox mode is the reason this module exists.  Time passes, weather turns,
factions advance the plans they had before the party arrived, and the debts the
world owes for earlier player decisions come due.  None of that requires a
language model: the simulator decides *that* something happens and records it,
and the DM AI is free to narrate it well.

The rests live here rather than in the combat engine because a rest is a change
to the world clock that happens to restore hit points, not a combat action.
"""

from __future__ import annotations

from ..characters.builder import CharacterError, _class_key
from ..dice import DiceEngine
from ..models.base import Ability, Condition, ConditionType, Visibility
from ..models.character import Creature
from ..models.events import EventType
from ..quests.tracker import QuestTracker
from ..rules.base import RulesEngine
from ..state import Journal

SHORT_REST_MINUTES = 60
LONG_REST_MINUTES = 8 * 60

#: Rolled on 1d8 when the weather is left to chance.  Deliberately coarse: the
#: weather is set dressing, and the DM layer can override it at any time.
WEATHER_TABLE = [
    "clear",
    "clear",
    "overcast",
    "overcast",
    "light rain",
    "fog",
    "heavy rain",
    "storm",
]

#: A faction acts roughly once a day of in-world time.
FACTION_TICK_HOURS = 24


class WorldSimulator:
    """Advances the clock and everything that moves with it."""

    def __init__(self, rules: RulesEngine, dice: DiceEngine, tracker: QuestTracker | None = None):
        self.rules = rules
        self.dice = dice
        self.tracker = tracker or QuestTracker()

    # --- the clock ---------------------------------------------------------

    def advance_time(self, journal: Journal, minutes: int, *, reason: str = "") -> None:
        """Move the world clock.  The event carries the resulting time, not the
        delta, so replaying it twice lands on the same hour."""
        if minutes <= 0:
            return
        clock = journal.state.world.time.model_copy(deep=True)
        clock.advance(minutes)
        journal.record(
            EventType.TIME_ADVANCED,
            summary=reason or f"{_duration(minutes)} passes. It is {clock}.",
            day=clock.day,
            hour=clock.hour,
            minute=clock.minute,
            minutes_elapsed=minutes,
            reason=reason,
        )

    def set_weather(self, journal: Journal, weather: str, *, reason: str = "") -> str:
        if weather == journal.state.world.weather:
            return weather
        journal.record(
            EventType.WEATHER_CHANGED,
            summary=f"The weather turns {weather}.",
            weather=weather,
            reason=reason,
        )
        return journal.state.world.weather

    def roll_weather(self, journal: Journal) -> str:
        """Let the dice decide.  Seeded, so a replayed campaign gets the same sky."""
        roll = self.dice.roll("1d8", reason="weather")
        return self.set_weather(journal, WEATHER_TABLE[roll.total - 1])

    # --- rests -------------------------------------------------------------

    def short_rest(
        self,
        journal: Journal,
        creature_ids: list[str],
        *,
        hit_dice: dict[str, int] | None = None,
    ) -> dict[str, int]:
        """An hour's breather.  Each creature may spend hit dice to heal.

        ``hit_dice`` maps creature id to dice spent; anyone left out simply
        rests.  Returns hit points restored, per creature.
        """
        self.advance_time(journal, SHORT_REST_MINUTES, reason="The party takes a short rest.")
        spent = hit_dice or {}
        healed: dict[str, int] = {}

        for creature_id in creature_ids:
            creature = self._require(journal, creature_id)
            dice_count = max(0, spent.get(creature_id, 0))
            if dice_count == 0 or creature.hp.current >= creature.hp.maximum:
                healed[creature_id] = 0
                continue
            die = self._hit_die(creature)
            constitution = creature.abilities.modifier(Ability.CON)
            roll = self.dice.roll(
                f"{dice_count}d{die}{dice_count * constitution:+d}"
                if constitution
                else f"{dice_count}d{die}",
                reason=f"{creature.name} spends {dice_count} hit dice",
                actor_id=creature_id,
            )
            restored = max(0, roll.total)
            healed[creature_id] = self._restore(
                journal, creature, restored, f"{creature.name} binds their wounds."
            )
        return healed

    def long_rest(self, journal: Journal, creature_ids: list[str]) -> None:
        """Eight hours.  Hit points full, resources back, one level of
        exhaustion gone, death saves cleared -- but the dead stay dead."""
        self.advance_time(journal, LONG_REST_MINUTES, reason="The party takes a long rest.")

        for creature_id in creature_ids:
            creature = self._require(journal, creature_id)
            if creature.dead:
                continue
            self._restore(
                journal,
                creature,
                creature.hp.maximum - creature.hp.current,
                f"{creature.name} wakes rested.",
                reset_death_saves=True,
                temporary=0,
            )
            for name, (_, maximum) in creature.resources.items():
                journal.record(
                    EventType.RESOURCE_CHANGED,
                    target_id=creature_id,
                    creature_id=creature_id,
                    summary=f"{creature.name} recovers {name}.",
                    resource=name,
                    current=maximum,
                    maximum=maximum,
                )
            self._ease_exhaustion(journal, creature_id)

    # --- the world's own agenda -------------------------------------------

    def world_event(
        self,
        journal: Journal,
        description: str,
        *,
        visibility: Visibility = Visibility.DM_ONLY,
    ) -> None:
        """Something happened that the party did not see."""
        journal.record(
            EventType.WORLD_EVENT,
            summary=description,
            description=description,
            visibility=visibility,
        )

    def advance_factions(self, journal: Journal) -> list[str]:
        """Every faction takes one step down its agenda.

        The step is *removed* from the agenda and recorded as a world event, so
        a plan cannot silently run twice and the log shows the world's progress
        even for schemes the party never noticed.
        """
        happened: list[str] = []
        for faction in list(journal.state.world.factions.values()):
            if not faction.agenda:
                continue
            step, *rest = faction.agenda
            advanced = faction.model_copy(deep=True)
            advanced.agenda = rest
            journal.record(
                EventType.FACTION_ADDED,
                target_id=faction.id,
                summary=f"{faction.name} advances its plans.",
                faction=advanced.model_dump(mode="json"),
                visibility=Visibility.DM_ONLY,
            )
            self.world_event(journal, f"{faction.name}: {step}")
            happened.append(f"{faction.name}: {step}")
        return happened

    def fire_due_consequences(self, journal: Journal) -> list[str]:
        """Pay what the world owes on or before today (spec section 29)."""
        today = journal.state.world.time.day
        fired: list[str] = []
        for consequence in self.tracker.pending(journal):
            if consequence.due_day is None or consequence.due_day > today:
                continue
            self.world_event(journal, consequence.effect, visibility=Visibility.PUBLIC)
            self.tracker.resolve(journal, consequence.id)
            fired.append(consequence.effect)
        return fired

    def tick(self, journal: Journal, minutes: int, *, reason: str = "") -> list[str]:
        """Advance time and let the world act on it.

        Factions move only in sandbox mode and only once a day, so a party that
        spends a week in one town still finds the world changed when they leave.
        """
        before = journal.state.world.time.day * 24 + journal.state.world.time.hour
        self.advance_time(journal, minutes, reason=reason)
        after = journal.state.world.time.day * 24 + journal.state.world.time.hour

        happened = self.fire_due_consequences(journal)
        if journal.state.campaign.settings.sandbox_mode:
            for _ in range((after - before) // FACTION_TICK_HOURS):
                happened.extend(self.advance_factions(journal))
        return happened

    # --- internals ---------------------------------------------------------

    def _hit_die(self, creature: Creature) -> int:
        """A creature's hit die, defaulting to d8 for anything classless."""
        try:
            class_data = self.rules.character_class(_class_key(self.rules, creature))
        except CharacterError:
            return 8
        return int((class_data or {}).get("hit_die", 8))

    def _restore(
        self,
        journal: Journal,
        creature: Creature,
        amount: int,
        summary: str,
        *,
        reset_death_saves: bool = False,
        temporary: int | None = None,
    ) -> int:
        """Write an absolute hit-point total, the way replay needs it."""
        target = min(creature.hp.maximum, creature.hp.current + max(0, amount))
        restored = target - creature.hp.current
        was_dying = creature.is_dying
        data: dict = {"hp_current": target}
        if temporary is not None:
            data["hp_temporary"] = temporary
        if reset_death_saves:
            data["death_saves_reset"] = True
        journal.record(
            EventType.HEALING,
            target_id=creature.id,
            creature_id=creature.id,
            summary=summary,
            amount=restored,
            **data,
        )
        if was_dying and target > 0:
            journal.record(
                EventType.CONDITION_REMOVED,
                target_id=creature.id,
                creature_id=creature.id,
                condition_type=ConditionType.UNCONSCIOUS.value,
                summary=f"{creature.name} regains consciousness.",
            )
        return restored

    @staticmethod
    def _ease_exhaustion(journal: Journal, creature_id: str) -> None:
        creature = journal.state.creature(creature_id)
        if creature is None:
            return
        exhaustion = next(
            (c for c in creature.conditions if c.type is ConditionType.EXHAUSTION), None
        )
        if exhaustion is None:
            return
        if exhaustion.level <= 1:
            journal.record(
                EventType.CONDITION_REMOVED,
                target_id=creature_id,
                creature_id=creature_id,
                summary=f"{creature.name} is no longer exhausted.",
                condition_type=ConditionType.EXHAUSTION.value,
            )
            return
        journal.record(
            EventType.CONDITION_ADDED,
            target_id=creature_id,
            creature_id=creature_id,
            summary=f"{creature.name}'s exhaustion eases.",
            condition=Condition(
                type=ConditionType.EXHAUSTION,
                source=exhaustion.source,
                level=exhaustion.level - 1,
            ).model_dump(mode="json"),
        )

    @staticmethod
    def _require(journal: Journal, creature_id: str) -> Creature:
        creature = journal.state.creature(creature_id)
        if creature is None:
            raise ValueError(f"no creature {creature_id} in this campaign")
        return creature


def _duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} minutes"
    hours, remainder = divmod(minutes, 60)
    if remainder:
        return f"{hours}h{remainder:02d}"
    return f"{hours} hour" + ("s" if hours != 1 else "")


__all__ = [
    "FACTION_TICK_HOURS",
    "LONG_REST_MINUTES",
    "SHORT_REST_MINUTES",
    "WEATHER_TABLE",
    "WorldSimulator",
]
