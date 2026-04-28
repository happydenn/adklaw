# Channels: current model vs a future gateway

## Today

Each channel is a self-contained process. `python -m app.channels.discord`
imports `app.agent.app`, builds its own `Runner`, opens its own
`SessionService` (SQLite under `.adklaw/`), and runs forever. The CLI
(`agents-cli run`) and playground (`agents-cli playground`) are similar:
each starts a process, owns its own runner+sessions, and exits when
stopped.

There is no central daemon. Channels do not coordinate. They share
nothing at runtime except files on disk (workspace, skills,
`.adklaw/sessions.db`).

This is intentional for the current scope:

- One user.
- One agent definition (`root_agent`).
- Three channels (CLI, playground, Discord) plus future GE deploy.
- Channel lifecycles are very different (CLI is ephemeral, playground is
  dev-only, Discord is long-running, GE is hosted by Google).

The seam where a future gateway would slot in is `app/channels/base.py`:
`ChannelBase` already factors `Runner` + `SessionService` + identity
mapping out of each transport.

## OpenClaw's gateway model (for reference)

OpenClaw runs a single long-lived daemon (the **Gateway**) — installed
as a launchd/systemd service — that all channels connect to. The
gateway owns:

- Sessions (one DB, shared across channels).
- Tool configuration and sandboxing (`main` session unsandboxed; group
  sessions in Docker by default).
- Multi-agent routing — channels/accounts/peers route to *different*
  agents based on a config table.
- Identity policy and DM pairing/allowlists.
- A control plane (pause an agent, hot-swap a model, list active
  sessions, view traces).
- Optional surfaces: voice wake/talk, Canvas/A2UI, companion apps.

Channels are thin — they just forward inbound transport messages to the
gateway and post the gateway's responses back.

## Tradeoffs

**Gateway wins when** any of:

1. You add a **second agent** and want different channels routed to
   different personas. (Routing in one place beats N processes each
   knowing about every agent.)
2. You want **shared session state across channels** ("started a thread
   in Discord, finished it in CLI, agent has context"). One process,
   one DB makes this trivial.
3. **Many channels.** Supervising 5+ separate processes (and their
   crash recovery) starts to dominate ops effort.
4. You want **central control**: pause/resume, model swap without
   redeploy, view across-channel activity, enforce sandboxing per
   session.
5. You want OpenClaw-style features: sandboxed group sessions, voice
   nodes, Canvas, pairing flows.

**Per-process wins when** any of:

1. **Single user, single agent, few channels.** Today's adklaw.
2. You want **independent lifecycles** — Discord can be down without
   affecting playground; CLI doesn't need anything else running.
3. **Hosted channels** (Gemini Enterprise, Agent Runtime) call *us*.
   They don't connect to our gateway, so the gateway model doesn't
   help for them — you end up running two architectures.
4. **No control plane needed yet.**

## Middle step: a multi-channel runner (no gateway)

If supervising several channel processes becomes annoying before the
real gateway wins kick in, there is a small intermediate step:

```
python -m app.channels        # runs every enabled channel in one event loop
```

Implementation sketch:

- `app/channels/__main__.py` reads which channels are enabled (env vars
  or a tiny config) and instantiates each.
- Single asyncio loop runs each channel's transport client concurrently
  via `asyncio.gather()`.
- All channels share **one** `Runner` and **one** `SessionService`, so
  cross-channel session sharing is opt-in by passing the same
  `session_id` from each channel's identity-mapper.
- No broker, no routing config, no control plane — just one process
  hosting N transports. Adding the gateway later means inserting a
  router between this runner and the channels; it does not require
  rebuilding the channels themselves.

This is the cheapest way to consolidate ops without committing to the
gateway architecture.

## Recommendation

Stay with per-process today. Adopt the multi-channel runner if/when
ops effort climbs. Adopt a real gateway when multi-agent routing, a
control plane, or sandboxed group sessions become real requirements
— not before.
