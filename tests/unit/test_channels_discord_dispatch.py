# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Tests for `DiscordChannel._on_message` — gating + dispatch logic.

Constructs a `DiscordChannel` with stub plumbing — no real `Runner`, no
real Discord gateway — and feeds it a `FakeMessage` shaped like what
discord.py would deliver. We then assert on what the channel did with it
(replied, dispatched to `handle_message`, ignored).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.channels import base as base_module
from app.channels.base import Origin
from app.channels.discord import DiscordChannel

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeUser:
    def __init__(self, *, id: int, name: str = "TestBot") -> None:
        self.id = id
        self.display_name = name
        self.bot = False


@dataclass
class _FakeAuthor:
    id: int
    display_name: str = "tester"
    bot: bool = False


@dataclass
class _FakeGuild:
    id: int
    name: str


@dataclass
class _FakeChannel:
    id: int
    name: str = "general"

    def typing(self) -> Any:
        @asynccontextmanager
        async def _cm():
            yield

        return _cm()


@dataclass
class _FakeMessage:
    id: int
    author: _FakeAuthor
    channel: _FakeChannel
    clean_content: str
    guild: _FakeGuild | None = None
    mentions: list[Any] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)

    async def reply(self, text: str) -> None:
        self.replies.append(text)


class _StubRunner:
    """No-op Runner replacement so ChannelBase can be constructed without ADK."""

    def __init__(self, *, app: Any, session_service: Any, auto_create_session: bool):
        self.app = app
        self.session_service = session_service


@pytest.fixture
def channel(monkeypatch: pytest.MonkeyPatch) -> DiscordChannel:
    """Construct a DiscordChannel with stub Runner + stub handle_message."""
    monkeypatch.setattr(base_module, "Runner", _StubRunner)

    bot_user = _FakeUser(id=999, name="TestBot")

    # We don't want a real discord.Client either — patch it before construction.
    import discord

    real_client_cls = discord.Client

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.user = bot_user
            self._handlers: dict[str, Any] = {}

        def event(self, fn: Any) -> Any:
            self._handlers[fn.__name__] = fn
            return fn

    monkeypatch.setattr(discord, "Client", _FakeClient)
    try:
        ch = DiscordChannel(
            app=object(), session_service=object(), token="t-fake"
        )
    finally:
        monkeypatch.setattr(discord, "Client", real_client_cls)

    # Replace handle_message with an AsyncMock so we can assert on call args.
    ch.handle_message = AsyncMock(return_value="agent reply")  # type: ignore[method-assign]
    return ch


def _bot_user_for(ch: DiscordChannel) -> _FakeUser:
    return ch._client.user  # type: ignore[return-value]


def _make_message(
    *,
    sender_id: int,
    content: str,
    is_dm: bool,
    bot: bool = False,
    mention_bot: DiscordChannel | None = None,
    sender_name: str = "papi",
) -> _FakeMessage:
    author = _FakeAuthor(id=sender_id, display_name=sender_name, bot=bot)
    channel = _FakeChannel(id=42, name="general")
    guild = None if is_dm else _FakeGuild(id=7, name="my-guild")
    mentions: list[Any] = []
    if mention_bot is not None:
        mentions.append(_bot_user_for(mention_bot))
    return _FakeMessage(
        id=1,
        author=author,
        channel=channel,
        clean_content=content,
        guild=guild,
        mentions=mentions,
    )


# ---------------------------------------------------------------------------
# Activation / allowlist gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_message_ignored(channel: DiscordChannel) -> None:
    msg = _make_message(sender_id=1, content="hi", is_dm=True, bot=True)
    await channel._on_message(msg)  # type: ignore[arg-type]
    assert msg.replies == []
    channel.handle_message.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_non_allowlisted_dm_first_time_replies_once(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "777")
    from app.channels.discord import _allowed_user_ids

    _allowed_user_ids.cache_clear()

    msg = _make_message(sender_id=1, content="hi", is_dm=True)
    await channel._on_message(msg)  # type: ignore[arg-type]
    assert len(msg.replies) == 1
    assert "Add `1`" in msg.replies[0]
    assert "DISCORD_ALLOWED_USER_IDS" in msg.replies[0]
    assert "1" in channel._notified_disallowed
    channel.handle_message.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_non_allowlisted_dm_repeat_silent(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "777")
    from app.channels.discord import _allowed_user_ids

    _allowed_user_ids.cache_clear()

    msg1 = _make_message(sender_id=1, content="hi", is_dm=True)
    msg2 = _make_message(sender_id=1, content="hi again", is_dm=True)
    await channel._on_message(msg1)  # type: ignore[arg-type]
    await channel._on_message(msg2)  # type: ignore[arg-type]
    assert len(msg1.replies) == 1
    assert msg2.replies == []


@pytest.mark.asyncio
async def test_non_allowlisted_guild_mention_default_dm_scope_dispatches(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default scope is 'dm' — guild mentions bypass the allowlist."""
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "777")
    monkeypatch.delenv("DISCORD_ALLOWLIST_SCOPE", raising=False)
    from app.channels.discord import _allowed_user_ids, _allowlist_scope

    _allowed_user_ids.cache_clear()
    _allowlist_scope.cache_clear()

    msg = _make_message(
        sender_id=1, content="hello", is_dm=False, mention_bot=channel
    )
    await channel._on_message(msg)  # type: ignore[arg-type]
    channel.handle_message.assert_called_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_non_allowlisted_guild_mention_all_scope_silent(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope='all' — allowlist gates guild mentions, but no notice is
    posted to the public channel; the message is silently ignored."""
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "777")
    monkeypatch.setenv("DISCORD_ALLOWLIST_SCOPE", "all")
    from app.channels.discord import _allowed_user_ids, _allowlist_scope

    _allowed_user_ids.cache_clear()
    _allowlist_scope.cache_clear()

    msg = _make_message(
        sender_id=1, content="hi", is_dm=False, mention_bot=channel
    )
    await channel._on_message(msg)  # type: ignore[arg-type]
    assert msg.replies == []
    assert "1" not in channel._notified_disallowed
    channel.handle_message.assert_not_called()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Dispatching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_no_allowlist_dispatches(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    from app.channels.discord import _allowed_user_ids

    _allowed_user_ids.cache_clear()

    msg = _make_message(sender_id=1, content="hi", is_dm=True)
    await channel._on_message(msg)  # type: ignore[arg-type]
    channel.handle_message.assert_called_once()  # type: ignore[attr-defined]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["user_id"] == "1"
    assert kwargs["session_id"] == "42"
    assert kwargs["message"] == "hi"
    origin: Origin = kwargs["origin"]
    assert origin.transport == "discord"
    assert origin.sender_id == "1"
    assert origin.location_id == "42"
    assert origin.location_display == "DM"
    assert msg.replies == ["agent reply"]


@pytest.mark.asyncio
async def test_guild_mention_no_allowlist_dispatches(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    from app.channels.discord import _allowed_user_ids

    _allowed_user_ids.cache_clear()

    msg = _make_message(
        sender_id=1, content="hello", is_dm=False, mention_bot=channel
    )
    await channel._on_message(msg)  # type: ignore[arg-type]
    channel.handle_message.assert_called_once()  # type: ignore[attr-defined]
    origin: Origin = channel.handle_message.call_args.kwargs["origin"]  # type: ignore[attr-defined]
    assert "my-guild" in origin.location_display
    assert "general" in origin.location_display
    assert "id=7" in origin.location_display


@pytest.mark.asyncio
async def test_guild_message_without_mention_skipped(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    from app.channels.discord import _allowed_user_ids

    _allowed_user_ids.cache_clear()

    msg = _make_message(sender_id=1, content="random chatter", is_dm=False)
    await channel._on_message(msg)  # type: ignore[arg-type]
    channel.handle_message.assert_not_called()  # type: ignore[attr-defined]
    assert msg.replies == []


@pytest.mark.asyncio
async def test_mention_stripped_from_prompt(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    from app.channels.discord import _allowed_user_ids

    _allowed_user_ids.cache_clear()

    bot_name = _bot_user_for(channel).display_name
    msg = _make_message(
        sender_id=1,
        content=f"@{bot_name} hello there",
        is_dm=False,
        mention_bot=channel,
    )
    await channel._on_message(msg)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["message"] == "hello there"


@pytest.mark.asyncio
async def test_empty_prompt_after_strip_skipped(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    from app.channels.discord import _allowed_user_ids

    _allowed_user_ids.cache_clear()

    bot_name = _bot_user_for(channel).display_name
    msg = _make_message(
        sender_id=1,
        content=f"@{bot_name}",
        is_dm=False,
        mention_bot=channel,
    )
    await channel._on_message(msg)  # type: ignore[arg-type]
    channel.handle_message.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_agent_failure_replies_apology(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    from app.channels.discord import _allowed_user_ids

    _allowed_user_ids.cache_clear()

    channel.handle_message.side_effect = RuntimeError("boom")  # type: ignore[attr-defined]
    msg = _make_message(sender_id=1, content="hi", is_dm=True)
    await channel._on_message(msg)  # type: ignore[arg-type]
    assert len(msg.replies) == 1
    assert "wrong" in msg.replies[0].lower()
