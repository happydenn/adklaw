"""Live-reloading wrapper around ADK's SkillToolset.

ADK ships a `SkillToolset` that exposes four tools to the agent (`list_skills`,
`load_skill`, `load_skill_resource`, `run_skill_script`). Out of the box it
takes the skill list once at construction. We wrap it so an arbitrary list of
on-disk skill directories is re-scanned every turn — adding a new skill
folder, editing `SKILL.md`, or deleting a skill all take effect on the next
message without restarting the agent.

The agent passes two directories: top-level `skills/` (shipped with
the project, tracked in git) and `<workspace>/skills/` (private to the
user). On a name collision the *later* directory wins, so user skills
override shipped ones.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from google.adk.skills import (
    Skill,
    list_skills_in_dir,
    load_skill_from_dir,
)
from google.adk.tools.skill_toolset import SkillToolset
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)


class LiveSkillToolset(SkillToolset):
    """A `SkillToolset` that re-scans its skill directories on every access.

    The base class stores skills in `self._skills` at construction. This
    subclass repopulates that dict from disk before each operation that
    reads it, so users can drop new skill folders into any of the
    configured directories and have them picked up on the next turn.

    Multiple directories are scanned in order. If two directories define a
    skill with the same name, the later one wins — the agent is wired so
    user-local skills override shipped defaults.
    """

    def __init__(self, skills_dirs: Iterable[Path]):
        super().__init__(skills=[])
        self._skills_dirs = list(skills_dirs)

    def _refresh(self) -> None:
        """Re-read every configured skills directory into `self._skills`.

        Invalid skills (bad frontmatter, name mismatch, etc.) are skipped
        with a warning rather than crashing the agent.
        """
        loaded: dict[str, Skill] = {}
        for skills_dir in self._skills_dirs:
            if not skills_dir.is_dir():
                continue
            for skill_id in list_skills_in_dir(str(skills_dir)):
                skill_path = skills_dir / skill_id
                try:
                    skill = load_skill_from_dir(str(skill_path))
                except (FileNotFoundError, ValueError) as e:
                    logger.warning("Skipping invalid skill '%s': %s", skill_id, e)
                    continue
                loaded[skill.name] = skill
        self._skills = loaded

    def _list_skills(self) -> list[Skill]:
        self._refresh()
        return super()._list_skills()

    def _get_skill(self, skill_name: str) -> Skill | None:
        self._refresh()
        return super()._get_skill(skill_name)

    async def process_llm_request(
        self, *, tool_context: ToolContext, llm_request: Any
    ) -> None:
        # Refresh before the per-turn instruction injection so the model
        # sees the current skill list in its system prompt.
        self._refresh()
        await super().process_llm_request(
            tool_context=tool_context, llm_request=llm_request
        )
