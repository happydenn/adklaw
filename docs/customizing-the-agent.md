# Customizing the agent

How to specialize the agent's behavior for a specific
workspace, without touching code. Three lever points, in
increasing scope.

## 1. `<workspace>/AGENTS.md`

The personalization layer. Identity, persona, conventions,
constraints — anything you'd tell a new collaborator about how
*this* workspace works. The agent runs perfectly well without
it; an empty workspace is a valid workspace.

`scripts/init-workspace.sh` seeds your workspace with
`templates/AGENTS.md`, a starter scaffold with named sections
(`Identity`, `Persona / voice`, `About the user`, `Project
conventions`, `Constraints`, `Notes`). Fill in what's
relevant; delete what isn't. Empty sections show
`_Not yet specified._` and act as a cue for the agent to ask
the user when the topic comes up.

**Append-only on top of `BASE_INSTRUCTION`.** Workspace
customization can *add* rules and personality but can't
remove or override safety / tool-use rules baked into the
agent. If you need to override a baked-in rule, that's a
code change to `app/agent.py:BASE_INSTRUCTION`, not a
workspace edit.

You can also drop **other `*.md` files** at the workspace
root and they'll be appended after `AGENTS.md` as additional
context. Useful for splitting concerns:

- `STYLE.md` — voice, formatting, length preferences
- `PROFILE.md` — facts about the user
- `CONVENTIONS.md` — project-specific rules

Files in **subdirectories** are not auto-loaded. The agent
can still read them through tools when relevant.

## 2. Skills

Skills are folders with a `SKILL.md` file (YAML frontmatter
for `name` + `description`, then a markdown body of
instructions). The agent automatically lists available skills
in its system prompt; when one looks relevant the agent loads
its full instructions via `load_skill` and follows them. A
skill can also include `references/`, `assets/`, and
`scripts/` subfolders the agent reads on demand. See
[agentskills.io](https://agentskills.io/specification) for
the full spec.

Skills are loaded from two places:

1. **`skills/`** at the repo root — shipped with the project,
   tracked in git. Edit or add skills here to share them with
   everyone who clones the repo.
2. **`<workspace>/skills/`** — your private skills, ignored
   by git. Drop a skill folder here to use it locally
   without committing.

A skill in `<workspace>/skills/` with the same `name` as one
in the top-level `skills/` overrides the shipped one for you.

## 3. Knowledge entries

Durable facts the agent records via `write_knowledge` (see
`docs/knowledge.md`). Different role from `AGENTS.md`:
`AGENTS.md` is *how* the agent should behave; knowledge
entries are *what* the agent knows. The agent writes these
itself as it learns; humans can edit the underlying
`<workspace>/.knowledge/<slug>.md` files when running with
the Local backend.

## Live reload

Edits to `AGENTS.md`, other top-level workspace `*.md` files,
or anything under `skills/` or `<workspace>/skills/` take
effect on the **next message** — no restart needed. Each turn
re-reads these files from disk.

## Switching workspaces

```bash
export ADKLAW_WORKSPACE=~/my-project
agents-cli run "what is in this workspace?"
```

The `agents-cli playground` and the Discord channel honor
the same env var.

## Lifecycle: dev → production

During development, edit `AGENTS.md`, `skills/`, etc. live;
the agent picks them up next turn. When you're ready to
deploy, **bake the workspace into the deployed image** —
typically `COPY workspace/ /app/workspace/` in the
Dockerfile (see `docs/deployments.md` once it lands in PR
5). Same loader code in production; the only difference is
where the files came from.
