# adklaw

A general-purpose AI assistant inspired by [OpenClaw](https://github.com/openclaw/openclaw),
built on the Google [Agent Development Kit (ADK)](https://adk.dev/).

The agent operates against a **workspace directory** (your shared
collaboration space with the agent), is customized via markdown files
the workspace, supports **agent skills** (folders with `SKILL.md`), and
exposes itself through pluggable **channels** (CLI, web playground,
Discord, future Slack/Telegram). The same agent code can also be
deployed to **Gemini Enterprise** via Agent Runtime.

## Project structure

```
adklaw/
├── app/
│   ├── agent.py              # root agent (model, instruction, tools, skills toolset)
│   ├── tools.py              # filesystem + shell + web_fetch tools
│   ├── skills.py             # LiveSkillToolset (live-reloading skills)
│   ├── workspace.py          # workspace path + AGENTS.md loading
│   ├── state.py              # agent state dir resolution (.adklaw/)
│   ├── channels/
│   │   ├── base.py           # ChannelBase: shared Runner + SessionService
│   │   └── discord.py        # Discord bot
│   └── fast_api_app.py       # HTTP/SSE app (used by playground + Agent Runtime)
├── skills/                   # default skills shipped with the project
├── templates/                # seed files for new workspaces
│   └── AGENTS.md             # default persona — copied into workspace/ by init script
├── scripts/
│   └── init-workspace.sh     # seed a workspace from templates/
├── workspace/                # human-agent collaboration space (gitignored, seeded by script)
├── .adklaw/                  # agent state dir (sessions DB, etc. — gitignored)
└── pyproject.toml
```

Two directories are easy to confuse — keep them straight:

- **`workspace/`** — human/agent collaboration surface. Notes, files
  the agent works on, your `AGENTS.md`, custom skills you don't want
  to commit. **Fully gitignored.** Seed it once with
  `bash scripts/init-workspace.sh`; after that it's yours to edit.
- **`.adklaw/`** — agent state. Sessions DB, channel state, future
  caches. Fully gitignored. Override location with `ADKLAW_STATE_DIR`.

## Requirements

- **uv** — Python package manager. [Install](https://docs.astral.sh/uv/getting-started/installation/).
- **agents-cli** — `uv tool install google-agents-cli`.
- **Google Cloud SDK** — for ADC auth. [Install](https://cloud.google.com/sdk/docs/install).

Set `GOOGLE_CLOUD_PROJECT` in `.env` (and optionally `GOOGLE_CLOUD_LOCATION`,
which defaults to `global`).

## Quick start

```bash
agents-cli install                     # uv sync
agents-cli login -i                    # one-time GCP auth
bash scripts/init-workspace.sh         # seed ./workspace from templates/AGENTS.md
agents-cli run "list every skill you have"
agents-cli playground                  # interactive web UI
```

`scripts/init-workspace.sh` copies `templates/AGENTS.md` into
`./workspace/AGENTS.md` and creates `./workspace/skills/`. It refuses
to overwrite an existing `AGENTS.md`, so re-running is safe. Pass an
absolute path (`bash scripts/init-workspace.sh ~/my-project`) to seed
an out-of-tree workspace.

If you skip this step the agent still runs, just without a custom
persona — it logs a one-shot info-level hint at startup pointing at
the script.

## Customizing the agent

The agent's behavior is driven by `workspace/AGENTS.md`. Edit it freely
— changes take effect on the **next message**, no restart needed. Drop
in additional `*.md` files (`STYLE.md`, `PROFILE.md`, `CONVENTIONS.md`,
…) at the workspace root and they'll be loaded as supplementary
context.

The template at `templates/AGENTS.md` is the canonical seed. Edit it
in the repo if you want to change what new workspaces start with;
existing workspaces aren't touched.

### Web search

The `web_search` tool calls Gemini 2.5 Flash-Lite with Google Search
grounding ([docs](https://ai.google.dev/gemini-api/docs/grounding))
and returns a synthesized answer plus the list of cited source URLs.
Two env knobs:

- `ADKLAW_WEB_SEARCH_MODEL` — model id; default `gemini-2.5-flash-lite`.
- `ADKLAW_WEB_SEARCH_LATLNG` — `"lat,lng"` to bias results
  geographically; default Taipei (`"25.0330,121.5654"`). Set to `""`
  to disable bias. (Country / language codes aren't exposed by the
  grounding API today; lat/lng is the available knob.)

Results are charged to the same Vertex billing as the main agent
model. Flash-Lite is cheap (sub-cent per call) — no separate API key
or service account needed.

To point the agent at a different workspace:

```bash
ADKLAW_WORKSPACE=~/my-project agents-cli run "..."
```

## Skills

Skills are folders with a `SKILL.md` (YAML frontmatter for `name` +
`description`, then markdown instructions). The agent automatically
lists them in its system prompt and loads the body when relevant. See
[agentskills.io](https://agentskills.io/specification) for the spec.

- `skills/` at the repo root — shipped with the project, tracked.
- `workspace/skills/` — your private skills, gitignored.

User skills override shipped skills with the same `name`. Edit, add,
or delete folders — picked up on the next message.

## Channels

Channels are adapters that route messages from external transports to
the agent. The shared `app/channels/base.py` wraps an ADK `Runner` and
`SessionService`; concrete channels just bind their SDK to it.

### Discord

Run as its own process:

```bash
uv run python -m app.channels.discord
```

The bot reads `DISCORD_BOT_TOKEN` (and the GCP env vars) from `.env`
automatically — no shell sourcing required. Channels auto-load `.env`
the same way `agents-cli` does. `discord.py` is part of the `channels`
default dependency group, so `agents-cli install` (or any `uv sync`)
keeps it installed.

Setup:

1. Create a Discord application + bot at <https://discord.com/developers>.
2. Enable the **Message Content Intent** in the bot's Privileged
   Gateway Intents.
3. Copy the bot token.
4. Invite the bot to a server with the OAuth2 scopes `bot` and
   `applications.commands`, plus the permissions `Send Messages` and
   `Read Message History`.
5. Add `DISCORD_BOT_TOKEN=...` to `.env` (already gitignored).

Behavior:

- DMs: bot responds (subject to the allowlist below).
- Server channels: bot only responds when **@-mentioned**.
- Each Discord channel/thread/DM keeps its own ADK session
  (conversation continuity per location).
- Sessions persist in `.adklaw/sessions.db` (SQLite) — survives restarts.
- Every message the agent sees is preceded by an `[origin]…[/origin]`
  block carrying the sender's Discord display name + ID and the
  channel/guild label + IDs, so the agent can address you by name and
  reason about where the message came from.

**Restricting who can DM the bot.** Anyone who shares a server with
your bot can DM it by default. To lock the bot to specific Discord
users, set `DISCORD_ALLOWED_USER_IDS=<id1>,<id2>,...` in `.env`. The
first DM from a non-listed user gets a one-shot reply showing their
ID so you can add them; subsequent DMs from the same user are
silently ignored until the bot restarts. Find your own Discord user
ID by enabling Developer Mode in Discord settings → right-click
your profile → "Copy User ID".

By default the allowlist gates **DMs only** — guild mentions always
go through (anyone who can @-mention the bot in a server you've
invited it to is implicitly trusted). To gate guild mentions too,
set `DISCORD_ALLOWLIST_SCOPE=all`. Valid values are `dm` (default)
and `all`.

**Replying to other bots.** By default the bot ignores any message
whose author is itself a bot (webhooks, bridges like PluralKit / IRC
relays, GitHub/news integrations, friendly bots). This blocks
bot-to-bot ping-pong loops. To opt in, set
`DISCORD_REPLY_TO_BOTS=true`. The bot's own messages are always
skipped regardless of this toggle. When this is on, the response is
delivered as a plain `channel.send(...)` rather than a quoted
`message.reply(...)` — that way the response carries no implicit
mention or `MessageReference` pointing back at the other bot, which
would otherwise re-trigger it. Set `DISCORD_QUOTE_BOT_REPLIES=true`
if you specifically want the visual quote (and accept the loop
risk). Pair with `DISCORD_ALLOWED_USER_IDS` +
`DISCORD_ALLOWLIST_SCOPE=all` to restrict which bots can wake the
agent.

**Conversation context backfill in channels.** When the bot is
mentioned in a guild channel, it backfills the recent message history
so the agent can follow the conversation flow rather than seeing only
the triggering message. Each channel keeps an in-memory rolling
buffer; the first mention per channel since process start triggers a
one-shot `channel.history()` fetch via the Discord API to seed the
buffer, and every subsequent mention reads context straight from
memory. Configure with `DISCORD_CONTEXT_HISTORY_LINES` (default `20`,
set to `0` to disable). DMs are not backfilled — the ADK session
already has full DM history. See [`docs/channels-context.md`](docs/channels-context.md)
for the design rationale.

### Future channels

Slack, Telegram, etc. follow the same shape as `discord.py`: subclass
`ChannelBase`, map your transport's user/conversation IDs into ADK
`(user_id, session_id)`, post the response back. Each new channel is
a small file plus an optional dep entry in `pyproject.toml`.

## Deploying to Gemini Enterprise

Gemini Enterprise (formerly Vertex AI Agent Platform) registers ADK
agents as managed Agent Runtime resources, so they show up in the GE
agent listing. Workflow:

```bash
# 1. Add the Agent Runtime deployment target (one-time).
agents-cli scaffold enhance . --deployment-target agent_runtime

# 2. Deploy. Agent Runtime hosts the FastAPI app from `app/fast_api_app.py`.
agents-cli deploy

# 3. Register the deployed agent with Gemini Enterprise.
agents-cli publish gemini-enterprise --registration-type adk
```

The plain `adk` registration path (above) works because Agent Runtime
translates SSE↔A2A internally — no `to_a2a()` wrapping needed. If you
deploy to Cloud Run instead, you'd switch to `--registration-type a2a`
and add a `to_a2a()` wrapper to expose an A2A endpoint.

GE manages its own sessions; conversations there are independent from
your local Discord/CLI sessions. Cross-channel session sharing would
require deploying on Cloud Run with a shared `Cloud SQL` session
backend — not in scope yet.

## Commands

| Command | Description |
|---|---|
| `agents-cli install` | Install dependencies via uv |
| `agents-cli run "prompt"` | One-shot CLI invocation |
| `agents-cli playground` | Web UI for interactive chat + traces |
| `agents-cli lint` | ruff + format + codespell + ty |
| `uv run python -m app.channels.discord` | Run the Discord bot |
| `agents-cli deploy` | Deploy to the configured target (after `scaffold enhance`) |
| `agents-cli publish gemini-enterprise` | Register with Gemini Enterprise |
| `uv run pytest tests/unit tests/integration` | Run tests |

## Development

Edit `app/agent.py` (model, base instruction) or
`workspace/AGENTS.md` (persona, behavior). Test with
`agents-cli playground` or `agents-cli run`. The agent re-reads the
workspace and skills directories on every turn, so most changes are
live-reload.

## Observability

Built-in telemetry exports to Cloud Trace, BigQuery, and Cloud Logging
(via the `app/app_utils/telemetry.py` setup).
