"""The engine's front door.

Spec section 2 and the closing instruction: *the Dungeon Master must be an
engine that interfaces talk to, not a GUI that contains an AI*.  `GameSession`
is that engine.  It owns one campaign's event log and wires every subsystem to
it, so a CLI, an HTTP route, a desktop window and an OpenClaw agent all drive
the same object and see the same game.

Three things this class is careful about:

* **One write path.**  Everything goes through the `Journal`, so the campaign
  can always be rebuilt from its log (`restore`), rolled back to a checkpoint,
  and audited afterwards.
* **No I/O.**  Saving is `dmai.persistence`'s job; narration is `dmai.ai`'s.
  A session runs happily with neither, which is what makes the engine testable
  without a disk or a model.
* **Seats, not omniscience.**  `log_for` filters the record to what one player
  is allowed to know (spec section 5).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .characters.builder import ScoreMethod, create_character, level_up
from .characters.checks import CheckResolver
from .characters.monsters import spawn_group, spawn_monster
from .combat.engine import CombatEngine
from .dice import DiceEngine
from .encounters.generator import EncounterGenerator
from .inventory.manager import InventoryManager
from .models.actions import PlayerAction
from .models.base import Visibility
from .models.campaign import Campaign, Checkpoint, GameState, Player, Session
from .models.character import Character, Creature
from .models.dice import DiceResult
from .models.events import EventType, GameEvent
from .models.memory import Memory, MemoryKind, MemoryTier
from .models.quests import QuestStatus
from .quests.tracker import QuestTracker
from .rules import RulesEngine, load_rules
from .state import EventStore, Journal, checkpoint_for, rebuild
from .world.atlas import Atlas
from .world.simulation import WorldSimulator


class GameSession:
    """One campaign, fully wired: rules, dice, log, and every subsystem."""

    def __init__(
        self,
        campaign: Campaign,
        *,
        rules: RulesEngine | None = None,
        dice: DiceEngine | None = None,
        events: Iterable[GameEvent] = (),
        pack_dir: Path | str | None = None,
    ):
        self.rules = rules or load_rules(campaign.settings.rules_pack, pack_dir)
        self.dice = dice or DiceEngine(seed=campaign.settings.rng_seed)

        self.store = EventStore(campaign.id, events)
        self.state: GameState = rebuild(campaign, self.store)
        self.journal = Journal(self.state, self.store)

        self.checks = CheckResolver(self.rules, self.dice)
        self.combat = CombatEngine(self.rules, self.dice)
        self.inventory = InventoryManager(self.rules)
        self.encounters = EncounterGenerator(self.rules, self.dice)
        self.quests = QuestTracker()
        self.atlas = Atlas()
        self.world = WorldSimulator(self.rules, self.dice, self.quests)

    # --- lifecycle ---------------------------------------------------------

    @classmethod
    def create(cls, campaign: Campaign, **kwargs: Any) -> GameSession:
        """Open a brand-new campaign and log its first event."""
        session = cls(campaign, **kwargs)
        session.journal.record(
            EventType.CAMPAIGN_CREATED,
            target_id=campaign.id,
            summary=f"Campaign created: {campaign.name}.",
            campaign=campaign.model_dump(mode="json"),
        )
        return session

    @classmethod
    def restore(
        cls, campaign: Campaign, events: Iterable[GameEvent], **kwargs: Any
    ) -> GameSession:
        """Reopen a saved campaign by replaying its log (spec section 10)."""
        return cls(campaign, events=events, **kwargs)

    @property
    def campaign(self) -> Campaign:
        return self.state.campaign

    def begin_session(self) -> Session:
        """Start a sitting.  Its recap covers the events from here on."""
        sitting = Session(
            campaign_id=self.campaign.id, first_event_seq=self.store.head + 1
        )
        self.journal.record(
            EventType.SESSION_STARTED,
            target_id=sitting.id,
            summary=f"Session begins. {self.state.world.time}.",
            session=sitting.model_dump(mode="json"),
        )
        return sitting

    def end_session(self, sitting: Session, *, recap: str = "") -> Session:
        """Close a sitting and attach its recap (spec section 30)."""
        closed = sitting.model_copy(deep=True)
        closed.last_event_seq = self.store.head
        closed.recap = recap or self.recap(since=sitting.first_event_seq)["narrative"]
        self.journal.record(
            EventType.SESSION_ENDED,
            target_id=closed.id,
            summary="Session ends.",
            session=closed.model_dump(mode="json"),
        )
        return closed

    # --- the table ---------------------------------------------------------

    def add_player(
        self, name: str, *, external_id: str | None = None, is_host: bool = False
    ) -> Player:
        """Seat a human.  ``external_id`` is how a chat adapter finds them again."""
        player = Player(name=name, external_id=external_id, is_host=is_host)
        self.journal.record(
            EventType.PLAYER_JOINED,
            actor_id=player.id,
            summary=f"{name} joins the table.",
            player=player.model_dump(mode="json"),
        )
        return self.state.players[player.id]

    def player_by_external_id(self, external_id: str) -> Player | None:
        """Find a seat from a chat identity -- the OpenClaw adapter's lookup."""
        return next(
            (p for p in self.state.players.values() if p.external_id == external_id),
            None,
        )

    def add_character(
        self,
        name: str,
        species: str = "human",
        character_class: str = "fighter",
        level: int = 1,
        *,
        player_id: str | None = None,
        method: ScoreMethod = ScoreMethod.STANDARD,
        **kwargs: Any,
    ) -> Character:
        """Roll up a character and put them in the party."""
        character = create_character(
            self.rules,
            name,
            species,
            character_class,
            level,
            method=method,
            dice=self.dice,
            player_id=player_id,
            **kwargs,
        )
        self.journal.record(
            EventType.CHARACTER_CREATED,
            actor_id=character.id,
            summary=f"{character.name}, {character.species} {character.character_class}, joins the party.",
            character=character.model_dump(mode="json", by_alias=True),
        )
        return self.state.party[character.id]

    def award_level(self, character_id: str, *, class_key: str | None = None) -> Character:
        """Advance a character one level, hit points and all."""
        character = self.state.party.get(character_id)
        if character is None:
            raise KeyError(f"no character {character_id} in the party")
        payload = level_up(self.rules, character, class_key)
        self.journal.record(
            EventType.LEVEL_UP,
            actor_id=character_id,
            creature_id=character_id,
            summary=f"{character.name} reaches level {payload['level']}.",
            **payload,
        )
        return self.state.party[character_id]

    def award_experience(self, amount: int, *, reason: str = "") -> None:
        """Split experience across the living party, as the table expects."""
        living = [c for c in self.state.party.values() if not c.dead]
        if not living or amount <= 0:
            return
        share = amount // len(living)
        for character in living:
            updated = character.model_copy(deep=True)
            updated.experience += share
            self.journal.record(
                EventType.CHARACTER_UPDATED,
                actor_id=character.id,
                summary=f"{character.name} gains {share} XP{f' ({reason})' if reason else ''}.",
                character=updated.model_dump(mode="json", by_alias=True),
                experience_gained=share,
            )

    # --- the bestiary ------------------------------------------------------

    def spawn(
        self, monster_key: str, count: int = 1, *, roll_hit_points: bool = True
    ) -> list[Creature]:
        """Bring monsters onto the board and log each one."""
        dice = self.dice if roll_hit_points else None
        creatures = (
            spawn_group(self.rules, monster_key, count, dice=dice)
            if count > 1
            else [spawn_monster(self.rules, monster_key, dice=dice)]
        )
        for creature in creatures:
            self.journal.record(
                EventType.CREATURE_SPAWNED,
                target_id=creature.id,
                summary=f"{creature.name} appears.",
                creature=creature.model_dump(mode="json", by_alias=True),
                visibility=Visibility.DM_ONLY,
            )
        return [self.state.bestiary[c.id] for c in creatures]

    # --- narration and talk ------------------------------------------------

    def narrate(
        self,
        text: str,
        *,
        visibility: Visibility = Visibility.PUBLIC,
        audience_id: str | None = None,
    ) -> GameEvent:
        return self.journal.record(
            EventType.DM_NARRATION,
            summary=text,
            text=text,
            visibility=visibility,
            audience_id=audience_id,
        )

    def npc_says(self, npc_id: str, text: str) -> GameEvent:
        npc = self.state.world.npcs.get(npc_id)
        name = npc.name if npc else npc_id
        return self.journal.record(
            EventType.NPC_DIALOGUE,
            actor_id=npc_id,
            summary=f"{name}: {text}",
            text=text,
            speaker=name,
        )

    def whisper(self, player_id: str, text: str) -> GameEvent:
        """A note passed to one player -- private information, spec section 5."""
        return self.journal.record(
            EventType.PRIVATE_MESSAGE,
            summary=text,
            text=text,
            visibility=Visibility.PRIVATE,
            audience_id=player_id,
        )

    def record_action(self, action: PlayerAction) -> GameEvent:
        """Log raw player input before anything interprets it."""
        character = self.state.party.get(action.character_id or "")
        name = character.name if character else "A player"
        return self.journal.record(
            EventType.PLAYER_ACTION,
            actor_id=action.character_id,
            summary=f"{name}: {action.text}",
            action_id=action.id,
            player_id=action.player_id,
            text=action.text,
        )

    def player_says(
        self, text: str, *, character_id: str | None = None, player_id: str | None = None
    ) -> PlayerAction:
        """Convenience wrapper: build the action, log it, hand it back."""
        action = PlayerAction(
            campaign_id=self.campaign.id,
            player_id=player_id,
            character_id=character_id,
            text=text,
        )
        self.record_action(action)
        return action

    def roll(self, expression: str, *, reason: str = "", actor_id: str | None = None) -> DiceResult:
        """Roll where everyone can see it (spec section 7: never a hidden result)."""
        result = self.dice.roll(expression, reason=reason, actor_id=actor_id)
        self.journal.record(
            EventType.DICE_ROLLED,
            actor_id=actor_id,
            summary=result.describe(),
            roll=result.model_dump(mode="json"),
        )
        return result

    def remember(
        self,
        content: str,
        *,
        tier: MemoryTier = MemoryTier.SESSION,
        kind: MemoryKind = MemoryKind.FACT,
        importance: int = 50,
        subjects: list[str] | None = None,
        visibility: Visibility = Visibility.PUBLIC,
    ) -> Memory:
        """File a fact the DM should still know three sessions from now."""
        memory = Memory(
            tier=tier,
            kind=kind,
            content=content,
            importance=importance,
            subjects=subjects or [],
            visibility=visibility,
        )
        self.journal.record(
            EventType.MEMORY_ADDED,
            summary=content,
            memory=memory.model_dump(mode="json"),
            visibility=visibility,
        )
        return memory

    def dm_override(self, description: str, **data: Any) -> GameEvent:
        """A human DM overrode the engine.  Logged, never hidden (spec 17)."""
        return self.journal.record(
            EventType.DM_OVERRIDE, summary=description, description=description, **data
        )

    # --- checkpoints and rollback ------------------------------------------

    def checkpoint(self, label: str = "", *, automatic: bool = False) -> Checkpoint:
        mark = checkpoint_for(self.campaign.id, self.store, label, automatic)
        # Point the mark at its own CHECKPOINT event rather than the event
        # before it, so rolling back to a checkpoint keeps the checkpoint.
        mark.event_seq = self.store.head + 1
        self.journal.record(
            EventType.CHECKPOINT,
            target_id=mark.id,
            summary=f"Checkpoint: {label}" if label else "Checkpoint.",
            checkpoint=mark.model_dump(mode="json"),
        )
        return mark

    def checkpoints(self) -> list[Checkpoint]:
        """Every checkpoint in the log, oldest first."""
        return [
            Checkpoint.model_validate(event.data["checkpoint"])
            for event in self.store
            if event.type is EventType.CHECKPOINT and "checkpoint" in event.data
        ]

    def rollback(self, seq: int) -> GameState:
        """Rewind the campaign to a point in the log and refold the state."""
        self.state = self.journal.rollback_to(seq)
        return self.state

    def rollback_to_checkpoint(self, checkpoint: Checkpoint) -> GameState:
        return self.rollback(checkpoint.event_seq)

    # --- reading the table -------------------------------------------------

    def log_for(
        self, player_id: str | None = None, *, is_dm: bool = False, limit: int | None = None
    ) -> list[GameEvent]:
        """The log as one seat sees it.  The DM seat sees everything."""
        visible = self.store.visible_to(player_id, is_dm)
        return visible[-limit:] if limit else visible

    def transcript(
        self, player_id: str | None = None, *, is_dm: bool = False, limit: int = 50
    ) -> list[str]:
        """The narrative line of the log: what a client would print."""
        return [
            event.summary
            for event in self.log_for(player_id, is_dm=is_dm, limit=limit)
            if event.summary
        ]

    def recap(self, *, since: int = 0) -> dict:
        """A session summary, assembled from the log (spec section 30).

        Deliberately mechanical.  The narrative line here is a plain digest of
        what the log says happened; the DM AI is expected to rewrite it in the
        campaign's voice, and it must never invent what this does not contain.
        """
        events = [e for e in self.store if e.seq >= since]
        state = self.state

        def summaries(*types: EventType) -> list[str]:
            return [e.summary for e in events if e.type in types and e.summary]

        loot = summaries(EventType.ITEM_GAINED)
        combats = [e for e in events if e.type is EventType.COMBAT_ENDED]
        deaths = summaries(EventType.CREATURE_DIED)
        quests_changed = summaries(EventType.QUEST_ADDED, EventType.QUEST_UPDATED)

        recap = {
            "campaign": self.campaign.name,
            "events_covered": len(events),
            "discoveries": summaries(EventType.DISCOVERY),
            "npc_interactions": summaries(EventType.NPC_MET, EventType.NPC_ATTITUDE_CHANGED),
            "combats": [e.summary or "A fight ended." for e in combats],
            "deaths": deaths,
            "loot": loot,
            "progression": summaries(EventType.LEVEL_UP),
            "quests": quests_changed,
            "world_changes": summaries(EventType.WORLD_EVENT, EventType.WEATHER_CHANGED),
            "decisions": summaries(EventType.STORY_DECISION),
            "unresolved": [c.effect for c in self.quests.pending(self.journal)],
            "open_objectives": [
                f"{quest.title}: {objective.description}"
                for quest, objective in self.quests.open_objectives(self.journal)
            ],
            "location": state.location.name if state.location else "somewhere unmapped",
            "time": str(state.world.time),
        }
        recap["narrative"] = _digest(recap)
        return recap

    def briefing(self) -> dict:
        """Everything needed to pick a campaign back up (spec section 31).

        This is the payload the DM AI is handed on resume.  It answers the
        eleven questions the spec asks, from state rather than from a
        transcript, so a campaign resumes correctly even if the chat history is
        long gone.
        """
        state = self.state
        return {
            "who": [
                {
                    "id": c.id,
                    "name": c.name,
                    "level": c.level,
                    "class": c.character_class,
                    "species": c.species,
                    "hp": f"{c.hp.current}/{c.hp.maximum}",
                    "conditions": [x.type.value for x in c.conditions],
                    "player": state.players[c.player_id].name
                    if c.player_id and c.player_id in state.players
                    else None,
                }
                for c in state.party.values()
            ],
            "where": {
                "location": state.location.name if state.location else None,
                "description": state.location.description if state.location else "",
                "npcs_present": [n.name for n in self.atlas.npcs_at(self.journal, state.current_location_id or "")],
                "exits": [loc.name for loc in self.atlas.neighbours(self.journal, state.current_location_id or "")],
            },
            "when": {
                "time": str(state.world.time),
                "day": state.world.time.day,
                "weather": state.world.weather,
            },
            "what_happened": self.transcript(is_dm=True, limit=25),
            "what_is_happening": {
                "in_combat": state.combat.active,
                "round": state.combat.round,
                "turn": (state.combat.current.creature_id if state.combat.current else None),
                "encounter": next(
                    (e.name for e in state.encounters.values() if not e.resolved and not e.abandoned),
                    None,
                ),
            },
            "players_know": state.story.discoveries,
            "players_do_not_know": [
                secret.content
                for location in state.world.locations.values()
                for secret in location.secrets
                if not secret.known_by
            ]
            + [
                secret.content
                for npc in state.world.npcs.values()
                for secret in npc.secrets
                if not secret.known_by
            ],
            "npcs_know": {
                npc.name: npc.knowledge for npc in state.world.npcs.values() if npc.knowledge
            },
            "factions": [
                {
                    "name": f.name,
                    "party_standing": f.party_standing,
                    "next_move": f.agenda[0] if f.agenda else None,
                }
                for f in state.world.factions.values()
            ],
            "active_quests": [
                {
                    "title": q.title,
                    "summary": q.summary,
                    "objectives": [o.description for o in q.objectives if not o.completed],
                }
                for q in self.quests.by_status(self.journal, QuestStatus.ACTIVE)
            ],
            "pending_consequences": [
                {"trigger": c.trigger, "effect": c.effect, "due_day": c.due_day}
                for c in self.quests.pending(self.journal)
            ],
            "memories": [
                m.content
                for m in sorted(state.memories, key=lambda m: -m.importance)[:20]
            ],
        }


def _digest(recap: dict) -> str:
    """Turn the recap's parts into a few plain sentences.

    Kept dull on purpose: this is the factual floor the DM AI narrates over.
    """
    lines = [f"The party is at {recap['location']} ({recap['time']})."]
    for label, key in (
        ("Discovered", "discoveries"),
        ("Fought", "combats"),
        ("Gained", "loot"),
        ("Quests", "quests"),
        ("The world", "world_changes"),
    ):
        items = recap[key]
        if items:
            lines.append(f"{label}: " + "; ".join(items[:5]) + ("..." if len(items) > 5 else ""))
    if recap["open_objectives"]:
        lines.append("Still open: " + "; ".join(recap["open_objectives"][:5]))
    if recap["unresolved"]:
        lines.append("Unresolved: " + "; ".join(recap["unresolved"][:5]))
    return "\n".join(lines)


__all__ = ["GameSession"]
