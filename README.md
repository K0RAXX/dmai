# DungeonMaster AI

An adaptive tabletop RPG engine: an AI Dungeon Master that can run a whole
fantasy campaign for one player or a table of them.

The important architectural claim is that **the engine is the product**. The
CLI, the desktop window, the OpenClaw agent and the offline SDK are all clients
of one `GameSession`. Delete any of them and the game loses nothing.

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
├── desktop/         Layer C -- the window
│   ├── bridge.py    The window's only door into the game
│   ├── app.py       pywebview host
│   └── web/         The medieval table: HTML, CSS, JS
└── integrations/    OpenClaw adapter -- headless, multi-table

sdk/                 dmai-sdk -- the embeddable, zero-network engine
standalone/          The browser build -- the same game, no Python at runtime
```

`standalone/` is a self-contained port: `src/` holds the engine in plain JS,
`src/ui/` the table, and `index.html` opens it straight from disk. It has no
filesystem, so the rules ship inside the bundle -- `embed_rules.py` regenerates
`src/10-rules-pack.js` from `rules_packs/`, and that file is never hand-edited.

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
python -m pytest tests/test_desktop.py -q   # the window, headless

cd sdk && python -m pytest           # the SDK has its own suite
```

Two tests are load-bearing, and both are about boundaries rather than features:

- `tests/test_architecture.py` fails the build if anything in `dmai/engine`
  imports the AI layer, the API, persistence, a UI, or any networking module.
  That constraint is what keeps the engine testable without a language model and
  swappable between clients.
- `sdk/tests/test_offline.py` plays a whole campaign with `socket.socket`
  replaced by a trap, so the SDK's offline claim is checked rather than asserted.

`node --check dmai/desktop/web/js/*.js` covers the window's JavaScript.

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

## The desktop window

A native window that plays the same campaigns as the CLI, themed as a table in a
scriptorium: parchment panels pinned to dark leather, a wax seal on every
chronicle, gold rules, and a drop cap on the DM's prose.

```bash
pip install -e ".[desktop]"
dmai-desktop                     # or: python -m dmai.desktop
dmai-desktop --root D:/campaigns --provider claude --model claude-opus-5
```

It is a *peer* of the CLI, not a wrapper around it — both drive one
`GameSession`, and a campaign started in one opens in the other. There is no
server: no port is opened and no HTTP is spoken. The page and the engine share a
process and talk through a function call, which is why the window works with the
network unplugged (offline DM) as well as with a model behind it.

Three columns: the party and its quests on the left, the chronicle in the middle,
initiative and dice on the right. Every panel is drawn from one `view()` call —
itself a fold of the event log — so the sheets, the turn order and the transcript
cannot drift out of agreement. Slash commands (`/roll`, `/narrate`, `/scene`,
`/recap`, `/mark`, `/marks`, `/help`) mirror the CLI's vocabulary.

`dmai/desktop/bridge.py` is the whole interface, and it imports no GUI toolkit —
which is why `tests/test_desktop.py` exercises everything the window can do
headlessly, without opening one.

To ship it as a single application:

```bash
pip install -e ".[desktop,dev]"
pyinstaller dmai-desktop.spec        # -> dist/DungeonMaster/DungeonMaster.exe
```

The spec bundles the page and the rules packs as data, and names `anthropic` as
a hidden import — `ClaudeProvider` imports the SDK lazily so that `dmai.ai` loads
without it, and that lazy import is invisible to PyInstaller's analysis, which
would otherwise ship a binary whose Claude DM fails on first use.

## The offline SDK

`sdk/` is a separately packaged `dmai-sdk`: the engine as an embeddable library,
with **no network, no API key and no model**.

```python
from dmai_sdk import Table

table = Table.new("Ashes of Emberfall", seed=1234)
table.add_hero("Vale", cls="fighter", level=2)

turn = table.act("I search the room")
print(turn.narration, turn.rolls)

table.save("emberfall.dmai")
```

There is no provider argument to point at a model, and the guarantee is tested
as behaviour rather than asserted: `sdk/tests/test_offline.py` replaces
`socket.socket` with a trap and plays a whole campaign through it, so one
outbound connection fails the build.

A seeded table is deterministic, which makes it usable as a test fixture. Saves
use the same bundle format the CLI imports, so `table.save()` opens with
`dmai import` and vice versa. See `sdk/README.md`.

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

### `dmai bot` — the line protocol

For transports that would rather not import Python, `dmai bot` speaks one JSON
object per line on stdin and one per line on stdout:

```bash
dmai bot --describe          # the operations this bot accepts
echo '{"op":"message","external_id":"telegram:44","text":"I look around"}' | dmai bot
dmai bot --once '{"op":"list_campaigns"}'
```

No port, no server, no import — a pipe is enough to run whole campaigns. A
malformed message comes back as `{"ok": false, "error": ...}` and the bot keeps
serving; only stdin closing ends it.

Operations are dispatched against an **allowlist** (`CALLABLE_OPERATIONS`), not
by attribute lookup. The caller is a chat transport carrying text from
strangers, so `{"op": "_session"}` is refused like any other unknown name.

### Sharing one key with an OpenClaw bot

Credentials are read from the environment by the Anthropic SDK, so pointing both
at one `ANTHROPIC_API_KEY` is the whole of it — no code, no config bridge. Note
that OpenClaw's `claude-cli` runtime authenticates with Claude Code's OAuth
store, which is *not* the `ant auth login` profile the SDK reads; sharing has to
go through an API key or an explicit setting.

## Status

Built and tested: dice, rules adapter, character creation and checks, combat
with tactics, inventory, encounter budgets, quests and consequences, the world
atlas and simulator, the session facade, save/load/export, the CLI, the DM AI
layer, the OpenClaw adapter, the desktop window, and the offline SDK.

Next: the HTTP API (`dmai/api`) — a client of the same `GameSession`.

## Licence

The engine and everything around it is MIT licensed. See `LICENSE`.

Rules content in `rules_packs/srd51/` is the one exception: it includes material
from the System Reference Document 5.1 by Wizards of the Coast LLC, used under
CC-BY-4.0. The generated `standalone/src/10-rules-pack.js` embeds that same data
and inherits those terms. See `NOTICE` and `rules_packs/srd51/LICENSE`.
