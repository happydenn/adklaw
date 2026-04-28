# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

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
