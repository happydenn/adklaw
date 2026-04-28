"""Sanity tests for `templates/AGENTS.md` — the persona seed file."""

from __future__ import annotations

from app.workspace import PROJECT_ROOT


def test_template_agents_md_exists() -> None:
    template = PROJECT_ROOT / "templates" / "AGENTS.md"
    assert template.is_file(), f"Missing template: {template}"


def test_template_agents_md_is_non_empty_and_mentions_keywords() -> None:
    template = PROJECT_ROOT / "templates" / "AGENTS.md"
    content = template.read_text(encoding="utf-8")
    assert content.strip(), "templates/AGENTS.md is empty"
    # Sanity that the seed actually documents the persona machinery.
    for keyword in ("AGENTS.md", "skills"):
        assert keyword in content, (
            f"templates/AGENTS.md should mention '{keyword}'"
        )


def test_init_workspace_script_exists_and_is_executable() -> None:
    import os

    script = PROJECT_ROOT / "scripts" / "init-workspace.sh"
    assert script.is_file(), f"Missing script: {script}"
    assert os.access(script, os.X_OK), f"{script} is not executable"
