"""The Dungeon Master agent: the reasoning loop of spec section 9.

    PLAYER INPUT -> interpret -> read state -> decide what must be rolled
      -> resolve mechanics -> update world/character/NPC state
      -> narrate the consequence -> update memory -> respond

The whole design rests on one boundary. The model is asked twice, and neither
time is it asked what happened:

1. **Interpret.**  "What is this player trying to do, and what must be rolled?"
   Answered as a schema-validated `ActionInterpretation`.
2. **Narrate.**  "Here is what the engine resolved; describe it."  The facts
   handed over are the summaries of the events the engine actually appended
   while resolving the action -- so the DM is narrating the log, not authoring
   it, and a model that tries to invent a hit is describing something the log
   will contradict.

Every id the model produces is checked against real game state before it is
used, so a hallucinated target is dropped rather than acted on. And every model
call has a floor: if the provider errors, refuses, or returns nothing usable,
the offline DM answers instead and play continues.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dmai.ai.dm_agent.prompts import (
    INTERPRETER,
    interpretation_prompt,
    narration_prompt,
    recap_prompt,
    resume_prompt,
    system_prompt,
)
from dmai.ai.memory.extraction import extract, recall
from dmai.ai.providers import AIProvider, OfflineProvider, ProviderError
from dmai.ai.providers.offline import interpret as offline_interpret
from dmai.engine.characters.checks import CheckError
from dmai.engine.combat.engine import CombatError
from dmai.engine.models.actions import (
    ActionInterpretation,
    ActionResult,
    CheckRequest,
    IntentKind,
    PlayerAction,
)
from dmai.engine.models.base import Visibility
from dmai.engine.models.dice import DiceResult
from dmai.engine.models.events import EventType, GameEvent
from dmai.engine.session import GameSession

#: Event types whose summaries are mechanical facts the narrator must honour.
FACT_EVENTS = {
    EventType.CHECK_RESOLVED,
    EventType.SAVE_RESOLVED,
    EventType.DICE_ROLLED,
    EventType.ATTACK,
    EventType.DAMAGE_DEALT,
    EventType.HEALING,
    EventType.CREATURE_DIED,
    EventType.CONDITION_ADDED,
    EventType.CONDITION_REMOVED,
    EventType.ITEM_GAINED,
    EventType.ITEM_LOST,
    EventType.CURRENCY_CHANGED,
    EventType.LOCATION_ENTERED,
    EventType.TIME_ADVANCED,
    EventType.COMBAT_STARTED,
    EventType.COMBAT_ENDED,
    EventType.OBJECTIVE_COMPLETED,
    EventType.DISCOVERY,
    EventType.WORLD_EVENT,
}

#: How many lines of prior play the narrator is shown.
RECENT_LINES = 6

#: Narration is short; interpretation is shorter.
NARRATION_TOKENS = 1024
INTERPRETATION_TOKENS = 1024


@dataclass
class TurnDiagnostics:
    """What the DM did this turn, for the control panel (spec section 17).

    Explanations only -- "a Perception check was required" -- never the model's
    reasoning, which the spec forbids surfacing.
    """

    provider: str = ""
    interpreted_by: str = ""
    narrated_by: str = ""
    #: The model that actually answered.  Not always the one asked for: a
    #: declined scene is re-run server-side on a fallback model.
    served_by: str = ""
    rationale: str = ""
    checks_requested: int = 0
    events_appended: int = 0
    cost_usd: float = 0.0
    degraded: list[str] = field(default_factory=list)


class DungeonMaster:
    """An AI DM bound to one campaign session."""

    def __init__(
        self,
        session: GameSession,
        provider: AIProvider | None = None,
        *,
        fallback: AIProvider | None = None,
    ):
        self.session = session
        self.provider = provider or OfflineProvider()
        #: Always present, always local.  The DM degrades to this rather than
        #: failing a turn, because a table mid-scene cannot wait for an outage.
        self.fallback = fallback or OfflineProvider()
        self.last: TurnDiagnostics = TurnDiagnostics(provider=self.provider.id)

    # --- the loop ----------------------------------------------------------

    def take_turn(self, action: PlayerAction) -> ActionResult:
        """Run one player action all the way through (spec section 9).

        The action is recorded here, before it is interpreted, so callers pass
        a `PlayerAction` they built rather than one they already logged with
        `GameSession.player_says` -- doing both writes the player's line twice.
        """
        session = self.session
        diagnostics = TurnDiagnostics(provider=self.provider.id)

        session.record_action(action)
        before = session.store.head

        interpretation = self._interpret(action, diagnostics)
        interpretation = self._ground(interpretation, action)
        diagnostics.checks_requested = len(interpretation.checks)
        diagnostics.rationale = interpretation.rationale

        rolls = self._resolve(interpretation, action)

        appended = session.store.since(before)
        facts = [
            fact_line(event)
            for event in appended
            if event.type in FACT_EVENTS and event.summary
        ]
        narration, cost = self._narrate(action, interpretation, facts, diagnostics)
        diagnostics.cost_usd = cost

        narration_event = None
        if narration:
            narration_event = session.narrate(narration)

        self._remember(appended)
        diagnostics.events_appended = session.store.head - before
        self.last = diagnostics

        return ActionResult(
            action_id=action.id,
            interpretation=interpretation,
            rolls=rolls,
            event_ids=[e.id for e in session.store.since(before)],
            narration=narration,
            cost_usd=cost,
        )

    # --- pass 1: interpret -------------------------------------------------

    def _interpret(self, action: PlayerAction, diagnostics: TurnDiagnostics) -> ActionInterpretation:
        """Ask what the player is trying to do; fall back rather than guess."""
        character = self.session.state.party.get(action.character_id or "")
        prompt = interpretation_prompt(
            self.session,
            action.text,
            actor_name=character.name if character else "",
            actor_id=character.id if character else "",
        )
        try:
            completion = self.provider.parse(
                system=f"{system_prompt(self.session)}\n\n{INTERPRETER}",
                messages=[{"role": "user", "content": prompt}],
                schema=ActionInterpretation,
                max_tokens=INTERPRETATION_TOKENS,
            )
        except ProviderError as exc:
            diagnostics.degraded.append(f"interpretation: {exc}")
            diagnostics.interpreted_by = self.fallback.id
            return offline_interpret(action.text)

        parsed = completion.parsed
        if not isinstance(parsed, ActionInterpretation):
            diagnostics.degraded.append("interpretation: provider returned no interpretation")
            diagnostics.interpreted_by = self.fallback.id
            return offline_interpret(action.text)

        diagnostics.interpreted_by = self.provider.id
        diagnostics.cost_usd += completion.cost_usd
        return parsed

    def _ground(self, interpretation: ActionInterpretation, action: PlayerAction) -> ActionInterpretation:
        """Bind an interpretation to reality before anything acts on it.

        The actor is whoever spoke, not whoever the model named. Target ids that
        do not exist are dropped and re-derived from the player's own words, so
        an invented creature can never be attacked.
        """
        grounded = interpretation.model_copy(deep=True)
        actor_id = action.character_id or ""

        grounded.actor_id = actor_id or grounded.actor_id
        known = {
            i for i in grounded.target_ids if self.session.state.creature(i) is not None
        } | {
            i for i in grounded.target_ids if i in self.session.state.world.locations
        }
        grounded.target_ids = [i for i in grounded.target_ids if i in known]
        if not grounded.target_ids:
            grounded.target_ids = self._targets_named_in(action.text)

        checks: list[CheckRequest] = []
        for check in grounded.checks:
            bound = check.model_copy(deep=True)
            if self.session.state.creature(bound.actor_id) is None:
                bound.actor_id = actor_id
            if bound.target_id and self.session.state.creature(bound.target_id) is None:
                bound.target_id = None
            if self.session.state.creature(bound.actor_id) is None:
                continue  # nobody real to roll it: drop the check, not the turn
            checks.append(bound)
        grounded.checks = checks
        return grounded

    def _targets_named_in(self, text: str) -> list[str]:
        """Find creatures the player named, by matching names in the scene."""
        lowered = text.lower()
        candidates = [
            *self.session.state.bestiary.values(),
            *self.session.state.world.npcs.values(),
            *self.session.state.party.values(),
        ]
        return [
            creature.id
            for creature in candidates
            if not creature.dead and creature.name.lower() in lowered
        ]

    # --- resolve mechanics -------------------------------------------------

    def _resolve(self, interpretation: ActionInterpretation, action: PlayerAction) -> list[DiceResult]:
        """Run everything the interpretation asked for, through the engine."""
        session = self.session
        rolls: list[DiceResult] = []

        if interpretation.intent is IntentKind.ATTACK:
            rolls.extend(self._resolve_attack(interpretation, action))

        for check in interpretation.checks:
            try:
                rolls.append(session.checks.resolve_request(session.journal, check))
            except (CheckError, ValueError) as exc:
                session.journal.record(
                    EventType.DM_OVERRIDE,
                    summary=f"A requested check could not be resolved: {exc}",
                    visibility=Visibility.DM_ONLY,
                )
        return rolls

    def _resolve_attack(
        self, interpretation: ActionInterpretation, action: PlayerAction
    ) -> list[DiceResult]:
        """An attack goes through the combat engine or it does not happen."""
        session = self.session
        attacker_id = interpretation.actor_id or action.character_id or ""
        target_id = next(iter(interpretation.target_ids), None)
        if not attacker_id or target_id is None:
            return []
        try:
            outcome = session.combat.attack(
                session.journal,
                attacker_id,
                target_id,
                spend_action=session.state.combat.active,
            )
        except (CombatError, ValueError) as exc:
            session.journal.record(
                EventType.DM_OVERRIDE,
                summary=f"That attack could not be resolved: {exc}",
                visibility=Visibility.DM_ONLY,
            )
            return []
        return [r for r in (outcome.attack_roll, outcome.damage_roll) if r is not None]

    # --- pass 2: narrate ---------------------------------------------------

    def _narrate(
        self,
        action: PlayerAction,
        interpretation: ActionInterpretation,
        facts: list[str],
        diagnostics: TurnDiagnostics,
    ) -> tuple[str, float]:
        memories = recall(
            self.session.state.memories,
            subjects=set(interpretation.target_ids) | {interpretation.actor_id or ""},
        )
        prompt = narration_prompt(
            self.session,
            text=action.text,
            facts=facts,
            memories=memories,
            rationale=interpretation.rationale,
            recent=self.session.transcript(limit=RECENT_LINES),
        )
        system = system_prompt(self.session)
        messages = [{"role": "user", "content": prompt}]

        try:
            completion = self.provider.complete(
                system=system, messages=messages, max_tokens=NARRATION_TOKENS
            )
            if completion.text.strip():
                diagnostics.narrated_by = self.provider.id
                diagnostics.served_by = completion.model
                if completion.meta.get("fell_back"):
                    # Worth saying out loud: the scene was declined and another
                    # model finished it, so the voice may shift mid-campaign.
                    diagnostics.degraded.append(
                        f"narration: the scene was declined; {completion.model} answered instead"
                    )
                return completion.text.strip(), completion.cost_usd
            diagnostics.degraded.append("narration: provider returned nothing")
        except ProviderError as exc:
            diagnostics.degraded.append(f"narration: {exc}")

        fallback = self.fallback.complete(
            system=system, messages=messages, max_tokens=NARRATION_TOKENS
        )
        diagnostics.narrated_by = self.fallback.id
        diagnostics.served_by = self.fallback.id
        return fallback.text.strip(), 0.0

    # --- memory ------------------------------------------------------------

    def _remember(self, events: list[GameEvent]) -> None:
        """File what is worth keeping, and nothing else (spec section 8)."""
        party = set(self.session.state.party)
        known = {m.source_event_id for m in self.session.state.memories}
        for memory in extract(events, party_ids=party):
            if memory.source_event_id in known:
                continue
            self.session.journal.record(
                EventType.MEMORY_ADDED,
                summary=memory.content,
                memory=memory.model_dump(mode="json"),
                visibility=memory.visibility,
            )

    # --- scenes ------------------------------------------------------------

    def open_scene(self, direction: str = "") -> str:
        """Narrate the opening of a scene, or the campaign itself."""
        session = self.session
        brief = direction or (
            f"Open the campaign. Premise: {session.campaign.premise or 'as yet unwritten'}. "
            "Set the scene and end by asking the party what they do."
        )
        return self._say(brief)

    def resume(self) -> str:
        """Bring the table back to where they left off (spec section 31)."""
        prompt = resume_prompt(self.session, self.session.transcript(is_dm=True, limit=12))
        return self._say(prompt, recorded=True)

    def session_recap(self, *, since: int = 0) -> str:
        """The session summary, in the campaign's voice (spec section 30)."""
        digest = self.session.recap(since=since)["narrative"]
        try:
            completion = self.provider.complete(
                system=system_prompt(self.session),
                messages=[{"role": "user", "content": recap_prompt(self.session, digest)}],
                max_tokens=NARRATION_TOKENS,
            )
            if completion.text.strip():
                return completion.text.strip()
        except ProviderError:
            pass
        return digest  # the mechanical recap is always true, if never pretty

    def _say(self, prompt: str, *, recorded: bool = True) -> str:
        """One narration call with the standing system prompt, logged."""
        system = system_prompt(self.session)
        messages = [{"role": "user", "content": prompt}]
        try:
            completion = self.provider.complete(
                system=system, messages=messages, max_tokens=NARRATION_TOKENS
            )
            text = completion.text.strip()
        except ProviderError:
            text = ""
        if not text:
            text = self.fallback.complete(
                system=system, messages=messages, max_tokens=NARRATION_TOKENS
            ).text.strip()
        if text and recorded:
            self.session.narrate(text)
        return text


def fact_line(event: GameEvent) -> str:
    """One event as a single line of fact.

    A dice event's summary is the multi-line block the table sees in the log
    (spec section 7).  That block is for players; a narrator wants one sentence,
    so rolls are compressed here rather than pasted in.
    """
    roll = event.data.get("roll")
    if isinstance(roll, dict) and "total" in roll:
        parts = [f"{roll.get('reason') or 'roll'}: {roll['total']}"]
        if roll.get("dc") is not None:
            parts.append(f"against DC {roll['dc']}")
        outcome = roll.get("outcome")
        if outcome and outcome != "not_applicable":
            parts.append(outcome.replace("_", " "))
        return " -- ".join(parts)
    return " ".join(event.summary.split())


__all__ = ["FACT_EVENTS", "DungeonMaster", "TurnDiagnostics", "fact_line"]
