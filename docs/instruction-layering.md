# Instruction layering

How the agent's system prompt is composed. There's a *lot* in
the final string the model sees — this doc maps where each
piece comes from, why the order matters, and what's safe to
edit at each layer.

## The problem

The agent runs against three distinct deployment shapes (local
dev, Cloud Run with a real workspace, Agent Runtime with the
workspace baked into an image) across two channels today
(Discord, CLI) and one tomorrow (gws — Gemini Enterprise via
Google Workspace surfaces). We need:

- A self-sufficient floor so the agent runs coherently even
  without a populated workspace.
- A way to personalize/customize the agent without code
  changes — drop AGENTS.md, edit, see results next message.
- Channel-specific envelope semantics (Discord's `[origin]` /
  `[reply_to]` blocks) without polluting the floor or the
  workspace.
- Cache-friendly composition so Gemini's prefix-based context
  cache actually hits between turns.

## What we do

The system instruction is composed of layers, applied in
order, sorted strictly by stability tier so the volatile
content sits at the tail and the stable bulk sits in the
prefix. Lower-numbered layers are more stable (change less
often).

```
[from app/agent.py:_instruction_provider_factory — we order these:]
1. BASE_INSTRUCTION                — codebase, hardcoded. Code-deploy only.
2. Channel extra_instruction       — codebase, hardcoded. Code-deploy only.
3. Current workspace: <path>       — stable per session.
4. Workspace AGENTS.md             — live-editable; baked stable in prod.
5. Workspace other top-level *.md  — live-editable; baked stable in prod.
6. KNOWLEDGE index                 — mid-session mutable (write_knowledge).

[appended by ADK after our string — we do NOT control position:]
7. _DEFAULT_SKILL_SYSTEM_INSTRUCTION  — hardcoded in ADK; tied to ADK version.
8. <skills> XML (names + descriptions) — from SKILL.md frontmatter.
```

Layers 1–6 come from our `_instruction_provider_factory` in
`app/agent.py`, which `ADK`'s `instructions.request_processor`
(`flows/llm_flows/instructions.py`) calls each turn and
appends to `config.system_instruction`.

Layers 7–8 are appended *after* our string by ADK's built-in
`SkillToolset.process_llm_request`
(`tools/skill_toolset.py:900`) when it runs as part of
`_process_agent_tools`. We don't control their position.

Separately, `config.tools` carries `FunctionDeclaration`s for
every registered tool (read_file, web_fetch, load_skill,
load_artifacts, etc.) — structured, not text, but also part
of the cacheable request.

## Layer 1 — `BASE_INSTRUCTION`

Hardcoded in `app/agent.py`. The self-sufficient floor: agent
identity, tool semantics, safety rules, working principles.

**Self-sufficiency rule.** The agent must run coherently on
`BASE_INSTRUCTION` alone — no workspace markdown loaded, no
KNOWLEDGE index, just the floor. This matters for stateless
deployments where the workspace might not be populated, and
for the empty-workspace developer experience.

Updates only via code review and redeployment.

## Layer 2 — Channel `extra_instruction`

Hardcoded in the channel adapter (e.g., `DiscordChannel`
declares an `extra_instruction` describing Discord's
`[origin]`, `[reply_to]`, `[context]` envelope blocks). Same
stability tier as `BASE_INSTRUCTION` — code-deploy only.

The CLI / playground / gws channel ships no
`extra_instruction` (empty string). They have no envelope
semantics to teach.

## Layers 3–5 — workspace context

- **Layer 3** (`Current workspace: <path>`) is a single line
  letting the agent know its CWD.
- **Layer 4** is the contents of `<workspace>/AGENTS.md` if
  present.
- **Layer 5** is the concatenation of every other top-level
  `*.md` file in the workspace (PROFILE.md, STYLE.md,
  CONVENTIONS.md, anything the user dropped in).

All three are loaded by
`app/workspace.py:load_workspace_instructions` and re-read
each turn. Edits take effect on the next message; no restart.

**Append-only rule.** Workspace markdown can *add* rules,
voice, project-specific conventions. It cannot remove or
override anything from `BASE_INSTRUCTION`. If you want to
disable a baked-in rule, it's a code change.

In production the workspace is typically baked into the
deployed image (`COPY workspace/ /app/workspace/` in the
Dockerfile). Same code path as local dev; the only difference
is whether the files came from the user's home directory or
from the image.

## Layer 6 — KNOWLEDGE index

(Lands in PR 3.) The list of `[{slug, summary}, …]` for every
entry in the agent's `.knowledge/` store. Only ~150 chars per
entry. Tail position because it's the only mid-session-mutable
layer — `write_knowledge` and `delete_knowledge` mutate it
during a turn.

## Layers 7–8 — ADK skills

Mechanically appended by `SkillToolset` after our string. We
don't choose the position. They contain:

- Layer 7: ADK's hardcoded `_DEFAULT_SKILL_SYSTEM_INSTRUCTION`
  — meta-rules about how to use `load_skill`.
- Layer 8: an XML block listing available skills (name +
  description from each `SKILL.md`'s frontmatter).

Skills are workspace-bound (loaded from `skills/` in the repo
+ `<workspace>/skills/`), so they're stable in production
(image-baked) but live-editable in dev.

## Why this order

Gemini context caching is prefix-based: any byte change inside
the cached prefix invalidates the cache from that point
forward. Putting volatile content early breaks the cache for
everything after it. So we sort strictly by stability:

| Tier | Layers | When does it change? |
|---|---|---|
| Most stable | 1, 2, FunctionDeclarations | Code deploy only |
| Stable (workspace-bound) | 3, 4, 5, 7, 8 | Live-editable in dev; baked stable in prod |
| Most volatile | 6 | Mid-session, on `write_knowledge` |

The bulk of the prompt (layers 1–5) is paid once per session
and cached. Layer 6 invalidates only its own suffix on edits.
Layers 7–8 are appended fresh by `SkillToolset` each turn but
they're small (~hundreds of bytes typically), so the
reprocessing cost is bounded.

## What's safe to edit where

- **Want to change a baked-in rule?** Edit
  `app/agent.py:BASE_INSTRUCTION`. Code review, redeploy.
- **Want to teach the agent your project conventions?** Edit
  `<workspace>/AGENTS.md` (or drop in `CONVENTIONS.md`). Live
  reloads next message.
- **Want to give the agent durable facts to remember?** Use
  `write_knowledge` — don't stuff facts into AGENTS.md or
  other workspace markdown.
- **Want to teach a channel about transport-specific
  envelopes?** Edit that channel's adapter (e.g.,
  `app/channels/discord.py`'s `extra_instruction`). Code
  change.
