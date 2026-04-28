"""Tests for `app.workspace` — workspace path resolution and `*.md` loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.workspace import (
    DEFAULT_WORKSPACE,
    PRIMARY_FILE,
    get_workspace,
    load_workspace_instructions,
    resolve_in_workspace,
)


def test_get_workspace_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADKLAW_WORKSPACE", raising=False)
    assert get_workspace() == DEFAULT_WORKSPACE


def test_get_workspace_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("ADKLAW_WORKSPACE", str(target))
    ws = get_workspace()
    assert ws == target.resolve()
    assert ws.is_dir()


def test_resolve_in_workspace_relative(workspace_dir: Path) -> None:
    resolved = resolve_in_workspace("foo.txt")
    assert resolved == workspace_dir / "foo.txt"


def test_resolve_in_workspace_absolute_inside(workspace_dir: Path) -> None:
    abs_inside = workspace_dir / "nested" / "thing.txt"
    resolved = resolve_in_workspace(str(abs_inside))
    assert resolved == abs_inside


def test_resolve_in_workspace_escape_rejected(workspace_dir: Path) -> None:
    with pytest.raises(ValueError):
        resolve_in_workspace("../oops")


def test_resolve_in_workspace_symlink_escape_rejected(
    workspace_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace_dir / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        resolve_in_workspace("link.txt")


def test_load_workspace_instructions_empty(workspace_dir: Path) -> None:
    assert load_workspace_instructions() == ""


def test_load_workspace_instructions_only_agents_md(workspace_dir: Path) -> None:
    (workspace_dir / PRIMARY_FILE).write_text("you are tested", encoding="utf-8")
    out = load_workspace_instructions()
    assert out.startswith("# Primary instructions (from `AGENTS.md`)")
    assert "you are tested" in out
    assert "# Additional context" not in out


def test_load_workspace_instructions_only_other(workspace_dir: Path) -> None:
    (workspace_dir / "STYLE.md").write_text("be terse", encoding="utf-8")
    out = load_workspace_instructions()
    assert "# Primary instructions" not in out
    assert "# Additional context" in out
    assert "## From `STYLE.md`" in out
    assert "be terse" in out


def test_load_workspace_instructions_both(workspace_dir: Path) -> None:
    (workspace_dir / PRIMARY_FILE).write_text("primary body", encoding="utf-8")
    (workspace_dir / "STYLE.md").write_text("style body", encoding="utf-8")
    out = load_workspace_instructions()
    primary_idx = out.index("# Primary instructions")
    extras_idx = out.index("# Additional context")
    assert primary_idx < extras_idx
    assert "primary body" in out
    assert "style body" in out


def test_load_workspace_instructions_sorted(workspace_dir: Path) -> None:
    (workspace_dir / "CCC.md").write_text("ccc", encoding="utf-8")
    (workspace_dir / "AAA.md").write_text("aaa", encoding="utf-8")
    (workspace_dir / "BBB.md").write_text("bbb", encoding="utf-8")
    out = load_workspace_instructions()
    assert out.index("AAA.md") < out.index("BBB.md") < out.index("CCC.md")


def test_load_workspace_instructions_skips_empty(workspace_dir: Path) -> None:
    (workspace_dir / "EMPTY.md").write_text("", encoding="utf-8")
    (workspace_dir / "FILLED.md").write_text("filled", encoding="utf-8")
    out = load_workspace_instructions()
    assert "EMPTY.md" not in out
    assert "FILLED.md" in out


def test_load_workspace_instructions_ignores_subdirs(workspace_dir: Path) -> None:
    nested = workspace_dir / "notes"
    nested.mkdir()
    (nested / "deep.md").write_text("hidden", encoding="utf-8")
    assert load_workspace_instructions() == ""
