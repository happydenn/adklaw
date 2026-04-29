# Architecture overview

Navigable index of `docs/`. Start here if you're new to the
codebase or coming back after time away. Each link below is
the canonical doc for that concern.

## State and persistence

The agent has five distinct kinds of state. Each is a stable
interface with pluggable backends — the agent code does not
change when the deployment shape changes; only the wired-up
backend does.

| Tier | Concept | Interface | Backends | Doc |
|---|---|---|---|---|
| 1 | Sessions (working memory) | `BaseSessionService` (ADK) | InMemory / Sqlite / Database / VertexAi | [session-memory.md](./session-memory.md) |
| 2 | Memory (episodic recall) | `BaseMemoryService` (ADK) | InMemory / VertexAiMemoryBank | *deferred indefinitely; see decisions-and-deferrals.md* |
| 3 | Knowledge (semantic facts) | `BaseKnowledgeService` (new) | InMemory / Local / Firestore | [knowledge.md](./knowledge.md) |
| 4 | Instructions (system prompt) | three-layer composition | layer 1 hardcoded; layer 2 file-driven; layer 3 channel-driven | [instruction-layering.md](./instruction-layering.md) |
| — | Artifacts (binary blobs) | `BaseArtifactService` (ADK) | InMemory / File / GCS | — |

The workspace itself (a real local filesystem at
`ADKLAW_WORKSPACE`, present on every deployment regardless of
persistence) is *not* an abstraction — it's a concrete
directory the agent reads, writes, and runs shell commands
in. See `docs/decisions-and-deferrals.md` for why.

## Channels and transports

The agent is channel-agnostic. Each channel adapter does its
own transport translation and constructs synthetic
`user_id` / `session_id` for its scoping rules.

| Channel | Adapter | Doc |
|---|---|---|
| CLI / playground | bare `app = build_app()` | — |
| Discord | `app/channels/discord.py` | [channels-context.md](./channels-context.md), [channels-gateway.md](./channels-gateway.md) |
| gws (Gemini Enterprise) | bare `app = build_app()` (no adapter needed) | [deployments.md](./deployments.md) |

> **Naming:** "gws" = Google Workspace surfaces accessed via
> Gemini Enterprise. Disambiguates from our own
> "workspace" scratch-space concept.

## Decisions and deferrals

Architectural calls made (and explicitly punted) live in
[decisions-and-deferrals.md](./decisions-and-deferrals.md).
That file is the durable record — read it before opening any
"why is X built this way?" conversation.

## How customization works

The agent is meant to be customized without code changes.
Three lever points, in increasing scope:

1. **`<workspace>/AGENTS.md`** + other top-level `*.md` —
   personalization, voice, project-specific rules. Live-edit
   in dev; baked into the deployed image for prod.
2. **`<workspace>/skills/`** — agent skills (folders with
   `SKILL.md`). Same lifecycle as AGENTS.md.
3. **`<workspace>/.knowledge/`** — durable structured facts
   the agent reads/writes through tools. Hidden directory —
   the agent owns this.

See [customizing-the-agent.md](./customizing-the-agent.md)
for the user-facing how-to and `templates/AGENTS.md` for the
starter scaffold a new workspace gets seeded with.
