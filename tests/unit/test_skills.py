# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Tests for `app.skills.LiveSkillToolset` — live-reloading skill discovery."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.skills import LiveSkillToolset


def _write_skill(
    root: Path,
    folder: str,
    *,
    name: str | None = None,
    description: str = "test skill",
    body: str = "skill body",
    valid: bool = True,
) -> Path:
    skill_dir = root / folder
    skill_dir.mkdir(parents=True, exist_ok=True)
    if valid:
        actual_name = name or folder
        content = (
            "---\n"
            f"name: {actual_name}\n"
            f"description: {description}\n"
            "---\n\n"
            f"{body}\n"
        )
    else:
        content = "no frontmatter at all\n"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


def test_refresh_empty_dir(tmp_path: Path) -> None:
    ts = LiveSkillToolset([tmp_path])
    ts._refresh()
    assert ts._skills == {}


def test_refresh_valid_skill(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha", description="does alpha things")
    ts = LiveSkillToolset([tmp_path])
    skills = ts._list_skills()
    assert len(skills) == 1
    assert skills[0].name == "alpha"
    assert skills[0].description == "does alpha things"


def test_refresh_skips_invalid(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_skill(tmp_path, "good")
    _write_skill(tmp_path, "bad", valid=False)
    ts = LiveSkillToolset([tmp_path])
    with caplog.at_level(logging.WARNING, logger="app.skills"):
        skills = ts._list_skills()
    names = {s.name for s in skills}
    assert "good" in names
    assert "bad" not in names
    assert any("bad" in r.message for r in caplog.records)


def test_refresh_user_overrides_default(tmp_path: Path) -> None:
    shipped = tmp_path / "shipped"
    user = tmp_path / "user"
    _write_skill(shipped, "translator", description="shipped version")
    _write_skill(user, "translator", description="user override")
    ts = LiveSkillToolset([shipped, user])
    skills = ts._list_skills()
    assert len(skills) == 1
    assert skills[0].description == "user override"


def test_list_skills_calls_refresh(tmp_path: Path) -> None:
    ts = LiveSkillToolset([tmp_path])
    assert ts._list_skills() == []
    _write_skill(tmp_path, "fresh")
    skills_after = ts._list_skills()
    assert len(skills_after) == 1
    assert skills_after[0].name == "fresh"
