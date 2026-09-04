# DungeonMaster AI

An adaptive tabletop RPG engine: an AI Dungeon Master that can run a whole
fantasy campaign for one player or a table of them.

The important architectural claim is that **the engine is the product**. The
CLI, the OpenClaw agent, and the planned desktop window and HTTP API are all
clients of one `GameSession`. Delete any of them and the game loses nothing.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev]"

dmai new "Ashes of Emberfall" -p "A caravan vanished on the Cinder Road." --seed 1234
dmai character add "Ashes of Emberfall" Vale --class fighter --level 2 --player James
dmai play "Ashes of Emberfall" --as James
```

Anything you type that is not a slash command goes through the DM's reasoning
loop. `/help` lists the rest: `/roll`, `/narrate`, `/dm`, `/recap`, `/scene`,
`/party`, `/quests`, `/checkpoint`, `/rollback`, `/save`, `/quit`. Play
autosaves, so closing the terminal loses nothing.

Campaigns live in `~/.dmai/campaigns` by default; set `DMAI_HOME` to move the
whole data directory (a USB stick, a synced folder), or pass `--root`.

## Layout

```
dmai/
├── engine/          Layer A -- the game. Pure: no network, no model, no I/O.
│   ├── models/      Domain schemas every layer speaks
│   ├── rules/       Rules adapters + the pack registry (SRD 5.1 by default)
│   ├── dice.py      Expression parser and roller
│   ├── state.py     The event log, the reducers, the Journal
│   ├── characters/  Character creation, skill checks, monsters
│   ├── combat/      Initiative, attacks, damage, death saves, monster tactics
│   ├── inventory/   Items, equipment, currency
│   ├── encounters/  Difficulty budgets and encounter composition
│   ├── quests/      Quests, objectives, the story ledger, consequences
│   ├── world/       The atlas (places, NPCs, factions) and the simulator
│   └── session.py   GameSession -- the engine's front door
│
├── ai/              Layer B -- the DM agent
│   ├── providers/   Provider abstraction: offline DM + Claude
│   ├── dm_agent/    The reasoning loop and the DM's prompts
│   ├── narrative/   Tone, voice and behaviour controls
│   └── memory/      Memory extraction and recall
├── persistence/     Saving and loading; stores the log, never the state
├── cli/             Layer C -- the terminal client
├── api/             Layer C -- HTTP/WebSocket (not yet built)
├── desktop/         Layer C -- the window (not yet built)
└── integrations/    OpenClaw adapter -- headless, multi-table
```

`rules_packs/srd51/` holds the rules as **data** (CC-BY-4.0 SRD 5.1). Swapping
or extending a ruleset means editing JSON, not Python.

## How state works

Nothing above the engine edits game state directly. Every change is an event
appended to a log, and the state is the fold of that log:

- **Replay** — `rebuild(campaign, events)` reproduces a campaign exactly.
- **Rollback** — a checkpoint is a sequence number; rewinding is truncating.
- **Saves** — only the log is written to disk, so a save can never disagree
  with its own history. A crash mid-write costs the last event, not the game.
- **Seats** — private information is filtered in one place (`visible_to`), so a
  player can be shown the log without being shown another player's whispers.

Mutating events carry *absolute* results (`hp_current: 4`, never `damage: 3`),
which is what makes replaying one twice safe.

## Development

```bash
python -m pytest                     # the whole suite
python -m pytest tests/test_session.py -q
```

`tests/test_architecture.py` is the load-bearing one: it fails the build if
anything in `dmai/engine` imports the AI layer, the API, persistence, a UI, or
any networking module. That constraint is what keeps the engine testable
without a language model and swappable between clients.

## The DM AI

The DM is asked two questions per turn, and neither is "what happened":

1. **Interpret** — what is this player trying to do, and what must be rolled?
   Answered as a schema-validated `ActionInterpretation`.
2. **Narrate** — here are the facts the engine resolved; describe them.

Between the two, the engine resolves every check and every attack. The facts
handed to the narrator are summaries of events the engine actually appended, so
the DM narrates the log rather than authoring it. Every id the model produces is
checked against real state first, so a hallucinated target is dropped rather
than attacked.

A fantasy table meets policy declines more than most software does — violence is
the subject matter — so narration opts into server-side refusal fallbacks
(`fallbacks: "default"`, routed by refusal category). A declined scene is re-run
on another model inside the same call, and the turn is priced at whichever model
actually served it. Interpretation stays on the plain endpoint: a refused
interpretation already degrades to the local classifier.

There is always a floor beneath that. `OfflineProvider` interprets by keyword and
narrates by restating facts — no network, no key, no model. If a provider errors,
returns nothing, or the whole fallback chain refuses, the DM degrades to it
mid-turn and play continues. `/dm` at the table shows which DM answered, and a
turn finished by a fallback model says so.

```bash
dmai new "Ashes of Emberfall" --provider claude --model claude-opus-5
dmai play "Ashes of Emberfall" --as James --resume
```

Credentials come from the environment (`ANTHROPIC_API_KEY`, or `ant auth
login`). A campaign save stores the provider *id* and never a key.

## OpenClaw

`dmai.integrations.openclaw.OpenClawAdapter` is a headless DM: a real
programmatic interface, not GUI automation. It exposes the operations the spec
names — `create_campaign`, `submit_player_action`, `start_combat`,
`get_game_state`, `send_private_message`, and the rest — and returns plain JSON,
so any chat transport can drive it.

```python
bot = OpenClawAdapter(CampaignStore("~/.dmai/campaigns"))
bot.join(campaign_id, "James", external_id="discord:111")
reply = bot.handle_message("discord:111", "I search the common room")
```

One adapter serves any number of tables. Sessions are cached per campaign id and
a chat identity routes to the one campaign it is seated at; a player seated at
two must name one rather than have the bot guess.

## Status

Built and tested: dice, rules adapter, character creation and checks, combat
with tactics, inventory, encounter budgets, quests and consequences, the world
atlas and simulator, the session facade, save/load/export, the CLI, the DM AI
layer, and the OpenClaw adapter.

Next: the HTTP API (`dmai/api`) and the desktop window (`dmai/desktop`) — each a
client of the same `GameSession`.

## Licence

Rules content in `rules_packs/srd51/` includes material from the System
Reference Document 5.1 by Wizards of the Coast LLC, used under CC-BY-4.0. See
`rules_packs/srd51/LICENSE`.
