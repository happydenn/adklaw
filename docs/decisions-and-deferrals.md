# Decisions and deferrals

Living register of architectural decisions, items explicitly
deferred, and paths considered and rejected. Each PR that
makes a non-obvious call should add to **Decided**; each PR
that surfaces a question worth tracking should add to
**Deferred**; each PR that turns down a viable-looking
alternative should add to **Rejected**.

The reasoning behind a non-obvious decision is the most
expensive thing to recover and the easiest thing to lose.
Plans expire. Conversations get compacted. This file
persists.

---

## Decided

### Workspace is universal across all deployments

Cloud Run's container filesystem is fully writable
in-memory; Agent Runtime is a managed container with the
same property. Workspace tools (`read_file`, `edit_file`,
`run_shell`, etc.) and skills are registered on every
deployment. Persistence varies (local volume vs ephemeral
RAM-backed FS); capability does not.

*Why:* the agent's customization story (AGENTS.md, skills,
knowledge, shell scripts) depends on the workspace existing
everywhere. Earlier draft gated workspace tools on a
"workspace backend" being configured — wrong framing.

### No `WorkspaceBackend` abstraction

Workspace I/O stays as `pathlib.Path`-based tools, not
abstracted behind a `BaseSessionService`-style interface.

*Why:* the workspace is always a real local filesystem (only
question is whether it persists across restarts), and
`run_shell` requires real paths a subprocess can `cd` into.
An abstraction would buy nothing and break shell.

### Knowledge (Tier 3) lives in `<workspace>/.knowledge/` (hidden)

The store is logically a database; entries should
round-trip through `read_knowledge` / `write_knowledge`
tools so the in-memory index stays consistent with disk.

*Why:* hidden directory signals "managed by the agent;
touch carefully," consistent with `.git/`, `.cache/`,
`.venv/`. Humans can still edit (`ls -a`, mtime check picks
up out-of-band edits), but the friction is intentional.

`LocalKnowledgeBackend` lands in PR 3 along with
`BaseKnowledgeService`, the four agent-facing tools, and
the prompt-index integration. File format: markdown with
YAML frontmatter (`summary`, `created_at`, `updated_at`).
Slug regex: `[a-z0-9][a-z0-9_-]*`.

`FirestoreKnowledgeBackend` lands in PR 4. Same data model,
stored as documents (doc id = slug) in a Firestore
collection. Selected by env vars:

- `ADKLAW_KNOWLEDGE_BACKEND=firestore`
- `ADKLAW_KNOWLEDGE_FIRESTORE_COLLECTION` (default
  `adklaw_knowledge`)
- `ADKLAW_KNOWLEDGE_FIRESTORE_PROJECT` (falls back to
  `GOOGLE_CLOUD_PROJECT`)

`list_knowledge` projects `summary` + `created_at`
server-side so the prompt-index path doesn't fetch full
content. The gRPC client is constructed lazily on first
method call so importing the module doesn't require GCP
credentials. Tests run against a hand-rolled in-memory fake
mirroring the AsyncClient surface; production validation
happens at deploy time in PR 5.

### No `GwsChannelAdapter`

The bare-core `app = build_app()` already exposed at
`app/agent.py:192` is what registers with Gemini Enterprise.
No new adapter module needed for the gws channel.

*Why:* Agent Engine invokes `App.root_agent` through ADK's
standard Runner. The gws surface has no envelope semantics
like Discord's @-mention/DM/guild distinction, no inbound
attachment translation, no outbound `discord.File` packing.
`ChannelBase` exists *because* Discord forces those
inventions; the gws channel forces nothing.

### Channel-agnostic agent

The agent has zero channel awareness. It runs against
`(user_id, session_id, origin, message, attachments) →
(text, files)`. Each channel adapter handles its own
transport translation and constructs synthetic `user_id` /
`session_id` for its scoping rules (e.g., DM-vs-guild for
Discord).

*Why:* lets the same agent code serve Discord, CLI, and gws
deployments without per-channel forks.

### AGENTS.md layering: append-only on top of `BASE_INSTRUCTION`

Workspace `AGENTS.md` and other top-level markdown can add
to the system instruction but can never override or remove
a baked-in rule. `BASE_INSTRUCTION` must be self-sufficient
on its own — the agent must run coherently without any
workspace markdown loaded.

*Why:* deployments where the workspace is empty (corner
case) must still produce a sane agent. And users
customizing AGENTS.md shouldn't be able to disable safety
rules.

The dev-time and prod-time experience use the same loader;
production just gets the workspace baked into the image.

### Knowledge index sits at the tail of our composed system prompt

Layers 1–5 (BASE_INSTRUCTION → channel `extra_instruction`
→ workspace path → AGENTS.md → other workspace `*.md`) form
the cacheable prefix. Layer 6 (the knowledge index) is the
only mid-session-mutable layer and goes last so edits
invalidate only the suffix from there forward.

*Why:* Gemini's prefix-based context cache means anything
volatile inside the prefix breaks caching for the whole
prefix. The index changes on `write_knowledge` /
`delete_knowledge`; it must not sit ahead of the bulk
content.

### Knowledge index ordering: append-only by `created_at`

New entries land at the end of the rendered index.
Existing entries don't shift.

*Why:* sorting alphabetically by slug would reshuffle on
every insertion and break the cache prefix into the index
itself. Append-only ordering keeps the cache prefix into
the index growing monotonically.

### Compaction + context caching enabled by config, not new code

ADK ships `events_compaction_config` and
`context_cache_config` on `App`. Wire them up; don't
implement compaction ourselves.

Done in PR 2. Concrete numbers chosen:

- `EventsCompactionConfig(compaction_interval=10,
  overlap_size=3, token_threshold=700_000,
  event_retention_size=40)`. 700k sits well under Gemini
  3's 1M ceiling; 40 raw events at the tail preserves
  recent turn continuity.
- `ContextCacheConfig(cache_intervals=10, ttl_seconds=1800,
  min_tokens=4096)`. `min_tokens=4096` floors caching to
  requests where savings exceed cache overhead — small CLI
  one-shots aren't worth caching.

See `docs/session-memory.md` for the full rationale.

### Documentation discipline lives in `CLAUDE.md` + `docs/`

Plans and conversations expire. Each PR ships its own design
doc in `docs/`. Decisions and deferrals get logged in this
file as they happen.

### `BASE_INSTRUCTION` is self-sufficient; AGENTS.md is append-only personalization

Audit done in PR 1. Verdict: `BASE_INSTRUCTION` already
covered identity, tool semantics, working principles, and
the destructive-shell rule. The only real gap was "act on
requests, don't pingpong with clarifying questions" — added
to `BASE_INSTRUCTION`. Skills awareness comes from ADK's
auto-injected `_DEFAULT_SKILL_SYSTEM_INSTRUCTION`, so it
doesn't need to live in AGENTS.md to be load-bearing.

`templates/AGENTS.md` was slimmed to focus on the
*personalization* role — voice, project conventions, what
to remember about the user — and the customization meta
(how to edit, how skills work, live reload, switching
workspaces). It now opens with a pointer to the layering
doc and an explicit "this layer is append-only" callout.

The agent now also gets a paragraph at the top of
`BASE_INSTRUCTION` explaining the three-layer composition
so it understands where its instructions are coming from.

---

## Deferred

### Knowledge editing UX in stateless deployments

Firestore has a console UI but it's not great for markdown
editing. Probably: agent edits autonomously most of the
time; humans use the mirror CLI to round-trip to local
markdown for editing.

**Trigger:** PR 4. Concrete tool design at that point.

### Cloud Run multi-instance sqlite

`SqliteSessionService` breaks if Cloud Run scales beyond
one instance. Today we use `min-instances=1` (personal-bot
reality). Migrating to `DatabaseSessionService` → Cloud SQL
is the escape route.

**Trigger:** if we ever need to scale beyond one instance.
Not real today.

### Vector search graduation

v1 has no `search_knowledge`. The system-prompt index is
the discovery mechanism (works for hundreds of entries).
Beyond that, search graduates *per-backend natively*:
sqlite-vec sidecar for Local, Firestore vector search for
Firestore.

**Trigger:** corpus size that the index alone can't
reasonably surface. Measure first.

### Knowledge namespace privacy

Single global namespace today. Per-(user, surface)
namespacing — like the proposed Memory Bank scoping — would
prevent DM-disclosed facts surfacing in guild contexts.

**Trigger:** real usage that exposes the leak. Defer until
observed.

### Agent-managed vs human-curated knowledge

Currently the agent owns everything in `.knowledge/`. We
may want a read-only-to-agent "human-curated"
subdirectory. Trivial to add later.

**Trigger:** observed footgun where the agent overwrites a
human-curated fact.

### Bake-the-workspace tooling

Decided in PR 5: the Dockerfile does
`COPY ./templates ./workspace` and sets
`ENV ADKLAW_WORKSPACE=/code/workspace`. `templates/` is the
single source of truth for the deployed workspace — the same
directory `scripts/init-workspace.sh` already uses to seed
local workspaces. No `make bake-workspace` target needed; the
build itself is the bake step.

`.knowledge/` is intentionally *not* baked. In production
the Firestore backend is the source of truth; the local
`.knowledge/` only exists when running with
`ADKLAW_KNOWLEDGE_BACKEND=local`.

### Memory Bank (Tier 2)

Episodic recall via `VertexAiMemoryBankService` +
`PreloadMemoryTool`. Server-side LLM extraction;
`$0.25/1K events`; GA in 2026. Scoped per `(app, user)` —
needs surface-aware `user_id` to avoid DM-to-guild leakage.

**Trigger:** when the agent's apparent forgetfulness across
sessions becomes a real problem. Tiers 1 + 3 + 4 cover
most cases for personal-scale use.

---

## Rejected

### Vertex AI Search / RAG Engine / Vector Search 2.0 as Tier 3 backend

Considered for knowledge search.

*Rejected:* over-engineered for personal-bot scale.
Firestore (with native vector search if/when we need it)
is the right size — pay-per-read, no idle cost, same DB
as everything else.

### BigQuery vector / AlloyDB / Cloud SQL as Tier 3 backend

Considered for knowledge storage.

*Rejected:* always-on cost (AlloyDB / Cloud SQL) or wrong
shape for our workload (BigQuery is analytical, not
transactional).

### Unified artifact-style interface across knowledge + workspace + artifacts

Considered consolidating `BaseArtifactService` /
`BaseKnowledgeService` / workspace I/O into one interface.

*Rejected:* the *pattern* (pluggable backends) is
unifying; the *literal interface* shouldn't unify. Artifacts
save/load versioned blobs by name; knowledge has slugs and
indexed summaries; workspace has hierarchical paths +
edit-by-anchor + glob + grep + shell. Operations differ.
Forcing a shared interface would distort all three.

### Conditional workspace tool registration based on deployment

Earlier draft proposed not registering workspace tools (and
`run_shell`) on stateless deployments.

*Rejected:* Cloud Run and Agent Runtime both expose
writable filesystems. Gating tools on deployment shape
would break the customization story (skills, AGENTS.md,
shell scripts depend on the workspace existing). See the
"Workspace is universal" decision.

### Firestore ↔ local markdown mirror CLI

Considered for PR 4 as a way to let humans bulk-edit
production knowledge in their editor and `git diff` it.

*Rejected for now:* the agent owns the write path
(`write_knowledge` is the primary write); one-off human
corrections go through the Firestore console UI in ~30
seconds. Reaching for a CLI is overkill for the actual
usage shape. Data model is stable, so if real demand for
git-tracked production knowledge surfaces later, the CLI
is ~50 LOC and trivial to add. Logged in
`Deferred → Knowledge editing UX in stateless deployments`
with the trigger that would force a decision.

### `WorkspaceChannelAdapter` for the gws channel

Earlier draft proposed mirroring `DiscordChannel`'s shape
for the gws (Gemini Enterprise) channel.

*Rejected:* unnecessary. Agent Engine invokes
`App.root_agent` directly via ADK's Runner. `ChannelBase`
exists for Discord because Discord forces invention; gws
forces nothing. See the "No `GwsChannelAdapter`" decision.

### Native Vertex AI Agent Engine deployment via `gcloud ai agent-engines deploy`

Considered for PR 5 as the deploy target alongside Cloud Run.

*Rejected for now:* the project's existing infrastructure
(`Dockerfile`, `app/fast_api_app.py`, ADK's
`get_fast_api_app`) is set up for HTTP-served agents on
Cloud Run. Switching to native Agent Engine deploys would
require additional SDK plumbing for marginal gain. The
workspace-bake and runtime env-var patterns we ship in PR 5
carry over unchanged if/when native Agent Engine deploys
become the target — only the deploy command differs.

### CI/CD wiring for deploys (GitHub Actions, Cloud Build)

Considered for PR 5 to automate the deploy on push.

*Rejected for now:* this is a local-operator workflow.
`scripts/deploy.sh` is what an operator runs at the
terminal. Hooking into CI is a follow-up once deploys are
frequent enough or shared across multiple operators.
