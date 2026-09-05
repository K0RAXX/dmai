"""The window's only door into the game.

The desktop client is a *client* in the sense the engine means it: it never
edits game state.  It calls `GameSession` and the DM agent, then re-reads what
they say afterwards.  Every panel the window draws -- the party sheets, the
initiative order, the chronicle -- is derived from one call to `view()`, which
is itself a fold of the event log, so three panels can never disagree.

Two rules hold this file together:

* **Nothing raises across the bridge.**  A webview's JS side cannot catch a
  Python traceback usefully, so every operation returns
  ``{"ok": true, ...}`` or ``{"ok": false, "error": "..."}``.  A bad campaign
  name is a message in the UI, not a dead window.
* **Nothing returns a live object.**  Only JSON-serialisable data crosses, the
  same discipline `OpenClawAdapter` keeps, so the UI never holds a handle to a
  session it could mutate behind the engine's back.

Saving happens after anything that changes state.  The store writes only the
events not already on disk, so calling it every turn is cheap.
"""

from __future__ import annotations

import functools
import json
import traceback
from pathlib import Path
from typing import Any, Callable

from dmai.ai.dm_agent.agent import DungeonMaster
from dmai.ai.providers import available_providers, load_provider
from dmai.engine.models.actions import PlayerAction
from dmai.engine.models.base import new_id
from dmai.engine.models.campaign import (
    Campaign,
    CampaignSettings,
    DMBehavior,
    NarrativeStyle,
    StoryTone,
)
from dmai.engine.models.events import EventType
from dmai.engine.quests.tracker import QuestStatus
from dmai.engine.rules import available_rules
from dmai.engine.session import GameSession
from dmai.persistence import CampaignStore, SaveError

#: How many lines of the log the chronicle panel holds.  The whole log is
#: always on disk; this is only what the window keeps in the DOM.
CHRONICLE_LIMIT = 400

#: Event type -> the CSS class the chronicle styles it with.  Anything not
#: named here renders as a plain system line, so a new event type degrades to
#: "visible but unstyled" rather than disappearing from the record.
ENTRY_KINDS: dict[EventType, str] = {
    EventType.DM_NARRATION: "narration",
    EventType.NPC_DIALOGUE: "dialogue",
    EventType.PLAYER_ACTION: "action",
    EventType.PRIVATE_MESSAGE: "whisper",
    EventType.DICE_ROLLED: "roll",
    EventType.CHECK_RESOLVED: "roll",
    EventType.SAVE_RESOLVED: "roll",
    EventType.INITIATIVE_ROLLED: "roll",
    EventType.ATTACK: "combat",
    EventType.DAMAGE_DEALT: "combat",
    EventType.HEALING: "heal",
    EventType.CREATURE_DIED: "death",
    EventType.DEATH_SAVE: "death",
    EventType.COMBAT_STARTED: "combat-edge",
    EventType.COMBAT_ENDED: "combat-edge",
    EventType.LEVEL_UP: "triumph",
    EventType.ITEM_GAINED: "loot",
    EventType.CURRENCY_CHANGED: "loot",
    EventType.QUEST_ADDED: "quest",
    EventType.QUEST_UPDATED: "quest",
    EventType.OBJECTIVE_COMPLETED: "quest",
    EventType.DISCOVERY: "discovery",
    EventType.LOCATION_ENTERED: "travel",
    EventType.WORLD_EVENT: "world",
    EventType.WEATHER_CHANGED: "world",
    EventType.CHECKPOINT: "mark",
}


class BridgeError(RuntimeError):
    """Something the player did wrong, phrased for the player."""


def operation(method: Callable) -> Callable:
    """Wrap a bridge method so the JS side always gets a result object.

    An expected failure (`BridgeError`, `SaveError`, a bad key) comes back as
    its message.  Anything unexpected is still caught -- a crashed operation
    must not take the window with it -- but is printed to the console so it can
    be found, because a silently swallowed bug is worse than a loud one.
    """

    @functools.wraps(method)
    def wrapper(self: DesktopBridge, *args: Any, **kwargs: Any) -> dict:
        try:
            payload = method(self, *args, **kwargs)
        except (BridgeError, SaveError, KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc).strip("'") or exc.__class__.__name__}
        except Exception as exc:  # noqa: BLE001 - the window must survive
            traceback.print_exc()
            return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
        if payload is None:
            return {"ok": True}
        if isinstance(payload, dict):
            return {"ok": True, **payload}
        return {"ok": True, "data": payload}

    return wrapper


class DesktopBridge:
    """Everything the window is allowed to ask the game to do."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        provider: str | None = None,
        model: str | None = None,
    ):
        self.store = CampaignStore(root)
        #: A provider named at launch overrides the campaign's own, so a table
        #: can try a model for one sitting without rewriting the save.
        self.provider_override = provider
        self.model_override = model

        self.session: GameSession | None = None
        self.dm: DungeonMaster | None = None
        self.sitting = None
        #: The seat the window is playing.  A desktop table is usually one
        #: person, so the first character created becomes the active one.
        self.player_id: str | None = None
        self.character_id: str | None = None

    # --- internals ---------------------------------------------------------

    def _open(self) -> GameSession:
        if self.session is None:
            raise BridgeError("no campaign is open")
        return self.session

    def _agent(self) -> DungeonMaster:
        if self.dm is None:
            raise BridgeError("no campaign is open")
        return self.dm

    def _find(self, campaign: str) -> str:
        """Resolve an id or a name to a campaign id, as the CLI does."""
        if self.store.exists(campaign):
            return campaign
        summaries = self.store.list_campaigns()
        exact = [s for s in summaries if s.name.lower() == campaign.lower()]
        loose = exact or [s for s in summaries if campaign.lower() in s.name.lower()]
        if not loose:
            raise BridgeError(f"no campaign matching {campaign!r}")
        if len(loose) > 1:
            names = ", ".join(s.name for s in loose)
            raise BridgeError(f"{campaign!r} matches several campaigns: {names}")
        return loose[0].id

    def _adopt(self, session: GameSession) -> None:
        """Make a loaded session the open one and build its DM."""
        settings = session.campaign.settings
        provider_id = self.provider_override or settings.ai_provider
        model = self.model_override or settings.ai_model
        kwargs = {"model": model} if model and provider_id != "offline" else {}

        self.session = session
        self.dm = DungeonMaster(session, load_provider(provider_id, **kwargs))
        self.sitting = session.begin_session()
        self._seat()

    def _seat(self) -> None:
        """Point the window at a seat: the host player and their character."""
        session = self._open()
        players = list(session.state.players.values())
        host = next((p for p in players if p.is_host), players[0] if players else None)
        self.player_id = host.id if host else None

        party = list(session.state.party.values())
        owned = [c for c in party if host and c.player_id == host.id]
        chosen = (owned or party)
        self.character_id = chosen[0].id if chosen else None

    def _save(self) -> None:
        if self.session is not None:
            self.store.save(self.session)

    # --- the library -------------------------------------------------------

    @operation
    def list_campaigns(self) -> dict:
        """The shelf: every saved campaign, newest first."""
        summaries = sorted(
            self.store.list_campaigns(), key=lambda s: s.updated_at, reverse=True
        )
        return {
            "campaigns": [
                {
                    "id": s.id,
                    "name": s.name,
                    "premise": s.premise,
                    "events": s.events,
                    "updated_at": s.updated_at.isoformat(),
                    "updated_human": f"{s.updated_at:%d %b %Y, %H:%M}",
                }
                for s in summaries
            ],
            "root": str(self.store.root),
        }

    @operation
    def options(self) -> dict:
        """Everything the new-campaign and new-character forms offer."""
        rules = load_rules_safely()
        return {
            "rules": available_rules(),
            "providers": available_providers(),
            "tones": [t.value for t in StoryTone],
            "styles": [s.value for s in NarrativeStyle],
            "behaviors": [b.value for b in DMBehavior],
            "classes": rules["classes"],
            "species": rules["species"],
        }

    @operation
    def create_campaign(self, form: dict | None = None) -> dict:
        """Found a campaign, seat its host, and open it."""
        form = form or {}
        name = str(form.get("name", "")).strip()
        if not name:
            raise BridgeError("a campaign needs a name")

        seed = form.get("seed")
        settings = CampaignSettings(
            tone=StoryTone(form.get("tone") or StoryTone.HEROIC.value),
            style=NarrativeStyle(form.get("style") or NarrativeStyle.DESCRIPTIVE.value),
            rules_pack=form.get("rules") or "srd51",
            rng_seed=int(seed) if str(seed or "").strip() else None,
            sandbox_mode=bool(form.get("sandbox")),
            ai_provider=form.get("provider") or "offline",
            ai_model=form.get("model") or "",
        )
        session = GameSession.create(
            Campaign(
                name=name,
                premise=str(form.get("premise", "")).strip(),
                setting=str(form.get("setting", "")).strip(),
                settings=settings,
            )
        )
        player_name = str(form.get("player", "")).strip()
        if player_name:
            session.add_player(player_name, is_host=True)

        self.store.save(session)
        self._adopt(session)
        return {"campaign_id": session.campaign.id}

    @operation
    def open_campaign(self, campaign: str) -> dict:
        """Load a campaign and sit down at it."""
        self._adopt(self.store.load(self._find(campaign)))
        return {"campaign_id": self._open().campaign.id}

    @operation
    def close_campaign(self) -> dict:
        """Leave the table, ending the sitting and saving the log."""
        if self.session is not None:
            if self.sitting is not None:
                self.session.end_session(self.sitting)
            self._save()
        self.session = self.dm = self.sitting = None
        self.player_id = self.character_id = None
        return {}

    @operation
    def delete_campaign(self, campaign_id: str) -> dict:
        """Destroy a campaign's save.  The UI confirms before calling this."""
        if self.session is not None and self.session.campaign.id == campaign_id:
            self.session = self.dm = self.sitting = None
        self.store.delete(campaign_id)
        return {}

    @operation
    def export_campaign(self, campaign_id: str, destination: str) -> dict:
        """Write a campaign bundle somewhere the player picked."""
        if not destination:
            raise BridgeError("no destination chosen")
        path = self.store.export(campaign_id, destination)
        return {"path": str(path)}

    @operation
    def import_campaign(self, source: str) -> dict:
        """Read a bundle in, never over a campaign that is already here.

        `import_bundle` keeps the bundle's own id, so importing a campaign the
        player already has would overwrite it -- and from a GUI, where the file
        was chosen from a dialog rather than typed, that is a silent way to
        lose a save.  A collision is imported alongside instead, under a fresh
        id, which is what `new_id` is for.
        """
        if not source:
            raise BridgeError("no file chosen")

        fresh = new_id("camp") if self._already_here(source) else None
        campaign = self.store.import_bundle(source, new_id=fresh)
        return {
            "campaign_id": campaign.id,
            "name": campaign.name,
            "renamed": fresh is not None,
        }

    def _already_here(self, source: str) -> bool:
        """Whether this bundle's campaign id is one the store already holds.

        A bundle that cannot be read at all is left to `import_bundle`, which
        raises the store's own error message rather than one invented here.
        """
        try:
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
            campaign_id = str((payload.get("campaign") or {}).get("id") or "")
            return bool(campaign_id) and self.store.exists(campaign_id)
        except (OSError, ValueError, AttributeError):
            return False

    # --- the table ---------------------------------------------------------

    @operation
    def add_character(self, form: dict | None = None) -> dict:
        """Roll up a character and put them in the party."""
        form = form or {}
        session = self._open()
        name = str(form.get("name", "")).strip()
        if not name:
            raise BridgeError("a character needs a name")

        player_name = str(form.get("player", "")).strip()
        player_id = None
        if player_name:
            seat = next(
                (
                    p
                    for p in session.state.players.values()
                    if p.name.lower() == player_name.lower()
                ),
                None,
            )
            player_id = (
                seat or session.add_player(player_name, is_host=not session.state.players)
            ).id
        elif self.player_id:
            player_id = self.player_id

        character = session.add_character(
            name,
            str(form.get("species") or "human"),
            str(form.get("character_class") or "fighter"),
            max(1, int(form.get("level") or 1)),
            player_id=player_id,
        )
        self._save()
        if self.character_id is None:
            self.character_id = character.id
        return {"character_id": character.id}

    @operation
    def play_as(self, character_id: str) -> dict:
        """Change which character the window's actions are attributed to."""
        session = self._open()
        if character_id not in session.state.party:
            raise BridgeError("that character is not in the party")
        self.character_id = character_id
        character = session.state.party[character_id]
        if character.player_id:
            self.player_id = character.player_id
        return {}

    @operation
    def act(self, text: str) -> dict:
        """One player action, all the way through the DM's reasoning loop.

        This is the slow call: with a model provider it makes two round trips.
        The UI shows the table as busy while it runs and re-reads `view()`
        afterwards rather than trying to patch panels from the result.
        """
        session, dm = self._open(), self._agent()
        text = (text or "").strip()
        if not text:
            raise BridgeError("say what you do")

        # Built rather than logged: `take_turn` records the action itself, so
        # going through `session.player_says` here would put the player's line
        # in the chronicle twice.
        action = PlayerAction(
            campaign_id=session.campaign.id,
            player_id=self.player_id,
            character_id=self.character_id,
            text=text,
        )
        result = dm.take_turn(action)
        self._save()
        return {
            "narration": result.narration,
            "rolls": [
                {"text": roll.describe(), "total": roll.total} for roll in result.rolls
            ],
            "diagnostics": self._diagnostics(),
        }

    @operation
    def roll(self, expression: str, reason: str = "") -> dict:
        """Roll where the whole table can see it."""
        session = self._open()
        result = session.roll(
            (expression or "d20").strip(), reason=reason, actor_id=self.character_id
        )
        self._save()
        return {"text": result.describe(), "total": result.total}

    @operation
    def narrate(self, text: str) -> dict:
        """Put a line into the record as the DM, without a model turn."""
        session = self._open()
        if not (text or "").strip():
            raise BridgeError("nothing to narrate")
        session.narrate(text.strip())
        self._save()
        return {}

    @operation
    def open_scene(self, direction: str = "") -> dict:
        """Have the DM set the scene -- what a session opens on."""
        narration = self._agent().open_scene(direction)
        self._save()
        return {"narration": narration, "diagnostics": self._diagnostics()}

    @operation
    def resume(self) -> dict:
        """Have the DM recap where the table left off, in its own voice."""
        narration = self._agent().resume()
        self._save()
        return {"narration": narration, "diagnostics": self._diagnostics()}

    @operation
    def recap(self) -> dict:
        """The mechanical recap of this sitting, from the log."""
        session = self._open()
        since = self.sitting.first_event_seq if self.sitting else 0
        return {"recap": session.recap(since=since)}

    # --- checkpoints -------------------------------------------------------

    @operation
    def checkpoint(self, label: str = "") -> dict:
        session = self._open()
        mark = session.checkpoint((label or "").strip())
        self._save()
        return {"seq": mark.event_seq, "label": mark.label}

    @operation
    def rollback(self, seq: int) -> dict:
        """Rewind the campaign.  Truncating the log is the whole operation."""
        session = self._open()
        session.rollback(int(seq))
        self._save()
        self._seat()
        return {}

    # --- reading the table -------------------------------------------------

    @operation
    def view(self) -> dict:
        """Everything the window draws, from one fold of the log."""
        return self._view()

    @operation
    def sheet(self, character_id: str) -> dict:
        """One character in full, for the sheet overlay."""
        session = self._open()
        character = session.state.party.get(character_id) or session.state.bestiary.get(
            character_id
        )
        if character is None:
            raise BridgeError("no such character")

        rules = session.rules
        return {
            "sheet": {
                **self._creature(character),
                "abilities": {
                    ability: {
                        "score": score,
                        "modifier": rules.ability_modifier(score),
                    }
                    for ability, score in character.abilities.model_dump().items()
                },
                "proficiency_bonus": character.proficiency_bonus,
                "initiative": rules.ability_modifier(character.abilities.dexterity),
                "skills": sorted(character.skill_proficiencies),
                "saves": [a.value for a in character.saving_throw_proficiencies],
                "inventory": [
                    {
                        "name": item.name,
                        "quantity": item.quantity,
                        "kind": item.kind.value,
                        "equipped": item.equipped,
                        "description": item.description,
                    }
                    for item in character.inventory
                ],
                "currency": character.currency.model_dump(),
                "experience": getattr(character, "experience", 0),
                "personality": getattr(character, "personality", None)
                and character.personality.model_dump(),
                "death_saves": character.death_saves.model_dump(),
            }
        }

    # --- view assembly -----------------------------------------------------

    def _view(self) -> dict:
        session = self._open()
        state = session.state
        campaign = session.campaign

        return {
            "campaign": {
                "id": campaign.id,
                "name": campaign.name,
                "premise": campaign.premise,
                "setting": campaign.setting,
                "tone": campaign.settings.tone.value,
                "rules": session.rules.name,
                "events": session.store.head,
                "sandbox": campaign.settings.sandbox_mode,
            },
            "party": [self._creature(c) for c in state.party.values()],
            "active_character_id": self.character_id,
            "chronicle": self._chronicle(),
            "combat": self._combat(),
            "quests": self._quests(),
            "where": self._where(),
            "when": {
                "time": str(state.world.time),
                "day": state.world.time.day,
                "weather": state.world.weather,
            },
            "dm": self._diagnostics(),
            "checkpoints": [
                {
                    "seq": mark.event_seq,
                    "label": mark.label or "Unmarked",
                    "automatic": mark.automatic,
                    "created": f"{mark.created_at:%d %b %H:%M}",
                }
                for mark in session.checkpoints()
            ],
        }

    def _creature(self, creature) -> dict:
        maximum = max(1, creature.hp.maximum)
        return {
            "id": creature.id,
            "name": creature.name,
            "level": creature.level,
            "class": creature.character_class,
            "species": creature.species,
            "hp": {
                "current": creature.hp.current,
                "maximum": creature.hp.maximum,
                "temporary": creature.hp.temporary,
                "fraction": max(0.0, min(1.0, creature.hp.current / maximum)),
            },
            "armor_class": creature.armor_class,
            "speed": creature.speed,
            "conditions": [c.type.value for c in creature.conditions],
            "dead": creature.dead,
            "player": self._player_name(getattr(creature, "player_id", None)),
        }

    def _player_name(self, player_id: str | None) -> str | None:
        if not player_id:
            return None
        session = self._open()
        seat = session.state.players.get(player_id)
        return seat.name if seat else None

    def _chronicle(self) -> list[dict]:
        session = self._open()
        events = session.log_for(self.player_id, limit=CHRONICLE_LIMIT)
        return [
            {
                "seq": event.seq,
                "kind": ENTRY_KINDS.get(event.type, "system"),
                "type": event.type.value,
                "text": event.summary,
                "actor": event.actor_id,
            }
            for event in events
            if event.summary
        ]

    def _combat(self) -> dict:
        session = self._open()
        combat = session.state.combat
        if not combat.active:
            return {"active": False, "round": 0, "order": []}

        order = []
        for index, combatant in enumerate(combat.order):
            creature = session.state.creature(combatant.creature_id)
            if creature is None:
                continue
            order.append(
                {
                    "id": combatant.creature_id,
                    "name": creature.name,
                    "initiative": combatant.initiative,
                    "is_player": combatant.is_player,
                    "current": index == combat.turn_index,
                    "dead": creature.dead,
                    "fled": combatant.fled,
                    "hp": {
                        "current": creature.hp.current,
                        "maximum": creature.hp.maximum,
                        "fraction": max(
                            0.0,
                            min(1.0, creature.hp.current / max(1, creature.hp.maximum)),
                        ),
                    },
                }
            )
        return {"active": True, "round": combat.round, "order": order}

    def _quests(self) -> list[dict]:
        session = self._open()
        journal = session.journal
        quests = []
        for status in (QuestStatus.ACTIVE, QuestStatus.COMPLETED, QuestStatus.FAILED):
            for quest in session.quests.by_status(journal, status):
                quests.append(
                    {
                        "title": quest.title,
                        "summary": quest.summary,
                        "status": status.value,
                        "objectives": [
                            {"description": o.description, "completed": o.completed}
                            for o in quest.objectives
                        ],
                    }
                )
        return quests

    def _where(self) -> dict:
        session = self._open()
        state = session.state
        location_id = state.current_location_id or ""
        return {
            "location": state.location.name if state.location else "Somewhere unmapped",
            "description": state.location.description if state.location else "",
            "npcs": [n.name for n in session.atlas.npcs_at(session.journal, location_id)],
            "exits": [
                loc.name for loc in session.atlas.neighbours(session.journal, location_id)
            ],
        }

    def _diagnostics(self) -> dict:
        """What the DM did last turn -- the control panel of spec section 17."""
        dm = self.dm
        if dm is None:
            return {"provider": "none", "degraded": []}
        last = dm.last
        return {
            "provider": dm.provider.name,
            "provider_id": dm.provider.id,
            "interpreted_by": last.interpreted_by,
            "narrated_by": last.narrated_by,
            "served_by": last.served_by,
            "rationale": last.rationale,
            "checks_requested": last.checks_requested,
            "events_appended": last.events_appended,
            "cost_usd": round(last.cost_usd, 4),
            "degraded": list(last.degraded),
        }


def load_rules_safely() -> dict[str, list[dict]]:
    """Read the default pack's classes and species for the creation form.

    Done through the rules engine rather than by reading JSON, so a pack that
    renames a key breaks here loudly instead of drawing an empty dropdown.
    """
    from dmai.engine.rules import load_rules

    rules = load_rules("srd51")
    classes = getattr(rules, "classes", {})
    species = getattr(rules, "species_data", {})
    return {
        "classes": [
            {"key": key, "name": value.get("name", key.title()), "hit_die": value.get("hit_die")}
            for key, value in sorted(classes.items())
        ],
        "species": [
            {"key": key, "name": value.get("name", key.title())}
            for key, value in sorted(species.items())
        ],
    }


__all__ = ["DesktopBridge", "BridgeError"]
