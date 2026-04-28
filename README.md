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
├── workspace/                # human-agent collaboration space (mostly gitignored)
│   └── AGENTS.md             # agent persona / instructions (tracked)
├── .adklaw/                  # agent state dir (sessions DB, etc. — gitignored)
└── pyproject.toml
```

Two directories are easy to confuse — keep them straight:

- **`workspace/`** — human/agent collaboration surface. Notes, files
  the agent works on, custom skills you don't want to commit. Mostly
  gitignored; `AGENTS.md` is the only tracked file.
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
agents-cli run "list every skill you have"
agents-cli playground                  # interactive web UI
```

## Customizing the agent

The agent's behavior is driven by `workspace/AGENTS.md`. Edit it freely
— changes take effect on the **next message**, no restart needed. Drop
in additional `*.md` files (`STYLE.md`, `PROFILE.md`, `CONVENTIONS.md`,
…) at the workspace root and they'll be loaded as supplementary
context.

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
uv sync --extra discord
DISCORD_BOT_TOKEN=<your token> uv run python -m app.channels.discord
```

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

- DMs: bot always responds.
- Server channels: bot only responds when **@-mentioned**.
- Each Discord channel/thread/DM keeps its own ADK session
  (conversation continuity per location).
- Sessions persist in `.adklaw/sessions.db` (SQLite) — survives restarts.

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
