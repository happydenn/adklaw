# DESIGN_SPEC.md

## Overview

`adklaw` is a general-purpose AI assistant CLI inspired by [OpenClaw](https://github.com/openclaw/openclaw), built on top of the Google Agent Development Kit (ADK). It runs locally and operates against a configurable **workspace directory** (default: `./workspace/`, but any absolute path works). The agent is customized by dropping `*.md` files into the workspace — those files are auto-loaded as additional system instructions, so the user can shape persona, style, project context, etc. without touching code.

## Example Use Cases

- **Coding helper in a project**: point workspace at a repo, drop `CONVENTIONS.md` describing style rules, ask "refactor `utils/parse.go` to handle empty input."
- **Research notebook**: point workspace at a folder of notes, drop `STYLE.md` describing how to format research summaries, ask "summarize the key claims across all `.md` files in `topics/`."
- **Personal assistant**: point workspace at `~/assistant/`, drop `PROFILE.md` describing the user's preferences, ask "draft a follow-up email about yesterday's meeting."

## Tools Required

All tools operate **rooted at the configured workspace path**. Paths are resolved against the workspace; absolute paths outside the workspace are rejected (configurable in future).

| Tool | Purpose |
|------|---------|
| `read_file(path)` | Read a text file from the workspace. |
| `write_file(path, content)` | Create or overwrite a text file. |
| `edit_file(path, old_string, new_string)` | Exact-string replacement in a file. |
| `list_dir(path)` | List entries in a workspace directory. |
| `glob_files(pattern)` | Match files via glob (e.g. `**/*.py`). |
| `grep(pattern, path)` | Regex search across files. |
| `run_shell(command)` | Execute a shell command (cwd = workspace). |
| `web_fetch(url)` | Fetch text/HTML/markdown from a URL. |

No external auth — runs locally with no API integrations beyond the LLM and `web_fetch`.

## Constraints & Safety Rules

- All filesystem operations are scoped to the workspace path. Reads/writes that would escape the workspace are rejected.
- `run_shell` runs with the workspace as `cwd`. The user owns the trust model — if they point the workspace at a sensitive dir, the agent can act there. This mirrors OpenClaw's "main session has full access" default.
- `web_fetch` is read-only and limited to HTTP(S).
- The agent does NOT auto-run shell commands without considering destructive intent — system instructions explicitly call out that the user should be told before destructive operations.

## Workspace Customization

On every turn, `adklaw` scans the workspace root for `*.md` files (top-level only, not recursive — keeps it predictable and fast) and concatenates them after the base system instruction. So:

```
workspace/
├── INSTRUCTIONS.md   # appended to system prompt
├── STYLE.md          # appended to system prompt
└── notes/            # ignored at root scan; agent can still read it via tools
```

A built-in `WORKSPACE.md` template is dropped into the default `./workspace/` on first run to show the user the pattern.

## Configuration

- `ADKLAW_WORKSPACE` env var or `--workspace <path>` flag overrides the default workspace path.
- Default workspace: `./workspace/` relative to the project root.
- Default model: `gemini-3-flash-preview` with thinking budget at "medium".

## Success Criteria

- `agents-cli run "what files are in the workspace?"` correctly invokes `list_dir` and returns the result.
- Dropping `STYLE.md` saying "respond only in haiku" causes the agent to respond only in haiku without any code change.
- `agents-cli run "fetch https://example.com and tell me the title"` correctly invokes `web_fetch`.
- Pointing `ADKLAW_WORKSPACE` at an arbitrary absolute path works the same as the default.

## Reference Samples

- None directly match. OpenClaw itself (Node.js) is the conceptual inspiration. ADK's `safety-plugins` may be revisited if guardrails are added later.

## Out of Scope (for v0)

- No multi-channel inbox (Slack/Discord/Telegram/etc.) — CLI only.
- No deployment scaffolding (Cloud Run / Agent Runtime) — prototype only.
- No persistent sessions / memory bank — each `agents-cli run` is independent.
- No sandboxing of `run_shell` — relies on workspace cwd + user trust.
