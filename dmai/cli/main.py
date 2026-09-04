"""The command-line client.

This is a *client* of the engine, not the engine (spec section 2).  Every
command here does the same thing an HTTP route or the OpenClaw adapter will
do: build or load a `GameSession`, call engine methods, save.  Nothing about
the rules lives in this file, and the CLI can be deleted without the game
losing a single feature.

Run ``dmai --help``, or ``dmai play <campaign>`` to sit down at a table.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from dmai.ai.dm_agent.agent import DungeonMaster
from dmai.ai.providers import available_providers, load_provider
from dmai.engine.models import Campaign, CampaignSettings, StoryTone
from dmai.engine.models.quests import QuestStatus
from dmai.engine.rules import available_rules
from dmai.engine.session import GameSession
from dmai.persistence import CampaignStore, SaveError

app = typer.Typer(
    add_completion=False,
    help="DungeonMaster AI -- an adaptive tabletop RPG engine.",
    no_args_is_help=True,
)
console = Console()

#: Styles that keep narration, dialogue, rolls and system chatter apart
#: (spec section 4: visual differentiation).
STYLES = {
    "narration": "italic bright_white",
    "dialogue": "cyan",
    "roll": "yellow",
    "combat": "bold red",
    "system": "dim",
    "quest": "magenta",
}


def _store(root: Path | None) -> CampaignStore:
    return CampaignStore(root)


def _dungeon_master(
    session: GameSession, provider: str | None = None, model: str | None = None
) -> DungeonMaster:
    """Build the DM for a session, honouring the campaign's own settings.

    A provider named on the command line wins over the campaign's, so a table
    can try a model for one sitting without changing the save.
    """
    settings = session.campaign.settings
    provider_id = provider or settings.ai_provider
    chosen = model or settings.ai_model
    kwargs = {"model": chosen} if chosen and provider_id != "offline" else {}
    return DungeonMaster(session, load_provider(provider_id, **kwargs))


def _load(store: CampaignStore, campaign: str) -> GameSession:
    """Find a campaign by id or by name, so nobody has to type a uuid."""
    if store.exists(campaign):
        return store.load(campaign)
    matches = [s for s in store.list_campaigns() if s.name.lower() == campaign.lower()]
    if not matches:
        matches = [s for s in store.list_campaigns() if campaign.lower() in s.name.lower()]
    if not matches:
        raise typer.BadParameter(f"no campaign matching {campaign!r}")
    if len(matches) > 1:
        names = ", ".join(f"{s.name} ({s.id})" for s in matches)
        raise typer.BadParameter(f"{campaign!r} matches several campaigns: {names}")
    return store.load(matches[0].id)


# --- campaign management ---------------------------------------------------


@app.command("new")
def new_campaign(
    name: str = typer.Argument(..., help="What to call this campaign."),
    premise: str = typer.Option("", "--premise", "-p", help="The hook, in a sentence."),
    setting: str = typer.Option("", "--setting", help="Where it takes place."),
    tone: StoryTone = typer.Option(StoryTone.HEROIC, "--tone", help="Story tone."),
    rules: str = typer.Option("srd51", "--rules", help=f"One of: {', '.join(available_rules())}"),
    seed: int = typer.Option(None, "--seed", help="Fix the dice for a reproducible game."),
    sandbox: bool = typer.Option(False, "--sandbox", help="Let the world move on its own."),
    provider: str = typer.Option(
        "offline", "--provider", help=f"DM AI: {', '.join(available_providers())}"
    ),
    model: str = typer.Option("", "--model", help="Model id, when the provider takes one."),
    root: Path = typer.Option(None, "--root", help="Where campaigns are stored."),
) -> None:
    """Create a campaign and save it."""
    store = _store(root)
    session = GameSession.create(
        Campaign(
            name=name,
            premise=premise,
            setting=setting,
            settings=CampaignSettings(
                tone=tone,
                rules_pack=rules,
                rng_seed=seed,
                sandbox_mode=sandbox,
                ai_provider=provider,
                ai_model=model,
            ),
        )
    )
    store.save(session)
    console.print(
        Panel(
            f"[bold]{name}[/bold]\n{premise or 'No premise set yet.'}\n\n"
            f"[dim]id: {session.campaign.id}  rules: {session.rules.name}  "
            f"DM: {_dungeon_master(session).provider.name}[/dim]",
            title="Campaign created",
            border_style="green",
        )
    )
    console.print(f"Next: [bold]dmai character add {name!r} <character name>[/bold]")


@app.command("list")
def list_campaigns(root: Path = typer.Option(None, "--root")) -> None:
    """Show every saved campaign."""
    summaries = _store(root).list_campaigns()
    if not summaries:
        console.print("[dim]No campaigns yet. Try: dmai new \"My Campaign\"[/dim]")
        raise typer.Exit()

    table = Table(title="Campaigns", header_style="bold")
    table.add_column("Name")
    table.add_column("Premise", overflow="fold")
    table.add_column("Events", justify="right")
    table.add_column("Updated")
    table.add_column("Id", style="dim")
    for summary in summaries:
        table.add_row(
            summary.name,
            summary.premise or "-",
            str(summary.events),
            f"{summary.updated_at:%Y-%m-%d %H:%M}",
            summary.id,
        )
    console.print(table)


@app.command("export")
def export_campaign(
    campaign: str,
    destination: Path = typer.Argument(..., help="Where to write the bundle."),
    root: Path = typer.Option(None, "--root"),
) -> None:
    """Write a campaign to a single portable JSON file."""
    store = _store(root)
    session = _load(store, campaign)
    path = store.export(session.campaign.id, destination)
    console.print(f"[green]Exported[/green] {session.campaign.name} -> {path}")


@app.command("import")
def import_campaign(
    bundle: Path = typer.Argument(..., help="A bundle written by 'dmai export'."),
    new_id: str = typer.Option(None, "--as", help="Import under a fresh campaign id."),
    root: Path = typer.Option(None, "--root"),
) -> None:
    """Read a campaign bundle back in."""
    campaign = _store(root).import_bundle(bundle, new_id=new_id)
    console.print(f"[green]Imported[/green] {campaign.name} ({campaign.id})")


# --- characters ------------------------------------------------------------

characters = typer.Typer(help="Create and inspect characters.", no_args_is_help=True)
app.add_typer(characters, name="character")


@characters.command("add")
def add_character(
    campaign: str,
    name: str,
    species: str = typer.Option("human", "--species"),
    character_class: str = typer.Option("fighter", "--class"),
    level: int = typer.Option(1, "--level", min=1),
    player: str = typer.Option(None, "--player", help="Name of the human playing them."),
    root: Path = typer.Option(None, "--root"),
) -> None:
    """Roll up a character and add them to the party."""
    store = _store(root)
    session = _load(store, campaign)

    player_id = None
    if player:
        seat = next(
            (p for p in session.state.players.values() if p.name.lower() == player.lower()),
            None,
        )
        player_id = (seat or session.add_player(player, is_host=not session.state.players)).id

    character = session.add_character(
        name, species, character_class, level, player_id=player_id
    )
    store.save(session)
    _print_sheet(session, character.id)


@characters.command("sheet")
def show_sheet(
    campaign: str,
    name: str = typer.Argument(None, help="Whose sheet. Omit to list the party."),
    root: Path = typer.Option(None, "--root"),
) -> None:
    """Show a character sheet, or the whole party."""
    session = _load(_store(root), campaign)
    if name is None:
        _print_party(session)
        return
    character = next(
        (c for c in session.state.party.values() if c.name.lower() == name.lower()), None
    )
    if character is None:
        raise typer.BadParameter(f"{name!r} is not in the party")
    _print_sheet(session, character.id)


# --- reading the campaign --------------------------------------------------


@app.command("status")
def status(campaign: str, root: Path = typer.Option(None, "--root")) -> None:
    """Where the campaign stands right now (spec section 31)."""
    session = _load(_store(root), campaign)
    briefing = session.briefing()

    console.print(
        Panel(
            f"[bold]{session.campaign.name}[/bold]\n{session.campaign.premise}",
            border_style="blue",
        )
    )
    _print_party(session)

    where = briefing["where"]
    console.print(f"\n[bold]Where:[/bold] {where['location'] or 'nowhere yet'}")
    if where["npcs_present"]:
        console.print(f"  Present: {', '.join(where['npcs_present'])}")
    if where["exits"]:
        console.print(f"  Exits: {', '.join(where['exits'])}")
    console.print(f"[bold]When:[/bold] {briefing['when']['time']}, {briefing['when']['weather']}")

    if briefing["active_quests"]:
        console.print("\n[bold]Active quests[/bold]")
        for quest in briefing["active_quests"]:
            console.print(f"  [magenta]{quest['title']}[/magenta]")
            for objective in quest["objectives"]:
                console.print(f"    [ ] {objective}")

    if briefing["factions"]:
        console.print("\n[bold]Factions[/bold]")
        for faction in briefing["factions"]:
            console.print(f"  {faction['name']}: standing {faction['party_standing']:+d}")

    if session.state.combat.active:
        console.print(f"\n[bold red]In combat[/bold red] -- round {session.state.combat.round}")


@app.command("log")
def show_log(
    campaign: str,
    limit: int = typer.Option(30, "--limit", "-n"),
    player: str = typer.Option(None, "--as", help="See the log as this player sees it."),
    root: Path = typer.Option(None, "--root"),
) -> None:
    """Replay the campaign's narrative log."""
    session = _load(_store(root), campaign)
    player_id = None
    if player:
        seat = next(
            (p for p in session.state.players.values() if p.name.lower() == player.lower()),
            None,
        )
        if seat is None:
            raise typer.BadParameter(f"no player called {player!r}")
        player_id = seat.id

    for line in session.transcript(player_id, is_dm=player is None, limit=limit):
        console.print(line)


@app.command("recap")
def recap(campaign: str, root: Path = typer.Option(None, "--root")) -> None:
    """A session summary, assembled from the log (spec section 30)."""
    session = _load(_store(root), campaign)
    summary = session.recap()
    console.print(Panel(summary["narrative"], title="Recap", border_style="blue"))
    if summary["unresolved"]:
        console.print("\n[bold]Unresolved[/bold]")
        for item in summary["unresolved"]:
            console.print(f"  - {item}")


# --- playing ---------------------------------------------------------------


@app.command("play")
def play(
    campaign: str,
    player: str = typer.Option(None, "--as", help="Which seat you are in."),
    provider: str = typer.Option(None, "--provider", help="Override the campaign's DM AI."),
    model: str = typer.Option(None, "--model", help="Override the campaign's model."),
    resume: bool = typer.Option(False, "--resume", help="Have the DM recap where you left off."),
    root: Path = typer.Option(None, "--root"),
) -> None:
    """Sit down at the table.

    Everything typed that is not a slash command goes through the DM's
    reasoning loop: interpret, resolve in the engine, narrate the result.
    With the offline DM that narration is terse but always true; with a model
    provider it is the same facts, told well.
    """
    store = _store(root)
    session = _load(store, campaign)
    dm = _dungeon_master(session, provider, model)
    sitting = session.begin_session()

    console.print(
        Panel(
            f"[bold]{session.campaign.name}[/bold]\n{session.campaign.premise}\n\n"
            f"[dim]DM: {dm.provider.name}. "
            "Type an action, or /help for commands. /quit saves and leaves.[/dim]",
            border_style="green",
        )
    )
    for line in session.transcript(is_dm=True, limit=10):
        console.print(f"[dim]{line}[/dim]")

    if resume:
        console.print()
        console.print(f"[{STYLES['narration']}]{dm.resume()}[/]")

    player_id = None
    character_id = None
    if player:
        seat = next(
            (p for p in session.state.players.values() if p.name.lower() == player.lower()),
            None,
        )
        if seat:
            player_id = seat.id
            character_id = seat.character_ids[0] if seat.character_ids else None

    while True:
        try:
            text = console.input("\n[bold green]> [/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            text = "/quit"

        if not text:
            continue
        if text.startswith("/"):
            if _handle_command(session, store, dm, text):
                break
            continue

        action = session.player_says(text, character_id=character_id, player_id=player_id)
        result = dm.take_turn(action)
        _print_turn(dm, result)
        store.save(session)

    session.end_session(sitting)
    store.save(session)
    console.print(f"[green]Saved.[/green] {session.store.head} events on record.")


def _print_turn(dm: DungeonMaster, result) -> None:
    """Show one DM turn: the rolls the table can see, then the narration."""
    for roll in result.rolls:
        console.print(f"[{STYLES['roll']}]{roll.describe()}[/]")
    if result.narration:
        console.print()
        console.print(f"[{STYLES['narration']}]{result.narration}[/]")
    for note in dm.last.degraded:
        console.print(f"[{STYLES['system']}]({note})[/]")


def _handle_command(
    session: GameSession, store: CampaignStore, dm: DungeonMaster, text: str
) -> bool:
    """Run a slash command.  Returns True when it is time to leave the table."""
    command, _, argument = text[1:].partition(" ")
    argument = argument.strip()

    if command in {"quit", "exit", "q"}:
        return True

    if command == "help":
        console.print(
            "\n".join(
                [
                    "  /roll <expr>        roll dice, e.g. /roll 1d20+3",
                    "  /narrate <text>     add DM narration to the log",
                    "  /dm                 which DM is answering, and why",
                    "  /recap              a recap of the session so far",
                    "  /scene <direction>  ask the DM to open or set a scene",
                    "  /party              show the party",
                    "  /status             where things stand",
                    "  /quests             the quest board",
                    "  /save               save now (play autosaves anyway)",
                    "  /checkpoint <label> mark a point you can roll back to",
                    "  /rollback <seq>     rewind the campaign to that point",
                    "  /quit               save and leave",
                ]
            )
        )
    elif command == "roll":
        result = session.roll(argument or "1d20", reason="table roll")
        console.print(f"[{STYLES['roll']}]{result.describe()}[/]")
    elif command == "narrate":
        session.narrate(argument)
        console.print(f"[{STYLES['narration']}]{argument}[/]")
    elif command == "party":
        _print_party(session)
    elif command == "status":
        where = session.state.location
        console.print(
            f"{where.name if where else 'Nowhere yet'} -- {session.state.world.time}"
        )
    elif command == "dm":
        console.print(dm.provider.health())
        if dm.last.rationale:
            console.print(f"Last turn: {dm.last.rationale}")
        if dm.last.cost_usd:
            console.print(f"Spent so far this turn: ${dm.last.cost_usd:.4f}")
    elif command == "recap":
        console.print(
            Panel(dm.session_recap(), title="Recap", border_style="blue")
        )
    elif command == "scene":
        console.print(f"[{STYLES['narration']}]{dm.open_scene(argument)}[/]")
    elif command == "quests":
        _print_quests(session)
    elif command == "save":
        store.save(session)
        console.print(f"[{STYLES['system']}]Saved at event {session.store.head}.[/]")
    elif command == "checkpoint":
        mark = session.checkpoint(argument)
        console.print(f"[{STYLES['system']}]Checkpoint {mark.event_seq}: {argument}[/]")
    elif command == "rollback":
        try:
            session.rollback(int(argument))
        except ValueError:
            console.print("[red]/rollback needs an event number, e.g. /rollback 42[/red]")
        else:
            store.save(session)
            console.print(f"[{STYLES['system']}]Rewound to event {argument}.[/]")
    else:
        console.print(f"[red]Unknown command /{command}. Try /help.[/red]")
    return False


# --- rendering -------------------------------------------------------------


def _print_party(session: GameSession) -> None:
    if not session.state.party:
        console.print("[dim]No characters yet.[/dim]")
        return
    table = Table(title="Party", header_style="bold")
    for column in ("Name", "Level", "Class", "HP", "AC", "Conditions"):
        table.add_column(column, justify="right" if column in {"Level", "HP", "AC"} else "left")
    for character in session.state.party.values():
        hp = f"{character.hp.current}/{character.hp.maximum}"
        table.add_row(
            character.name,
            str(character.level),
            character.character_class,
            f"[red]{hp}[/red]" if character.hp.is_bloodied else hp,
            str(character.armor_class),
            ", ".join(c.type.value for c in character.conditions) or "-",
        )
    console.print(table)


def _print_sheet(session: GameSession, character_id: str) -> None:
    character = session.state.party[character_id]
    abilities = "  ".join(
        f"{name[:3].upper()} {getattr(character.abilities, name)}"
        for name in ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")
    )
    equipment = ", ".join(i.name for i in character.inventory) or "nothing"
    console.print(
        Panel(
            f"[bold]{character.name}[/bold] -- level {character.level} "
            f"{character.species} {character.character_class}\n"
            f"{abilities}\n"
            f"HP {character.hp.current}/{character.hp.maximum}   AC {character.armor_class}   "
            f"Speed {character.speed}   Proficiency +{character.proficiency_bonus}\n"
            f"Skills: {', '.join(sorted(character.skill_proficiencies)) or 'none'}\n"
            f"Carrying: {equipment}",
            border_style="cyan",
        )
    )


def _print_quests(session: GameSession) -> None:
    quests = session.state.story.quests
    if not quests:
        console.print("[dim]No quests yet.[/dim]")
        return
    for quest in quests.values():
        colour = {
            QuestStatus.ACTIVE: "magenta",
            QuestStatus.COMPLETED: "green",
            QuestStatus.FAILED: "red",
        }.get(quest.status, "dim")
        console.print(f"[{colour}]{quest.title}[/] ({quest.status.value})")
        for objective in quest.objectives:
            console.print(f"    [{'x' if objective.completed else ' '}] {objective.description}")


@app.callback()
def main() -> None:
    """DungeonMaster AI."""


def run() -> None:
    """Entry point that turns save errors into a readable message."""
    try:
        app()
    except SaveError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    run()


__all__ = ["app", "run"]
