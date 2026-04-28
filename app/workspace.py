"""Workspace path resolution and markdown-based customization loading.

The workspace is the directory the agent operates against. By default it lives
at `./workspace/` relative to the project root, but the `ADKLAW_WORKSPACE`
environment variable can point to any absolute path.

`AGENTS.md` at the workspace root is the **primary** customization file — it
defines what the agent is and what it should do. Any other top-level `*.md`
files are loaded as supplementary context. Files are re-read every turn, so
edits take effect on the next message without restarting the agent.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACE = PROJECT_ROOT / "workspace"

# Module-level latch so the "no AGENTS.md, run the init script" hint
# fires exactly once per process. The autouse fixture in
# tests/conftest.py resets this between tests.
_warned_no_agents_md: bool = False


def get_workspace() -> Path:
    """Return the resolved workspace path, creating it if it does not exist."""
    raw = os.environ.get("ADKLAW_WORKSPACE")
    path = Path(raw).expanduser().resolve() if raw else DEFAULT_WORKSPACE
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_in_workspace(path: str) -> Path:
    """Resolve `path` relative to the workspace and reject escapes.

    Accepts either a path relative to the workspace or an absolute path that
    is already inside the workspace. Raises ValueError for anything that
    would escape the workspace root.
    """
    workspace = get_workspace()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as e:
        raise ValueError(
            f"Path {candidate} is outside the workspace ({workspace})."
        ) from e
    return candidate


PRIMARY_FILE = "AGENTS.md"


def _read_md(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_workspace_instructions() -> str:
    """Read top-level markdown files from the workspace.

    `AGENTS.md` is loaded first under a "Primary instructions" heading; other
    top-level `*.md` files (sorted alphabetically for determinism) are loaded
    after under "Additional context". Returns an empty string if neither
    exists.
    """
    global _warned_no_agents_md
    workspace = get_workspace()
    sections: list[str] = []

    primary = workspace / PRIMARY_FILE
    primary_content = _read_md(primary) if primary.is_file() else ""
    if primary_content:
        sections.append(
            f"# Primary instructions (from `{PRIMARY_FILE}`)\n\n{primary_content}"
        )
    elif not _warned_no_agents_md:
        # Hint, not an error — the agent runs fine without a persona.
        # Logged once per process so we don't spam at every turn.
        logger.info(
            "No %s in workspace %s; run `scripts/init-workspace.sh` to seed "
            "one from `templates/AGENTS.md`.",
            PRIMARY_FILE,
            workspace,
        )
        _warned_no_agents_md = True

    extras: list[str] = []
    for md in sorted(workspace.glob("*.md")):
        if md.name == PRIMARY_FILE:
            continue
        content = _read_md(md)
        if content:
            extras.append(f"## From `{md.name}`\n\n{content}")
    if extras:
        sections.append("# Additional context\n\n" + "\n\n".join(extras))

    return "\n\n".join(sections)
