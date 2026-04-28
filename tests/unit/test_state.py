"""Tests for `app.state` — agent state directory resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.state import DEFAULT_STATE_DIR, get_state_dir


def test_get_state_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADKLAW_STATE_DIR", raising=False)
    sd = get_state_dir()
    assert sd == DEFAULT_STATE_DIR
    assert sd.is_dir()


def test_get_state_dir_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state"
    monkeypatch.setenv("ADKLAW_STATE_DIR", str(target))
    sd = get_state_dir()
    assert sd == target.resolve()
    assert sd.is_dir()
