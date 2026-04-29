# Knowledge (Tier 3 semantic memory)

Durable structured facts the agent learns and reuses across
sessions: people's identifiers, project conventions,
deployment details, things the user has told you about
themselves. The store the agent can write to deliberately.

## The problem

The agent already has two sources of memory:

- **Sessions (Tier 1)** — verbatim transcript. Compacts under
  token pressure (see `docs/session-memory.md`); not durable
  for *semantic* recall after compaction.
- **Workspace markdown** — `AGENTS.md` and friends. Durable on
  local dev / Cloud Run with a volume; **vanishes on Agent
  Runtime** because the workspace filesystem is ephemeral
  there (see `docs/decisions-and-deferrals.md`).

Neither covers "I want the agent to remember `papi's Discord
ID is X` six months from now, regardless of where it's
deployed."

## What we do

A pluggable knowledge service. The interface is small —
list / read / write / delete — and backends differ in where
they put the bytes.

### Interface

`app/knowledge/base.py:BaseKnowledgeService`. Every backend
exposes:

```python
async def list_knowledge() -> list[KnowledgeIndexEntry]
async def read_knowledge(slug) -> KnowledgeEntry | None
async def write_knowledge(slug, summary, content) -> KnowledgeEntry
async def delete_knowledge(slug) -> bool
```

Each entry has three logical fields:

- `slug` — kebab-case stable identifier
  (`[a-z0-9][a-z0-9_-]*`). Filename for Local; document id
  for Firestore.
- `summary` — one short line (~120 chars) describing the
  entry. Goes into the prompt index every turn.
- `content` — schema-free markdown. The agent picks structure.

The agent supplies the summary at write time so the index
stays accurate without a separate summarization pass.

### Backends

| Backend | Storage | Used for |
|---|---|---|
| `LocalKnowledgeBackend` | markdown files under `<workspace>/.knowledge/` | local dev, Cloud Run with persistent volume |
| `FirestoreKnowledgeBackend` | Firestore collection | stateless deploys (Agent Runtime, Cloud Run without a volume) |
| `InMemoryKnowledgeBackend` | python dict | tests |

Selected by `ADKLAW_KNOWLEDGE_BACKEND` (default `local`).

### Env vars

| Var | Default | Used by |
|---|---|---|
| `ADKLAW_KNOWLEDGE_BACKEND` | `local` | always |
| `ADKLAW_KNOWLEDGE_FIRESTORE_COLLECTION` | `adklaw_knowledge` | firestore backend |
| `ADKLAW_KNOWLEDGE_FIRESTORE_PROJECT` | falls back to `GOOGLE_CLOUD_PROJECT` | firestore backend |

If neither `ADKLAW_KNOWLEDGE_FIRESTORE_PROJECT` nor
`GOOGLE_CLOUD_PROJECT` is set when the firestore backend is
selected, instantiation fails with a clear error rather than
deferring to a cryptic gRPC error at first call.

### Why a hidden directory

`.knowledge/`, not `KNOWLEDGE/`. The store is logically a
database; entries should round-trip through the tools so the
in-memory index stays in sync with disk. Hidden directory
signals "managed by the agent; touch carefully," consistent
with `.git/`, `.cache/`, `.venv/`. Humans *can* still edit via
filesystem (mtime check on session start picks up out-of-band
changes), but the friction is intentional.

### Document shape (Firestore backend)

One document per slug, doc id = slug. Fields:

| Field | Type | Notes |
|---|---|---|
| `summary` | string | One-line, ~120 chars. Surfaces in the prompt index. |
| `content` | string | Freeform markdown. |
| `created_at` | Timestamp | Set on first write, immutable thereafter. Drives index ordering (append-only). |
| `updated_at` | Timestamp | Advances on every write. |

The Firestore backend's `list_knowledge` projects only
`summary` and `created_at` server-side via `.select(...)`,
so building the prompt index doesn't pay for full content
on every entry.

The runtime service account needs Firestore read/write IAM
on the project hosting the database — typically
`roles/datastore.user` granted to the Agent Runtime / Cloud
Run service account. See `docs/deployments.md` (PR 5) for
the deployment-time grant.

### File format (Local backend)

```markdown
---
summary: papi's Discord identifier and DM allowlist
created_at: 2026-04-29T15:00:00+00:00
updated_at: 2026-04-29T15:00:00+00:00
---
papi (Discord ID: 12345) is the operator. DMs from this
account are allowed; allowlist is enforced channel-wide.
```

`summary` drives the prompt index. `created_at` drives index
ordering — append-only, so existing entries don't shift in
the rendered prompt. `updated_at` advances on every write.

## Tools the agent gets

Wrappers in `app/tools.py`:

- `list_knowledge()` — full index (also surfaced in the
  system prompt if non-empty).
- `read_knowledge(slug)` — full markdown content for one
  entry.
- `write_knowledge(slug, summary, content)` — create or
  update.
- `delete_knowledge(slug)` — remove.

Plus a paragraph in `BASE_INSTRUCTION` telling the agent
*when* to write knowledge ("user told you a stable fact",
"discovered something via tools that's useful to remember")
and *when not to* ("conversation context for the current
task", "things that change frequently", "things already
obvious from the codebase").

## Prompt index

At session start, the instruction provider in `app/agent.py`
calls `service.list_knowledge()` and renders the result into
layer 6 of the system prompt (see
`docs/instruction-layering.md`). Format:

```markdown
## Knowledge index

Durable facts you have recorded. Read with `read_knowledge(slug)`;
create or update with `write_knowledge(slug, summary, content)`.

- `papi-discord` — papi's Discord identifier and DM allowlist
- `deploy-target` — current deployment target and region
- `coding-conventions` — uv not pip, ruff for lint
```

Empty store → no index section in the prompt at all (don't
spend tokens telling the agent "your store is empty").

### Cache stability

The index sits at the **tail** of our composed system prompt
because it's the only mid-session-mutable layer (`write_knowledge`
and `delete_knowledge` mutate it during a turn). Putting it
last means edits invalidate only the index suffix; layers 1–5
stay cached. Three additional rules keep the index itself
cache-friendly:

1. **Append-only ordering by `created_at`.** New entries land
   at the end; existing entries don't shift.
2. **No volatile fields rendered.** Only `slug` and `summary`
   appear in the prompt. `updated_at` is used for cache
   invalidation but never emitted.
3. **Stable summaries.** The agent supplies the summary once
   at write time and shouldn't rewrite it on minor content
   edits — instruction-level rule, not enforced.

## Search

**v1 has no search tool.** The system-prompt index is the
discovery mechanism: the agent sees the slug + summary list
and picks which to read. Works comfortably for hundreds of
entries.

When the corpus outgrows the index, search graduates
*per-backend natively* (deferred until measured):

- *Local.* Sidecar embedding index at
  `<workspace>/.knowledge/.index/embeddings.sqlite`
  (sqlite-vec or similar). Rebuilt on `write_knowledge`.
- *Firestore.* Firestore vector search (native KNN, GA 2024).
  Embed on write into a `vector` field; query at read time.
  No extra service.

Plain grep over Firestore is intentionally not supported —
pulling every document client-side doesn't scale.

## Editing knowledge as a human

- **Local backend:** edit the file directly under
  `<workspace>/.knowledge/`. Mtime check picks up the change
  on the next session start. Just keep the YAML frontmatter
  intact — `summary` and `created_at` are required.
- **Firestore backend:** edit the document via the Firestore
  console UI (Cloud Console → Firestore → your collection).
  Round-trips to local markdown for git-tracked review are
  *not* supported in v1 — the agent owns the write path.
  See `docs/decisions-and-deferrals.md` for the trigger that
  would justify adding a mirror CLI later.

## What's currently out of scope

Captured in `docs/decisions-and-deferrals.md`:

- Per-(user, surface) namespacing for privacy (single global
  namespace today).
- Read-only-to-agent "human-curated" subdirectory.
- Encrypted entries.
- Cross-app knowledge.
