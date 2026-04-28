# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Tests for `app.channels._env.load_channel_env`.

`find_dotenv` walks up from the module's own file (`usecwd=False`) — not
the cwd — so the tests patch `find_dotenv` in the module under test to
point at a tmp `.env`. That way the assertions cover the contract we
actually rely on (`load_dotenv(found, override=False)`) without
depending on the test runner's working tree.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.channels import _env as env_module


def test_disable_var_skips_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("ADKLAW_TEST_FOO=bar\n", encoding="utf-8")
    monkeypatch.setattr(
        env_module, "find_dotenv", lambda **kwargs: str(dotenv)
    )
    monkeypatch.setenv("ADK_DISABLE_LOAD_DOTENV", "1")
    monkeypatch.delenv("ADKLAW_TEST_FOO", raising=False)
    env_module.load_channel_env()
    assert "ADKLAW_TEST_FOO" not in os.environ


def test_loads_dotenv_when_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("ADKLAW_TEST_FOO=bar\n", encoding="utf-8")
    monkeypatch.setattr(
        env_module, "find_dotenv", lambda **kwargs: str(dotenv)
    )
    monkeypatch.delenv("ADK_DISABLE_LOAD_DOTENV", raising=False)
    monkeypatch.delenv("ADKLAW_TEST_FOO", raising=False)
    env_module.load_channel_env()
    assert os.environ.get("ADKLAW_TEST_FOO") == "bar"


def test_existing_env_not_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("ADKLAW_TEST_FOO=clobber\n", encoding="utf-8")
    monkeypatch.setattr(
        env_module, "find_dotenv", lambda **kwargs: str(dotenv)
    )
    monkeypatch.delenv("ADK_DISABLE_LOAD_DOTENV", raising=False)
    monkeypatch.setenv("ADKLAW_TEST_FOO", "keep")
    env_module.load_channel_env()
    assert os.environ["ADKLAW_TEST_FOO"] == "keep"


def test_missing_env_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env_module, "find_dotenv", lambda **kwargs: "")
    monkeypatch.delenv("ADK_DISABLE_LOAD_DOTENV", raising=False)
    monkeypatch.delenv("ADKLAW_TEST_FOO", raising=False)
    env_module.load_channel_env()  # should not raise
    assert "ADKLAW_TEST_FOO" not in os.environ
