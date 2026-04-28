# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""adklaw — a general-purpose, OpenClaw-inspired assistant on top of ADK.

The agent operates against a single workspace directory. All filesystem
tools resolve paths relative to the workspace and reject escapes; shell
commands run with the workspace as cwd. The system instruction is rebuilt
each turn from `BASE_INSTRUCTION` plus any top-level `*.md` files in the
workspace, so users can customize behavior by dropping in markdown files.
"""

import os

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.models import Gemini
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
- Before running destructive shell commands (rm, mv that overwrites, git
  reset --hard, etc.), tell the user what you are about to do and wait
  for confirmation unless they already approved this turn's plan.
- When a tool returns `status: "error"`, surface the error message to
  the user instead of silently retrying with different inputs.
- Refer to files by their workspace-relative path when summarizing.
- Keep responses concise. Do not narrate every tool call — let the tool
  results speak and summarize at the end.
"""


def _instruction_provider(ctx: ReadonlyContext) -> str:
    """Rebuild the system instruction each turn so workspace `*.md` edits
    take effect on the very next message without restarting the agent."""
    workspace = get_workspace()
    parts = [BASE_INSTRUCTION, f"Current workspace: `{workspace}`"]
    custom = load_workspace_instructions()
    if custom:
        parts.append("# Workspace customizations\n\n" + custom)
    return "\n\n".join(parts)


# Live-reloading skills toolset. Two directories are scanned every turn:
# - `default_skills/` ships with the project and is tracked in git.
# - `<workspace>/skills/` is the user's private overlay; user skills with
#   the same name as a default override it.
_skills_toolset = LiveSkillToolset(
    [PROJECT_ROOT / "default_skills", get_workspace() / "skills"]
)

root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-3-flash-preview",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=_instruction_provider,
    generate_content_config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="medium"),
    ),
    tools=[*ALL_TOOLS, _skills_toolset],
)

app = App(
    root_agent=root_agent,
    name="app",
)
