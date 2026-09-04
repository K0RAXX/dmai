"""The OpenClaw adapter (spec sections 19 and 20).

The spec is emphatic about what this must *not* be: OpenClaw does not drive a
GUI with simulated clicks.  It calls a real programmatic interface, and this is
it -- the same operations the spec names, each one a thin call onto a
`GameSession` and the DM agent.

    OpenClaw -> DungeonMaster agent -> game engine -> campaign store

Two properties matter more than the method list:

* **Campaigns never mix.**  Sessions are cached per campaign id, a player is
  routed by their chat identity to the one campaign they are seated at, and no
  method reads state it was not asked for.  A bot serving three tables serves
  three separate worlds (spec section 20).
* **Every reply is JSON.**  Nothing here returns a `GameSession` or a pydantic
  model, so a chat transport can serialise a response without knowing anything
  about the engine.

Everything is saved after any call that changes state, so a bot that dies
between messages loses at most the message it was handling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dmai.ai.dm_agent.agent import DungeonMaster
from dmai.ai.providers import AIProvider, OfflineProvider
from dmai.engine.characters.builder import export_character
from dmai.engine.models.base import Visibility
from dmai.engine.models.campaign import Campaign, CampaignSettings
from dmai.engine.session import GameSession
from dmai.persistence import CampaignStore, SaveError


#: Chat-room bindings live beside the campaigns they point at, so moving a
#: store moves its tables with it.
CHANNELS_FILE = "channels.json"


def _read_channels(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}  # a corrupt binding file loses routing, never a campaign
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _write_channels(path: Path, channels: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(channels, indent=2), encoding="utf-8")
    temporary.replace(path)


class OpenClawError(RuntimeError):
    """The adapter was asked for a campaign, player or table it cannot serve."""


class OpenClawAdapter:
    """A headless, multi-table Dungeon Master.

    One adapter can run any number of campaigns.  It holds an open session per
    campaign so a busy table is not replayed from its log on every message, and
    it writes through to disk after every state change.
    """

    def __init__(
        self,
        store: CampaignStore | None = None,
        *,
        provider: AIProvider | None = None,
    ):
        self.store = store or CampaignStore()
        self.provider = provider or OfflineProvider()
        self._sessions: dict[str, GameSession] = {}
        self._agents: dict[str, DungeonMaster] = {}
        self._channels: dict[str, str] | None = None

    # --- channels ----------------------------------------------------------
    #
    # A chat room is a table.  Routing by player identity alone breaks the
    # moment someone plays in two games -- and in a group chat it is the
    # *room*, not the person, that says which campaign a message belongs to.

    @property
    def channels(self) -> dict[str, str]:
        """channel id -> campaign id, loaded from disk on first use."""
        if self._channels is None:
            self._channels = _read_channels(self.store.root / CHANNELS_FILE)
        return self._channels

    def bind_channel(self, channel_id: str, campaign_id: str) -> dict:
        """Point a chat room at a campaign.  Survives a restart."""
        self._session(campaign_id)  # fail now if the campaign is not real
        self.channels[channel_id] = campaign_id
        _write_channels(self.store.root / CHANNELS_FILE, self.channels)
        return {"channel_id": channel_id, "campaign_id": campaign_id, "bound": True}

    def unbind_channel(self, channel_id: str) -> dict:
        """Forget a room's table.  The campaign itself is untouched."""
        removed = self.channels.pop(channel_id, None)
        _write_channels(self.store.root / CHANNELS_FILE, self.channels)
        return {"channel_id": channel_id, "campaign_id": removed, "bound": False}

    def channel_binding(self, channel_id: str) -> dict:
        return {
            "channel_id": channel_id,
            "campaign_id": self.channels.get(channel_id),
        }

    def list_channels(self) -> list[dict]:
        return [
            {"channel_id": channel, "campaign_id": campaign}
            for channel, campaign in sorted(self.channels.items())
        ]

    # --- campaigns ---------------------------------------------------------

    def create_campaign(
        self,
        name: str,
        *,
        premise: str = "",
        setting: str = "",
        settings: dict[str, Any] | None = None,
        channel_id: str | None = None,
    ) -> dict:
        campaign = Campaign(
            name=name,
            premise=premise,
            setting=setting,
            settings=CampaignSettings(**(settings or {})),
        )
        session = GameSession.create(campaign)
        self._adopt(session)
        self.store.save(session)
        if channel_id is not None:
            # One call to start a table in a room, so a chat command cannot
            # half-succeed into a campaign nobody can reach.
            self.bind_channel(channel_id, campaign.id)
        return self._campaign_summary(session)

    def load_campaign(self, campaign_id: str) -> dict:
        """Open a campaign, replaying it from its log if it is not already open."""
        return self._campaign_summary(self._session(campaign_id))

    def list_campaigns(self) -> list[dict]:
        return [
            {
                "campaign_id": s.id,
                "name": s.name,
                "premise": s.premise,
                "events": s.events,
                "updated_at": s.updated_at.isoformat(),
            }
            for s in self.store.list_campaigns()
        ]

    def save_campaign(self, campaign_id: str) -> dict:
        session = self._session(campaign_id)
        self.store.save(session)
        return {"campaign_id": campaign_id, "events": session.store.head, "saved": True}

    def close_campaign(self, campaign_id: str) -> dict:
        """Save and drop a table from memory.  Safe to call on an idle campaign."""
        if campaign_id in self._sessions:
            self.store.save(self._sessions[campaign_id])
            self._sessions.pop(campaign_id)
            self._agents.pop(campaign_id, None)
        return {"campaign_id": campaign_id, "closed": True}

    # --- the table ---------------------------------------------------------

    def join(
        self, campaign_id: str, name: str, *, external_id: str, is_host: bool = False
    ) -> dict:
        """Seat a chat identity at a table.  This is what routing depends on."""
        session = self._session(campaign_id)
        existing = session.player_by_external_id(external_id)
        player = existing or session.add_player(
            name, external_id=external_id, is_host=is_host
        )
        self.store.save(session)
        return {
            "campaign_id": campaign_id,
            "player_id": player.id,
            "name": player.name,
            "external_id": player.external_id,
            "characters": list(player.character_ids),
        }

    def create_character(
        self,
        campaign_id: str,
        name: str,
        *,
        species: str = "human",
        character_class: str = "fighter",
        level: int = 1,
        external_id: str | None = None,
        player_id: str | None = None,
    ) -> dict:
        session = self._session(campaign_id)
        if player_id is None and external_id is not None:
            seat = session.player_by_external_id(external_id)
            player_id = seat.id if seat else None
        character = session.add_character(
            name, species, character_class, level, player_id=player_id
        )
        self.store.save(session)
        return export_character(character)

    def get_character_sheet(self, campaign_id: str, character_id: str) -> dict:
        session = self._session(campaign_id)
        character = session.state.party.get(character_id)
        if character is None:
            raise OpenClawError(f"no character {character_id} in {campaign_id}")
        return export_character(character)

    # --- play --------------------------------------------------------------

    def submit_player_action(
        self,
        campaign_id: str,
        text: str,
        *,
        external_id: str | None = None,
        player_id: str | None = None,
        character_id: str | None = None,
    ) -> dict:
        """The main entry point: one player message in, one DM reply out."""
        session = self._session(campaign_id)
        player_id, character_id = self._seat(session, external_id, player_id, character_id)

        action = session.player_says(text, character_id=character_id, player_id=player_id)
        result = self._agent(campaign_id).take_turn(action)
        self.store.save(session)

        diagnostics = self._agent(campaign_id).last
        return {
            "campaign_id": campaign_id,
            "player_id": player_id,
            "character_id": character_id,
            # A seat with no character cannot roll anything, so every check the
            # DM asks for is dropped and the turn reads as "nothing happened".
            # Say so plainly instead: the caller should offer to roll one up.
            "needs_character": character_id is None,
            "narration": result.narration,
            "intent": result.interpretation.intent.value if result.interpretation else None,
            # A short player-facing explanation, never the model's reasoning
            # (spec section 17).
            "explanation": diagnostics.rationale,
            "rolls": [r.model_dump(mode="json") for r in result.rolls],
            "events": len(result.event_ids),
            "cost_usd": result.cost_usd,
        }

    def handle_message(
        self,
        external_id: str,
        text: str,
        *,
        campaign_id: str | None = None,
        channel_id: str | None = None,
        display_name: str | None = None,
    ) -> dict:
        """Route one chat message to the right table and answer it.

        The full section-20 flow: identify the player, find their campaign, load
        its state, resolve the action, persist, reply.

        A campaign is chosen in this order, most specific first:

        1. ``campaign_id``, when the caller already knows it;
        2. the campaign bound to ``channel_id`` -- the room is the table, which
           is what makes a group chat work;
        3. the one campaign this player is seated at.

        An ambiguous identity is an error rather than a guess: the one thing
        worse than not answering is answering in the wrong world.

        In a bound room, a player nobody has seated yet is seated on their
        first message when ``display_name`` is given -- joining a game should
        not need a separate command. That never happens in an unbound room, so
        a stray message cannot conjure a seat at someone else's table.
        """
        if campaign_id is None and channel_id is not None:
            campaign_id = self.channels.get(channel_id)
            if campaign_id is not None and display_name:
                self._ensure_seated(campaign_id, external_id, display_name)
        campaign_id = campaign_id or self.campaign_for(external_id)
        return self.submit_player_action(campaign_id, text, external_id=external_id)

    def _ensure_seated(self, campaign_id: str, external_id: str, display_name: str) -> None:
        session = self._session(campaign_id)
        if session.player_by_external_id(external_id) is None:
            self.join(
                campaign_id,
                display_name,
                external_id=external_id,
                is_host=not session.state.players,
            )

    def campaign_for(self, external_id: str) -> str:
        """Which table this chat identity is seated at."""
        seats = [
            summary["campaign_id"]
            for summary in self.list_campaigns()
            if self._session(summary["campaign_id"]).player_by_external_id(external_id)
        ]
        if not seats:
            raise OpenClawError(f"{external_id} is not seated at any campaign")
        if len(set(seats)) > 1:
            raise OpenClawError(
                f"{external_id} is seated at several campaigns "
                f"({', '.join(sorted(set(seats)))}); name one"
            )
        return seats[0]

    def send_private_message(self, campaign_id: str, player_id: str, text: str) -> dict:
        """A whisper to one seat, invisible to the rest of the table."""
        session = self._session(campaign_id)
        if player_id not in session.state.players:
            raise OpenClawError(f"no player {player_id} at {campaign_id}")
        event = session.whisper(player_id, text)
        self.store.save(session)
        return {"campaign_id": campaign_id, "player_id": player_id, "event_id": event.id}

    def roll_dice(
        self, campaign_id: str, expression: str, *, reason: str = "", actor_id: str | None = None
    ) -> dict:
        session = self._session(campaign_id)
        result = session.roll(expression, reason=reason, actor_id=actor_id)
        self.store.save(session)
        return result.model_dump(mode="json")

    # --- combat ------------------------------------------------------------

    def start_combat(
        self, campaign_id: str, participant_ids: list[str], *, surprised_ids: list[str] | None = None
    ) -> dict:
        session = self._session(campaign_id)
        combat = session.combat.start(
            session.journal, participant_ids, surprised_ids=surprised_ids
        )
        self.store.save(session)
        return combat.model_dump(mode="json")

    def advance_turn(self, campaign_id: str) -> dict:
        session = self._session(campaign_id)
        combatant = session.combat.advance_turn(session.journal)
        self.store.save(session)
        return {
            "campaign_id": campaign_id,
            "active": session.state.combat.active,
            "round": session.state.combat.round,
            "current": combatant.model_dump(mode="json") if combatant else None,
            "over": session.combat.is_over(session.journal),
        }

    def end_combat(self, campaign_id: str, reason: str = "") -> dict:
        session = self._session(campaign_id)
        session.combat.end(session.journal, reason)
        self.store.save(session)
        return {"campaign_id": campaign_id, "active": session.state.combat.active}

    # --- reading state -----------------------------------------------------

    def get_game_state(self, campaign_id: str, *, player_id: str | None = None) -> dict:
        """The briefing, plus the log as this seat is allowed to see it."""
        session = self._session(campaign_id)
        return {
            "campaign_id": campaign_id,
            "campaign": session.campaign.name,
            "briefing": session.briefing(),
            "transcript": session.transcript(player_id, is_dm=player_id is None, limit=25),
            "events": session.store.head,
        }

    def get_quest_state(self, campaign_id: str) -> dict:
        session = self._session(campaign_id)
        return {
            "campaign_id": campaign_id,
            "quests": [
                {
                    "id": quest.id,
                    "title": quest.title,
                    "status": quest.status.value,
                    "summary": quest.summary,
                    "objectives": [
                        {"description": o.description, "completed": o.completed}
                        for o in quest.objectives
                        if o.visibility is not Visibility.DM_ONLY
                    ],
                }
                for quest in session.state.story.quests.values()
            ],
            "discoveries": session.state.story.discoveries,
        }

    def recap(self, campaign_id: str) -> dict:
        session = self._session(campaign_id)
        return {
            "campaign_id": campaign_id,
            "recap": self._agent(campaign_id).session_recap(),
            "facts": session.recap(),
        }

    def resume(self, campaign_id: str) -> dict:
        """What the DM says when the table sits back down (spec section 31)."""
        session = self._session(campaign_id)
        narration = self._agent(campaign_id).resume()
        self.store.save(session)
        return {"campaign_id": campaign_id, "narration": narration}

    # --- dispatch ----------------------------------------------------------

    def dispatch(self, op: str, args: dict | None = None) -> Any:
        """Invoke one named operation with keyword arguments.

        This is the contract a transport speaks: a name and a dict, in, and
        JSON-ready data out.  Restricted to `CALLABLE_OPERATIONS`, so a chat
        message cannot reach an attribute that was never meant to be remote.
        """
        if op not in CALLABLE_OPERATIONS:
            raise OpenClawError(
                f"unknown operation {op!r}; available: {', '.join(sorted(CALLABLE_OPERATIONS))}"
            )
        try:
            return getattr(self, op)(**(args or {}))
        except TypeError as exc:
            # Wrong or missing arguments from the caller, not a bug in here.
            raise OpenClawError(f"{op}: {exc}") from exc

    # --- internals ---------------------------------------------------------

    def _session(self, campaign_id: str) -> GameSession:
        """The open session for a campaign, loading it if need be.

        The cache is keyed by campaign id and nothing else, which is the whole
        of the "never mix state between campaigns" guarantee.
        """
        if campaign_id not in self._sessions:
            try:
                session = self.store.load(campaign_id)
            except SaveError as exc:
                raise OpenClawError(str(exc)) from exc
            self._adopt(session)
        return self._sessions[campaign_id]

    def _agent(self, campaign_id: str) -> DungeonMaster:
        if campaign_id not in self._agents:
            self._agents[campaign_id] = DungeonMaster(
                self._session(campaign_id), self.provider
            )
        return self._agents[campaign_id]

    def _adopt(self, session: GameSession) -> None:
        self._sessions[session.campaign.id] = session
        self._agents.pop(session.campaign.id, None)

    @staticmethod
    def _seat(
        session: GameSession,
        external_id: str | None,
        player_id: str | None,
        character_id: str | None,
    ) -> tuple[str | None, str | None]:
        """Work out who is speaking and which character they control."""
        if player_id is None and external_id is not None:
            seat = session.player_by_external_id(external_id)
            if seat is None:
                raise OpenClawError(f"{external_id} is not seated at this campaign")
            player_id = seat.id
        if character_id is None and player_id in session.state.players:
            characters = session.state.players[player_id].character_ids
            character_id = characters[0] if characters else None
        return player_id, character_id

    @staticmethod
    def _campaign_summary(session: GameSession) -> dict:
        return {
            "campaign_id": session.campaign.id,
            "name": session.campaign.name,
            "premise": session.campaign.premise,
            "rules": session.rules.id,
            "events": session.store.head,
            "party": [
                {"id": c.id, "name": c.name, "level": c.level}
                for c in session.state.party.values()
            ],
        }


#: The operations spec section 19 names, for adapters that want to advertise a
#: tool schema without importing the class.
OPERATIONS = (
    "create_campaign",
    "load_campaign",
    "create_character",
    "get_game_state",
    "submit_player_action",
    "roll_dice",
    "start_combat",
    "advance_turn",
    "get_character_sheet",
    "get_quest_state",
    "send_private_message",
    "save_campaign",
)

#: Everything a remote message is allowed to invoke.
#:
#: An allowlist rather than `getattr`, because the caller is a chat transport:
#: without this, a crafted message could reach `_session`, `store`, or any
#: other attribute on the adapter.  Adding an operation here is a deliberate
#: act, which is the point.
CALLABLE_OPERATIONS = (
    *OPERATIONS,
    "handle_message",
    "campaign_for",
    "bind_channel",
    "unbind_channel",
    "channel_binding",
    "list_channels",
    "list_campaigns",
    "close_campaign",
    "join",
    "end_combat",
    "recap",
    "resume",
)


__all__ = [
    "CALLABLE_OPERATIONS",
    "CHANNELS_FILE",
    "OPERATIONS",
    "OpenClawAdapter",
    "OpenClawError",
]
