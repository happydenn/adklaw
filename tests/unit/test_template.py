"""Sanity tests for `templates/AGENTS.md` — the persona seed file."""

from __future__ import annotations

from app.workspace import PROJECT_ROOT


def test_template_agents_md_exists() -> None:
    template = PROJECT_ROOT / "templates" / "AGENTS.md"
    assert template.is_file(), f"Missing template: {template}"


def test_template_agents_md_is_a_fillable_scaffold() -> None:
    """The template is a *scaffold* the agent and human fill in
    together over their first conversations, not a tutorial.

    Voice and shape are inspired by OpenClaw's IDENTITY.md /
    USER.md / SOUL.md / BOOTSTRAP.md templates, consolidated into
    a single file: warm second-person, fillable bullets with
    parenthetical hints, baked-in default values, and a first-run
    cue. User-facing instructions on how to customize the agent
    live in `docs/customizing-the-agent.md`.

    Check the structural shape — fillable bullets and a first-run
    section — without locking in the prose so the file can keep
    evolving.
    """
    template = PROJECT_ROOT / "templates" / "AGENTS.md"
    content = template.read_text(encoding="utf-8")
    assert content.strip(), "templates/AGENTS.md is empty"
    # The two essential fillable sections: who the agent is, who
    # the human is.
    assert "Who you are" in content, (
        "templates/AGENTS.md should have a 'Who you are' section "
        "for the agent's identity."
    )
    assert "About your human" in content, (
        "templates/AGENTS.md should have an 'About your human' "
        "section for the user profile."
    )
    # First-run cue so the agent knows what to do on a fresh
    # workspace where everything is still a placeholder.
    assert "First run" in content, (
        "templates/AGENTS.md should have a 'First run' section "
        "guiding the agent's first conversation in a fresh workspace."
    )
    # Italicized invitation — the OpenClaw flavor that makes this
    # a real scaffold rather than a form.
    assert "Make it yours" in content or "make it yours" in content.lower()


def test_init_workspace_script_exists_and_is_executable() -> None:
    import os

    script = PROJECT_ROOT / "scripts" / "init-workspace.sh"
    assert script.is_file(), f"Missing script: {script}"
    assert os.access(script, os.X_OK), f"{script} is not executable"
