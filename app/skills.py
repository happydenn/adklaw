# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Live-reloading wrapper around ADK's SkillToolset.

ADK ships a `SkillToolset` that exposes four tools to the agent (`list_skills`,
`load_skill`, `load_skill_resource`, `run_skill_script`). Out of the box it
takes the skill list once at construction. We wrap it so the on-disk
`workspace/skills/` directory is re-scanned every turn — adding a new skill
folder, editing `SKILL.md`, or deleting a skill all take effect on the next
message without restarting the agent.
"""

from __future__ import annotations

import logging
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
    """A `SkillToolset` that re-scans its skills directory on every access.

    The base class stores skills in `self._skills` at construction. This
    subclass repopulates that dict from disk before each operation that
    reads it, so users can drop new skill folders into the workspace and
    have them picked up on the next turn.
    """

    def __init__(self, skills_dir: Path):
        super().__init__(skills=[])
        self._skills_dir = skills_dir

    def _refresh(self) -> None:
        """Re-read the skills directory into `self._skills`.

        Invalid skills (bad frontmatter, name mismatch, etc.) are skipped
        with a warning rather than crashing the agent.
        """
        if not self._skills_dir.is_dir():
            self._skills = {}
            return

        loaded: dict[str, Skill] = {}
        for skill_id in list_skills_in_dir(str(self._skills_dir)):
            skill_path = self._skills_dir / skill_id
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
