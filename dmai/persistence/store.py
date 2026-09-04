"""Campaigns on disk (spec sections 10, 16 and 22).

A save is two files, and both are plain text on purpose:

* ``campaign.json`` -- the metadata: name, premise, table settings, save version.
* ``events.jsonl``  -- the event log, one JSON object per line, in sequence order.

The state itself is never written.  It is rebuilt by replaying the log, which
means a save can never disagree with its own history, and a corrupted state
cannot be persisted -- the worst case is a truncated log, which replays to an
earlier, still-consistent moment.

Writes are atomic (temp file, then replace) so a crash mid-save leaves the
previous save intact rather than half of two.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dmai.engine.models.base import utcnow
from dmai.engine.models.campaign import Campaign
from dmai.engine.models.events import GameEvent
from dmai.engine.session import GameSession

CAMPAIGN_FILE = "campaign.json"
EVENTS_FILE = "events.jsonl"

#: Bumped when the on-disk shape changes.  `MIGRATIONS` maps a version to the
#: function that lifts a save of that version to the next one.
CURRENT_SAVE_VERSION = 1

#: version -> function lifting a save of that version to the next one.
Migration = Callable[[dict], dict]
MIGRATIONS: dict[int, Migration] = {}

#: Where campaigns live when nobody says otherwise.  ``DMAI_HOME`` moves the
#: whole data directory, which is what a portable/USB install needs.
DEFAULT_ROOT_ENV = "DMAI_HOME"


class SaveError(RuntimeError):
    """A campaign could not be read or written."""


@dataclass(frozen=True)
class CampaignSummary:
    """Enough to draw a load-game list without replaying anything."""

    id: str
    name: str
    premise: str
    updated_at: datetime
    events: int
    path: Path

    def describe(self) -> str:
        return f"{self.name} ({self.events} events, updated {self.updated_at:%Y-%m-%d %H:%M})"


class CampaignStore:
    """A directory of campaigns, one subdirectory each."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else default_root()

    # --- writing -----------------------------------------------------------

    def save(self, session: GameSession) -> Path:
        """Persist a session.  Cheap enough to call after every action.

        Only the events not already on disk are written, unless the log has
        been rolled back -- in which case the file is rewritten, because a
        rolled-back campaign must not keep a future it no longer has.
        """
        directory = self.path_for(session.campaign.id)
        directory.mkdir(parents=True, exist_ok=True)

        # Stamp the live campaign, not a copy, so what is in memory and what is
        # on disk agree -- a reloaded session must equal the one that saved it.
        campaign = session.campaign
        campaign.updated_at = utcnow()
        campaign.save_version = CURRENT_SAVE_VERSION
        _write_atomic(directory / CAMPAIGN_FILE, campaign.model_dump_json(indent=2))

        events_path = directory / EVENTS_FILE
        on_disk = _last_seq(events_path)
        head = session.store.head

        if on_disk > head:  # a rollback happened: rewrite the whole log
            _write_atomic(events_path, _serialise(session.store.all()))
        elif on_disk < head:
            with events_path.open("a", encoding="utf-8") as handle:
                handle.write(_serialise(session.store.since(on_disk)))
        return directory

    def delete(self, campaign_id: str) -> None:
        directory = self.path_for(campaign_id)
        if directory.is_dir():
            shutil.rmtree(directory)

    # --- reading -----------------------------------------------------------

    def exists(self, campaign_id: str) -> bool:
        return (self.path_for(campaign_id) / CAMPAIGN_FILE).is_file()

    def load(self, campaign_id: str, **kwargs) -> GameSession:
        """Replay a campaign back into a live session."""
        directory = self.path_for(campaign_id)
        if not self.exists(campaign_id):
            raise SaveError(f"no campaign saved at {directory}")

        payload = _read_json(directory / CAMPAIGN_FILE)
        payload = migrate(payload)
        campaign = Campaign.model_validate(payload)
        events = list(read_events(directory / EVENTS_FILE))
        return GameSession.restore(campaign, events, **kwargs)

    def list_campaigns(self) -> list[CampaignSummary]:
        """Every saved campaign, most recently updated first."""
        if not self.root.is_dir():
            return []
        summaries: list[CampaignSummary] = []
        for directory in sorted(self.root.iterdir()):
            metadata = directory / CAMPAIGN_FILE
            if not metadata.is_file():
                continue
            try:
                campaign = Campaign.model_validate(migrate(_read_json(metadata)))
            except (SaveError, ValueError):
                continue  # a broken save should not hide the working ones
            summaries.append(
                CampaignSummary(
                    id=campaign.id,
                    name=campaign.name,
                    premise=campaign.premise,
                    updated_at=campaign.updated_at,
                    events=_last_seq(directory / EVENTS_FILE),
                    path=directory,
                )
            )
        return sorted(summaries, key=lambda s: s.updated_at, reverse=True)

    # --- moving a campaign between machines --------------------------------

    def export(self, campaign_id: str, destination: Path | str) -> Path:
        """Write one self-contained JSON bundle (spec section 16)."""
        session_dir = self.path_for(campaign_id)
        if not self.exists(campaign_id):
            raise SaveError(f"no campaign saved at {session_dir}")
        bundle = {
            "format": "dmai-campaign",
            "save_version": CURRENT_SAVE_VERSION,
            "campaign": _read_json(session_dir / CAMPAIGN_FILE),
            "events": [
                json.loads(line) for line in _lines(session_dir / EVENTS_FILE)
            ],
        }
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(destination, json.dumps(bundle, indent=2))
        return destination

    def import_bundle(self, source: Path | str, *, new_id: str | None = None) -> Campaign:
        """Read a bundle back in, optionally under a fresh campaign id.

        A fresh id is how the same bundle is imported twice -- for a rewind, or
        to run two tables through the same starting campaign.
        """
        bundle = _read_json(Path(source))
        if bundle.get("format") != "dmai-campaign":
            raise SaveError(f"{source} is not a DungeonMaster AI campaign bundle")

        campaign = Campaign.model_validate(migrate(bundle["campaign"]))
        events = [GameEvent.model_validate(e) for e in bundle.get("events", [])]
        if new_id:
            campaign = campaign.model_copy(update={"id": new_id})

        directory = self.path_for(campaign.id)
        directory.mkdir(parents=True, exist_ok=True)
        _write_atomic(directory / CAMPAIGN_FILE, campaign.model_dump_json(indent=2))
        _write_atomic(directory / EVENTS_FILE, _serialise(events))
        return campaign

    # --- paths -------------------------------------------------------------

    def path_for(self, campaign_id: str) -> Path:
        """Where a campaign lives.  Ids are engine-generated and slug-safe, but
        this refuses anything that could climb out of the store."""
        if not campaign_id or "/" in campaign_id or "\\" in campaign_id or campaign_id.startswith("."):
            raise SaveError(f"unsafe campaign id: {campaign_id!r}")
        return self.root / campaign_id


def default_root() -> Path:
    """The default campaign directory, honouring ``DMAI_HOME``."""
    home = os.environ.get(DEFAULT_ROOT_ENV)
    base = Path(home) if home else Path.home() / ".dmai"
    return base / "campaigns"


# --- migrations ------------------------------------------------------------


def migrate(payload: dict) -> dict:
    """Lift an old save forward, one version at a time.

    An unknown *future* version is refused rather than guessed at; an old one
    is migrated.  This is the whole reason `save_version` is written down.
    """
    version = int(payload.get("save_version", 1))
    if version > CURRENT_SAVE_VERSION:
        raise SaveError(
            f"this save was written by a newer build (save version {version}; "
            f"this build understands {CURRENT_SAVE_VERSION})"
        )
    while version < CURRENT_SAVE_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise SaveError(f"no migration from save version {version}")
        payload = step(payload)  # type: ignore[operator]
        version = int(payload.get("save_version", version + 1))
    return payload


# --- file helpers ----------------------------------------------------------


def read_events(path: Path) -> list[GameEvent]:
    """Parse an event log.  A truncated final line is dropped, not fatal.

    A half-written last line means the process died mid-append; the campaign
    replays to the moment before, which is exactly the guarantee an append-only
    log is supposed to give.
    """
    events: list[GameEvent] = []
    for line in _lines(path):
        try:
            events.append(GameEvent.model_validate_json(line))
        except ValueError:
            break
    return events


def _lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _serialise(events: list[GameEvent]) -> str:
    return "".join(event.model_dump_json() + "\n" for event in events)


def _last_seq(path: Path) -> int:
    """Highest sequence number on disk, or 0 for an absent or empty log."""
    lines = _lines(path)
    while lines:
        try:
            return int(json.loads(lines[-1])["seq"])
        except (ValueError, KeyError):
            lines.pop()  # trailing partial write; look at the line before it
    return 0


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise SaveError(f"missing save file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SaveError(f"{path} is not valid JSON: {exc}") from exc


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file so a crash cannot leave a half-written save."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


__all__ = [
    "CURRENT_SAVE_VERSION",
    "CampaignStore",
    "CampaignSummary",
    "SaveError",
    "default_root",
    "migrate",
    "read_events",
]
