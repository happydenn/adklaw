"""Tests for the module-level helpers in `app.channels.discord`."""

from __future__ import annotations

import pytest

from app.channels.discord import (
    DEFAULT_HISTORY_LINES,
    DISCORD_MESSAGE_LIMIT,
    _allowed_user_ids,
    _allowlist_scope,
    _history_limit,
    _split_for_discord,
)

# ---------------------------------------------------------------------------
# _split_for_discord
# ---------------------------------------------------------------------------


def test_split_short() -> None:
    assert _split_for_discord("hello") == ["hello"]


def test_split_long_breaks_at_newline() -> None:
    # 1500 + \n + 1500 = 3001 chars; first chunk should split at the newline.
    line = "a" * 1500
    text = line + "\n" + line
    chunks = _split_for_discord(text, limit=2000)
    assert len(chunks) == 2
    assert all(len(c) <= 2000 for c in chunks)
    assert chunks[0] == line  # broke cleanly on the newline, no trailing \n
    assert chunks[1] == line


def test_split_no_newlines_hard_splits() -> None:
    chunks = _split_for_discord("a" * 5000, limit=2000)
    assert len(chunks) == 3
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == "a" * 5000


def test_split_strips_leading_newlines() -> None:
    line = "x" * 1500
    text = line + "\n\n\n" + line  # forces a break at the newlines
    chunks = _split_for_discord(text, limit=2000)
    assert len(chunks) >= 2
    for chunk in chunks[1:]:
        assert not chunk.startswith("\n")


def test_default_limit_is_discord_max() -> None:
    assert DISCORD_MESSAGE_LIMIT == 2000


# ---------------------------------------------------------------------------
# _allowed_user_ids
# ---------------------------------------------------------------------------


def test_allowed_user_ids_empty_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    assert _allowed_user_ids() == frozenset()


def test_allowed_user_ids_blank_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "   ")
    assert _allowed_user_ids() == frozenset()


def test_allowed_user_ids_parses_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "1, 2 ,,3")
    assert _allowed_user_ids() == frozenset({"1", "2", "3"})


# ---------------------------------------------------------------------------
# _allowlist_scope
# ---------------------------------------------------------------------------


def test_allowlist_scope_default_is_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_ALLOWLIST_SCOPE", raising=False)
    assert _allowlist_scope() == "dm"


def test_allowlist_scope_explicit_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_ALLOWLIST_SCOPE", "all")
    assert _allowlist_scope() == "all"


def test_allowlist_scope_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_ALLOWLIST_SCOPE", "ALL")
    assert _allowlist_scope() == "all"


def test_allowlist_scope_unknown_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    monkeypatch.setenv("DISCORD_ALLOWLIST_SCOPE", "wat")
    with caplog.at_level(logging.WARNING, logger="app.channels.discord"):
        assert _allowlist_scope() == "dm"
    assert any("wat" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _history_limit
# ---------------------------------------------------------------------------


def test_history_limit_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_CONTEXT_HISTORY_LINES", raising=False)
    assert _history_limit() == DEFAULT_HISTORY_LINES == 20


def test_history_limit_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_CONTEXT_HISTORY_LINES", "0")
    assert _history_limit() == 0


def test_history_limit_explicit_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_CONTEXT_HISTORY_LINES", "5")
    assert _history_limit() == 5


def test_history_limit_negative_clamps_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_CONTEXT_HISTORY_LINES", "-3")
    assert _history_limit() == 0


def test_history_limit_invalid_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    monkeypatch.setenv("DISCORD_CONTEXT_HISTORY_LINES", "wat")
    with caplog.at_level(logging.WARNING, logger="app.channels.discord"):
        assert _history_limit() == DEFAULT_HISTORY_LINES
    assert any("wat" in r.message for r in caplog.records)
