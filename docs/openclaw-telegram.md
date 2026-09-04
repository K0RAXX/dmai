# Running the DM as a Telegram bot through OpenClaw

The engine is already headless (`dmai bot`), so the integration is three pieces
of wiring: a Telegram bot token, an OpenClaw agent bound to that account, and a
skill that tells the agent to call `dmai` and relay what it says.

```
Telegram ──▶ OpenClaw channel (account: dungeon-master)
                └─▶ agent: dungeon-master  ──(skill: dungeon-master)──▶ dmai bot
                                                                         └─▶ GameSession
```

## Why the agent is a relay, not the DM

OpenClaw agents are LLMs. `dmai` also has an LLM in it. Left alone, the OpenClaw
agent would paraphrase the DM's narration and quietly invent details — two
narrators, one story, no continuity.

The skill therefore gives the agent one job: pass the message to `dmai`, print
the answer verbatim. The DM's voice, the dice, and every rule stay inside the
engine, where they are tested. If you ever see the bot narrating something the
`dmai` log does not contain, the skill's "relay verbatim" rule is what slipped.

## 1. Create the bot

In Telegram, message **@BotFather** → `/newbot` → follow the prompts. Save the
token (looks like `123456789:AA...`, ~46 characters).

While you're there, decide how it should behave in groups:

- `/setprivacy` → **Disable** lets the bot see every group message. A DM bot
  wants this; otherwise it only sees messages that @mention it.
- After changing privacy, remove and re-add the bot to each group so Telegram
  applies it.

## 2. Store the token in a file, not in the config

Match the pattern your other bot already uses (`tokenFile` beats `botToken`):

```powershell
$t = Read-Host "Telegram bot token" -AsSecureString | ConvertFrom-SecureString -AsPlainText
Set-Content -Path "$HOME\.openclaw\credentials\telegram-dungeon-master-token.txt" -Value $t -NoNewline
Remove-Variable t
```

## 3. Install the skill

The skill is version-controlled in this repo. Copy it into the DM agent's
workspace:

```powershell
$dest = "$HOME\.openclaw\workspace-dm\skills\dungeon-master"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item "D:\D&D AI DEV\integrations\openclaw-skill\dungeon-master\SKILL.md" $dest
```

A separate workspace keeps the DM's instructions away from your main agent, and
your main agent's skills away from the game.

`dmai` must be on the PATH the OpenClaw gateway runs with — check with
`dmai bot --describe`. **On this machine it is not**, so the installed copy of
the skill was rewritten to call
`"D:\D&D AI DEV\.venv\Scripts\dmai.exe"` directly. The repo version keeps
the plain `dmai`; if you ever put it on PATH, re-copy the repo version.

Verify the skill loaded for the right agent, and only that agent:

```bash
openclaw skills list --agent dungeon-master   # 🎲 dungeon-master, ✓ ready
openclaw skills list --agent main             # should NOT list it
```

## 4. Merge this into `~/.openclaw/openclaw.json`

> **Already applied on this machine** (backup: `~/.openclaw/openclaw.json.pre-dm-*`).
> The Telegram account is present but `enabled: false` until its token file
> exists — see step 5.

Back it up first (`Copy-Item ~\.openclaw\openclaw.json ~\.openclaw\openclaw.json.pre-dm`).
Merge — do not replace — these sections.

**Two traps worth knowing before you hand-edit this file:**

*The default agent moves.* Routing picks the default agent as
`agents.list[].default`, **else the first list entry**. If `agents.list` did not
exist and you add one containing only the DM, the DM silently becomes the
default for every message that matches no binding — including your existing
bots. So `main` goes in the list first, explicitly `default: true`, and the
existing accounts get explicit bindings rather than relying on a fallback.

*The workspace nests.* `agents.defaults.workspace` is set, so an agent with no
stated workspace lands at `<defaults.workspace>/<agentId>` — inside the main
workspace, sharing its skills. State `workspace` explicitly.

```json5
{
  channels: {
    telegram: {
      accounts: {
        "dungeon-master": {
          name: "Dungeon Master",
          enabled: true,
          tokenFile: "C:\\Users\\K0RAXX\\.openclaw\\credentials\\telegram-dungeon-master-token.txt",
          groups: { "*": { requireMention: false } },
        },
      },
    },
  },

  agents: {
    list: [
      {
        id: "dungeon-master",
        name: "Dungeon Master",
        workspace: "C:\\Users\\K0RAXX\\.openclaw\\workspace-dm",
      },
    ],
  },

  bindings: [
    { match: { channel: "telegram", accountId: "dungeon-master" }, agentId: "dungeon-master" },
  ],
}
```

`requireMention: false` matters: in a game group, "I search the room" is a
player action, not a mention of the bot. Your existing `default` account keeps
`requireMention: true` — this only loosens the new one.

The binding is what stops game messages reaching your main agent, and vice
versa. Without it, every routing rule falls through to the default agent.

## 5. Start it

Once the token file from step 2 exists, enable the account:

```powershell
$c = Get-Content "$HOME\.openclaw\openclaw.json" -Raw | ConvertFrom-Json
$c.channels.telegram.accounts.'dungeon-master'.enabled = $true
$c | ConvertTo-Json -Depth 100 | Set-Content "$HOME\.openclaw\openclaw.json"
```

It ships disabled because the gateway reloads this file, and an enabled account
whose token file is missing is a startup failure.

```bash
openclaw gateway
openclaw pairing list telegram
openclaw pairing approve telegram <CODE>
```

Then in Telegram: DM the bot, or add it to a group and send

```
/newgame Ashes of Emberfall
```

## How a room becomes a table

`dmai` binds a **chat room** to a campaign, not a person. That is what makes a
group work: everyone in `telegram:-100123` plays one campaign, and a player in
two games is routed by the room they spoke in rather than by an ambiguous
identity lookup.

- `create_campaign` with a `channel_id` starts a table and binds the room in one
  call, so a chat command cannot half-succeed into a campaign nobody can reach.
- A player who is not yet seated is seated on their first message in a bound
  room, provided the skill passes `display_name`. Joining a game should not need
  a separate command.
- An **unbound** room never seats anyone. A stray message cannot conjure a seat
  at someone else's table.

Bindings live in `channels.json` beside the campaigns, so moving the store moves
its tables with it, and they survive a gateway restart.

## Which DM answers

The campaign stores `settings.ai_provider`. Create the table with
`"settings": {"ai_provider": "claude"}` for model narration, or leave it
`offline` for a terse DM that needs no key and no network.

Credentials come from the environment, so `ANTHROPIC_API_KEY` set for the
gateway process is all the sharing needed between this and any other OpenClaw
bot. Note that OpenClaw's `claude-cli` runtime authenticates against Claude
Code's OAuth store, which is *not* the profile the Anthropic SDK reads — sharing
has to go through an API key.

## Checking it without Telegram

Everything above is a transport. The game itself is testable from a terminal:

```bash
dmai bot --once '{"op":"create_campaign","name":"Test Table","channel_id":"telegram:-100123"}'
dmai bot --once '{"op":"message","external_id":"telegram:44","channel_id":"telegram:-100123","display_name":"James","text":"I search the room"}'
```

If those work and Telegram does not, the problem is in OpenClaw's routing —
check `openclaw logs --follow` and confirm the binding matched the right agent.
