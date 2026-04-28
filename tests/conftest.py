"""Shared pytest fixtures for the adklaw test suite.

Each test gets a hermetic workspace and state directory under `tmp_path`,
with the corresponding env vars patched in. The workspace, state, and
skills modules read these env vars on every call, so tests stay isolated
without monkey-patching internals.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def workspace_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic workspace under `tmp_path`. Sets `ADKLAW_WORKSPACE` and
    creates the directory eagerly so tests can drop files into it."""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ADKLAW_WORKSPACE", str(ws))
    return ws.resolve()


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic state dir under `tmp_path`. Sets `ADKLAW_STATE_DIR`."""
    sd = tmp_path / ".adklaw"
    monkeypatch.setenv("ADKLAW_STATE_DIR", str(sd))
    return sd.resolve()


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset any module-level `@functools.cache` state between tests so
    env-driven config is recomputed.

    Also removes the channel-config env vars so a developer's local
    `.env` (auto-loaded by `uv run`) can't leak into tests. Each test
    that needs a specific value must `monkeypatch.setenv` it
    explicitly.
    """
    from app import tools as tools_module
    from app import workspace as workspace_module
    from app.channels.discord import (
        _allowed_user_ids,
        _allowlist_scope,
        _history_limit,
        _quote_bot_replies,
        _reply_to_bots,
    )
    from app.tools import _web_search_client

    _allowed_user_ids.cache_clear()
    _allowlist_scope.cache_clear()
    _history_limit.cache_clear()
    _quote_bot_replies.cache_clear()
    _reply_to_bots.cache_clear()
    _web_search_client.cache_clear()
    tools_module._file_read_cache.clear()
    workspace_module._warned_no_agents_md = False

    for name in (
        "DISCORD_ALLOWED_USER_IDS",
        "DISCORD_ALLOWLIST_SCOPE",
        "DISCORD_CONTEXT_HISTORY_LINES",
        "DISCORD_QUOTE_BOT_REPLIES",
        "DISCORD_REPLY_TO_BOTS",
        "ADKLAW_WEB_SEARCH_LATLNG",
        "ADKLAW_WEB_SEARCH_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
