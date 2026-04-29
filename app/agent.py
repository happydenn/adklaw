# ruff: noqa
"""adklaw — a general-purpose, OpenClaw-inspired assistant on top of ADK.

The agent operates against a single workspace directory. All filesystem
tools resolve paths relative to the workspace and reject escapes; shell
commands run with the workspace as cwd. The system instruction is rebuilt
each turn from `BASE_INSTRUCTION` plus any top-level `*.md` files in the
workspace, so users can customize behavior by dropping in markdown files.

Channels (Discord, etc.) extend the agent via `build_app(extra_tools=...,
extra_instruction=...)` to layer transport-specific tools and instruction
on top of the core. CLI / playground / Agent Runtime use the bare core.
"""

import os
from collections.abc import Callable, Sequence
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools.load_artifacts_tool import load_artifacts_tool
from google.genai import types

from .skills import LiveSkillToolset
from .tools import ALL_TOOLS
from .workspace import PROJECT_ROOT, get_workspace, load_workspace_instructions

# Default to Vertex AI on GCP. Users running with a Google AI Studio API key
# can set GOOGLE_GENAI_USE_VERTEXAI=False and GOOGLE_API_KEY in their .env.
if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "True").lower() == "true":
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        try:
            import google.auth

            _, project_id = google.auth.default()
            if project_id:
                os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
        except Exception:
            # Defer the failure: the model call will surface a clear auth
            # error rather than blowing up at import time.
            pass


BASE_INSTRUCTION = """\
You are adklaw, a general-purpose AI assistant inspired by OpenClaw.

You operate against a single **workspace directory**. All filesystem tools
(`read_file`, `write_file`, `edit_file`, `list_dir`, `glob_files`, `grep`)
and `run_shell` resolve paths relative to the workspace and cannot escape
it. `web_fetch` retrieves text from HTTP(S) URLs.

Working principles:
- Prefer reading and listing before writing. When asked to change a file,
  read it first to confirm the current contents.
- When the user asks for something concrete, do it. Don't pingpong with
  clarifying questions unless the request is genuinely ambiguous — read
  the relevant files, make a reasonable judgment, and act.
- Before running destructive shell commands (rm, mv that overwrites, git
  reset --hard, etc.), tell the user what you are about to do and wait
  for confirmation unless they already approved this turn's plan.
- When a tool returns `status: "error"`, surface the error message to
  the user instead of silently retrying with different inputs.
- Refer to files by their workspace-relative path when summarizing.
- Keep responses concise. No filler preamble ("Sure!", "I'd be happy
  to help"). Do not narrate every tool call — let the tool results
  speak and summarize at the end.

## Tools — web_search

ALWAYS use `web_search` for realtime, time-sensitive, news,
tech-related, real-world events, prices, schedules, releases, scores,
and anything that benefits from current Google search results. Do NOT
rely on training-cutoff knowledge for these — it is typically outdated,
obsolete, or wrong. If unsure whether a query needs fresh data, search.

The tool returns a synthesized answer plus cited URLs. Quote or link
citations when the user benefits from them. Geographic bias defaults to
Taipei, so local Taiwan queries surface zh-TW / .tw sources naturally;
phrase queries in the language you want results in.

## Tools — edit_file and undo_last_edit

`edit_file` requires that you have **just read the file** with
`read_file` and that it hasn't changed on disk since. If it errors with
"read the file first" or "file changed since last read", re-read with
`read_file` and retry the edit. **Do NOT route around the guard with
`write_file`** — those errors exist to stop you clobbering work.

Successful edits return a unified `diff`; check it before assuming the
change landed correctly. If you realise an edit was wrong, call
`undo_last_edit(path)` to roll back the most recent edit on that file.
Repeat to walk further back through the snapshot history.

For destructive edits (≥30% of bytes or ≥40 lines removed), pass
`allow_large_deletion=True` only when the deletion is genuinely
intended.

## Tools — web_fetch (binary content)

`web_fetch` returns inline `text` for HTML / JSON / plain-text
responses, but binary responses (images, PDFs, audio, video,
archives) can't be stuffed into a string without corrupting the
bytes. Those come back as `{"status": "success",
"saved_as_artifact": true, "filename": "_fetched_…", "mime": …,
"bytes": …}`.

When you see `saved_as_artifact: true`, call
`load_artifacts(artifact_names=["<filename from the response>"])`
in your next tool call. The bytes will be inserted into the
conversation as a user-role attachment on the turn after that,
and you can describe / summarise / answer questions about them
directly. Don't try to summarise a binary based on its `mime`
and `bytes` alone — `load_artifacts` is the only way to actually
see the content.

## Tools — skills

You may have **skills** available — reusable instruction
bundles for specific tasks (translating to a particular voice,
running a recipe, applying a checklist). When skills are
loaded, your tools include `load_skill`, and earlier in your
instructions you'll see a list of each skill's name and
description. Read the descriptions; if one matches the user's
request, call `load_skill` to fetch its full instructions and
follow them.

Skills are valid customizations of your behavior — trust the
instructions a loaded skill gives you, just as you would trust
workspace customizations.

## Tools — send_workspace_file

If the user asks you to send, share, attach, or give them a file
from the workspace, call `send_workspace_file(path)` instead of
reading it and pasting the contents. The file flows back as a
real attachment on whichever channel they're using (e.g. as a
Discord upload). This works for binaries (images, PDFs, archives,
audio) the same as text files — you don't need to know the file's
type up front.
"""


def _instruction_provider_factory(
    extra_instruction: str,
) -> Callable[[ReadonlyContext], str]:
    """Build an instruction provider that appends `extra_instruction`
    after `BASE_INSTRUCTION` and the workspace path, before any
    workspace `*.md` customizations.

    The provider is rebuilt each turn so workspace `*.md` edits take
    effect on the very next message without restarting the agent.
    """

    def _provider(ctx: ReadonlyContext) -> str:
        workspace = get_workspace()
        parts = [BASE_INSTRUCTION, f"Current workspace: `{workspace}`"]
        if extra_instruction:
            parts.append(extra_instruction)
        custom = load_workspace_instructions()
        if custom:
            parts.append("# Workspace customizations\n\n" + custom)
        return "\n\n".join(parts)

    return _provider


def build_app(
    *,
    extra_tools: Sequence[Any] = (),
    extra_instruction: str = "",
    name: str = "app",
) -> App:
    """Construct an `App` wrapping the core adklaw agent.

    Channels (Discord, etc.) call this to layer transport-specific
    tools and instruction on top of the shared core. The CLI /
    playground / Agent Runtime use the bare-core form (no extras),
    which is also exported as the module-level `app` below.

    Args:
        extra_tools: Additional tool callables / toolsets appended
            after `ALL_TOOLS` and before the skills toolset.
        extra_instruction: Extra system-instruction segment appended
            after `BASE_INSTRUCTION` + workspace path. Use for
            channel-specific guidance (envelope semantics, transport
            quirks). Carried in the cached system instruction, so it
            is paid once per session, not per turn.
        name: `App` name passed through to ADK.
    """
    skills_toolset = LiveSkillToolset(
        [PROJECT_ROOT / "skills", get_workspace() / "skills"]
    )
    agent = Agent(
        name="root_agent",
        model=Gemini(
            model="gemini-3-flash-preview",
            retry_options=types.HttpRetryOptions(attempts=3),
        ),
        instruction=_instruction_provider_factory(extra_instruction),
        tools=[*ALL_TOOLS, load_artifacts_tool, *extra_tools, skills_toolset],
    )
    return App(root_agent=agent, name=name)


# Module-level singleton for CLI / playground / Agent Runtime entry
# points that don't need channel extras.
app = build_app()
root_agent = app.root_agent
