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
class _FakeReference:
    """Mimic `discord.MessageReference`."""

    message_id: int | None
    resolved: Any | None = None


@dataclass
class _FakeAttachment:
    """Mimic `discord.Attachment`."""

    filename: str
    content_type: str | None
    size: int
    _data: bytes = b""
    _read_raises: BaseException | None = None
    id: int = 0

    async def read(self) -> bytes:
        if self._read_raises is not None:
            raise self._read_raises
        return self._data


@dataclass
class _FakeChannel:
    id: int
    name: str = "general"
    history_messages: list[Any] = field(default_factory=list)
    history_calls: list[dict[str, Any]] = field(default_factory=list)
    history_raises: BaseException | None = None
    sends: list[str] = field(default_factory=list)
    fetched: dict[int, Any] = field(default_factory=dict)
    fetch_calls: list[int] = field(default_factory=list)
    fetch_raises: BaseException | None = None

    def typing(self) -> Any:
        @asynccontextmanager
        async def _cm():
            yield

        return _cm()

    async def send(self, text: str) -> None:
        """Mimic `discord.TextChannel.send` — records each call so tests
        can assert the response went out as a plain channel message
        (no `MessageReference`) rather than a quoted reply."""
        self.sends.append(text)

    async def fetch_message(self, msg_id: int) -> Any:
        """Mimic `discord.TextChannel.fetch_message` — looks up by id in
        `fetched`, or raises `fetch_raises` if set."""
        self.fetch_calls.append(msg_id)
        if self.fetch_raises is not None:
            raise self.fetch_raises
        return self.fetched[msg_id]

    def history(self, *, limit: int, before: Any) -> Any:
        """Mimic `discord.TextChannel.history` — returns an async iterator
        that yields newest-first (matching discord.py semantics)."""
        self.history_calls.append({"limit": limit, "before": before})
        raises = self.history_raises
        msgs = list(self.history_messages)

        async def _gen():
            if raises is not None:
                raise raises
            for m in msgs:
                yield m

        return _gen()


@dataclass
class _FakeMessage:
    id: int
    author: _FakeAuthor
    channel: _FakeChannel
    clean_content: str
    guild: _FakeGuild | None = None
    mentions: list[Any] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)
    reference: _FakeReference | None = None
    attachments: list[_FakeAttachment] = field(default_factory=list)

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
async def test_bot_mention_ignored_by_default(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default `DISCORD_REPLY_TO_BOTS=false` blocks bot-authored guild
    mentions entirely."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.delenv("DISCORD_REPLY_TO_BOTS", raising=False)

    msg = _make_message(
        sender_id=1, content="hi", is_dm=False, bot=True, mention_bot=channel
    )
    await channel._on_message(msg)  # type: ignore[arg-type]
    assert msg.replies == []
    assert msg.channel.sends == []
    channel.handle_message.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_bot_mention_replies_via_channel_send_when_toggle_on(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`DISCORD_REPLY_TO_BOTS=true` opens the gate; with the quote
    toggle off (default), the response goes out via `channel.send`,
    not `message.reply` — no `MessageReference` to ping the other bot."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_REPLY_TO_BOTS", "true")
    monkeypatch.delenv("DISCORD_QUOTE_BOT_REPLIES", raising=False)

    msg = _make_message(
        sender_id=1, content="hi", is_dm=False, bot=True, mention_bot=channel
    )
    await channel._on_message(msg)  # type: ignore[arg-type]
    channel.handle_message.assert_called_once()  # type: ignore[attr-defined]
    assert msg.replies == []
    assert msg.channel.sends == ["agent reply"]


@pytest.mark.asyncio
async def test_bot_mention_with_quote_toggle_uses_message_reply(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`DISCORD_QUOTE_BOT_REPLIES=true` opts back into the quoted
    reply for bot authors."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_REPLY_TO_BOTS", "true")
    monkeypatch.setenv("DISCORD_QUOTE_BOT_REPLIES", "true")

    msg = _make_message(
        sender_id=1, content="hi", is_dm=False, bot=True, mention_bot=channel
    )
    await channel._on_message(msg)  # type: ignore[arg-type]
    channel.handle_message.assert_called_once()  # type: ignore[attr-defined]
    assert msg.replies == ["agent reply"]
    assert msg.channel.sends == []


@pytest.mark.asyncio
async def test_human_mention_always_uses_message_reply(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quote toggle only affects bot authors. Humans always get
    `message.reply(...)` regardless of `DISCORD_QUOTE_BOT_REPLIES`."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_QUOTE_BOT_REPLIES", "false")

    msg = _make_message(
        sender_id=1, content="hi", is_dm=False, mention_bot=channel
    )
    await channel._on_message(msg)  # type: ignore[arg-type]
    channel.handle_message.assert_called_once()  # type: ignore[attr-defined]
    assert msg.replies == ["agent reply"]
    assert msg.channel.sends == []


@pytest.mark.asyncio
async def test_self_bot_always_skipped_even_when_toggle_on(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Self-bot messages never wake the agent, regardless of toggles —
    the self-loop guard is independent of `DISCORD_REPLY_TO_BOTS`."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_REPLY_TO_BOTS", "true")

    self_id = _bot_user_for(channel).id
    msg = _make_message(
        sender_id=self_id,
        content="hi",
        is_dm=False,
        bot=True,
        mention_bot=channel,
    )
    await channel._on_message(msg)  # type: ignore[arg-type]
    assert msg.replies == []
    assert msg.channel.sends == []
    channel.handle_message.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_bot_mention_with_toggle_on_still_respects_allowlist(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with the gate open, `DISCORD_ALLOWLIST_SCOPE=all` plus a
    user-ID allowlist still drops bot-authored mentions whose id isn't
    on the list. No notice is posted to the public channel."""
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "999999")
    monkeypatch.setenv("DISCORD_ALLOWLIST_SCOPE", "all")
    monkeypatch.setenv("DISCORD_REPLY_TO_BOTS", "true")
    from app.channels.discord import _allowed_user_ids, _allowlist_scope

    _allowed_user_ids.cache_clear()
    _allowlist_scope.cache_clear()

    msg = _make_message(
        sender_id=1, content="hi", is_dm=False, bot=True, mention_bot=channel
    )
    await channel._on_message(msg)  # type: ignore[arg-type]
    assert msg.replies == []
    assert msg.channel.sends == []
    channel.handle_message.assert_not_called()  # type: ignore[attr-defined]
    assert "1" not in channel._notified_disallowed


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


# ---------------------------------------------------------------------------
# Context backfill: rolling buffer + seed-once-per-channel via channel.history()
# ---------------------------------------------------------------------------


def _hist_msg(
    msg_id: int,
    sender_id: int,
    sender_name: str,
    content: str,
    *,
    bot: bool = False,
) -> _FakeMessage:
    """Build a `_FakeMessage` shaped like a discord.py history entry."""
    return _FakeMessage(
        id=msg_id,
        author=_FakeAuthor(id=sender_id, display_name=sender_name, bot=bot),
        channel=_FakeChannel(id=42),
        clean_content=content,
        guild=_FakeGuild(id=7, name="my-guild"),
    )


def _make_guild_message(
    *,
    msg_id: int,
    sender_id: int,
    content: str,
    fake_channel: _FakeChannel,
    sender_name: str = "papi",
    bot: bool = False,
    mention_bot: DiscordChannel | None = None,
) -> _FakeMessage:
    """Like `_make_message(is_dm=False)` but reuses a passed-in fake channel
    so we can attach a shared `history_messages` / `history_calls`."""
    author = _FakeAuthor(id=sender_id, display_name=sender_name, bot=bot)
    mentions: list[Any] = []
    if mention_bot is not None:
        mentions.append(_bot_user_for(mention_bot))
    return _FakeMessage(
        id=msg_id,
        author=author,
        channel=fake_channel,
        clean_content=content,
        guild=_FakeGuild(id=7, name="my-guild"),
        mentions=mentions,
    )


@pytest.mark.asyncio
async def test_buffer_records_every_non_bot_guild_message(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even messages that don't trigger the bot get appended to the
    channel's rolling buffer so they're available for the next mention."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    fc = _FakeChannel(id=42)
    msg = _make_guild_message(
        msg_id=1, sender_id=10, content="just chatting", fake_channel=fc
    )
    await channel._on_message(msg)  # type: ignore[arg-type]
    channel.handle_message.assert_not_called()  # type: ignore[attr-defined]
    buf = channel._channel_buffers.get(42)
    assert buf is not None
    assert len(buf) == 1
    assert buf[0].sender_id == "10"
    assert buf[0].text == "just chatting"


@pytest.mark.asyncio
async def test_first_mention_seeds_via_history_api(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First mention in a channel triggers `channel.history(limit=N, before=msg)`,
    replaces the buffer with the API result, and marks the channel seeded."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    fc = _FakeChannel(id=42)
    # discord.py yields newest-first; the channel should reverse to oldest-first.
    fc.history_messages = [
        _hist_msg(202, 333, "bob", "yeah what's up"),
        _hist_msg(201, 222, "alice", "hey is anyone here good with python?"),
    ]
    trigger = _make_guild_message(
        msg_id=300,
        sender_id=111,
        content="hello",
        fake_channel=fc,
        mention_bot=channel,
    )
    await channel._on_message(trigger)  # type: ignore[arg-type]
    assert len(fc.history_calls) == 1
    assert fc.history_calls[0]["limit"] == 20
    assert fc.history_calls[0]["before"] is trigger
    assert 42 in channel._seeded_channels
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    ctx = kwargs["context"]
    assert ctx is not None
    assert [m.text for m in ctx] == [
        "hey is anyone here good with python?",
        "yeah what's up",
    ]


@pytest.mark.asyncio
async def test_subsequent_mention_uses_buffer_no_api(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a channel is seeded, mentions read context from the buffer
    with zero `history()` calls."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    fc = _FakeChannel(id=42)
    # Pre-seed manually to simulate "we've already done the API once".
    channel._seeded_channels.add(42)
    import collections

    channel._channel_buffers[42] = collections.deque(maxlen=20)

    chatter1 = _make_guild_message(
        msg_id=1, sender_id=222, content="msg one", fake_channel=fc,
        sender_name="alice",
    )
    chatter2 = _make_guild_message(
        msg_id=2, sender_id=333, content="msg two", fake_channel=fc,
        sender_name="bob",
    )
    await channel._on_message(chatter1)  # type: ignore[arg-type]
    await channel._on_message(chatter2)  # type: ignore[arg-type]
    trigger = _make_guild_message(
        msg_id=3, sender_id=111, content="follow up", fake_channel=fc,
        mention_bot=channel,
    )
    await channel._on_message(trigger)  # type: ignore[arg-type]

    assert fc.history_calls == []  # buffer warm — no API call
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    ctx = kwargs["context"]
    assert [m.text for m in ctx] == ["msg one", "msg two"]


@pytest.mark.asyncio
async def test_quiet_channel_with_few_messages_seeds_with_what_exists(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a channel that has only 3 lifetime messages must
    seed with 3 and serve from the buffer thereafter — never hit the
    API on every subsequent mention just because the buffer has < N."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    fc = _FakeChannel(id=42)
    fc.history_messages = [
        _hist_msg(102, 222, "alice", "two"),
        _hist_msg(101, 222, "alice", "one"),
    ]  # only 2 in the channel's lifetime, much less than N=20
    trigger1 = _make_guild_message(
        msg_id=200, sender_id=111, content="hi", fake_channel=fc,
        mention_bot=channel,
    )
    await channel._on_message(trigger1)  # type: ignore[arg-type]
    assert len(fc.history_calls) == 1
    trigger2 = _make_guild_message(
        msg_id=201, sender_id=111, content="hi again", fake_channel=fc,
        mention_bot=channel,
    )
    await channel._on_message(trigger2)  # type: ignore[arg-type]
    # No second API call — the channel is already seeded.
    assert len(fc.history_calls) == 1


@pytest.mark.asyncio
async def test_history_fetch_failure_falls_through_silently(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `history()` raises, the agent still gets the mention — just
    without a `[context]` block. The channel must NOT be marked seeded
    so the next mention can retry."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    fc = _FakeChannel(id=42)
    fc.history_raises = RuntimeError("rate limited")
    trigger = _make_guild_message(
        msg_id=1, sender_id=111, content="hi", fake_channel=fc,
        mention_bot=channel,
    )
    await channel._on_message(trigger)  # type: ignore[arg-type]
    channel.handle_message.assert_called_once()  # type: ignore[attr-defined]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["context"] is None
    assert 42 not in channel._seeded_channels


@pytest.mark.asyncio
async def test_dm_never_fetches_history_or_uses_buffer(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DMs already round-trip every message; no `[context]` block needed."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    msg = _make_message(sender_id=1, content="hi", is_dm=True)
    await channel._on_message(msg)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["context"] is None
    assert msg.channel.history_calls == []
    # DM channel id never gets a buffer.
    assert msg.channel.id not in channel._channel_buffers


@pytest.mark.asyncio
async def test_history_lines_zero_disables_buffer_and_api(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_CONTEXT_HISTORY_LINES", "0")
    from app.channels.discord import _history_limit

    _history_limit.cache_clear()

    fc = _FakeChannel(id=42)
    fc.history_messages = [_hist_msg(101, 222, "alice", "one")]
    chatter = _make_guild_message(
        msg_id=1, sender_id=222, content="lurk", fake_channel=fc,
        sender_name="alice",
    )
    trigger = _make_guild_message(
        msg_id=2, sender_id=111, content="hi", fake_channel=fc,
        mention_bot=channel,
    )
    await channel._on_message(chatter)  # type: ignore[arg-type]
    await channel._on_message(trigger)  # type: ignore[arg-type]
    assert fc.history_calls == []
    assert 42 not in channel._channel_buffers
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["context"] is None


@pytest.mark.asyncio
async def test_buffer_excludes_only_self_keeps_other_bots(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Webhooks, bridge bots (PluralKit, IRC relays), and integration
    bots (GitHub, news) are real conversational content. Only our own
    bot's messages get filtered (they're already in the ADK session)."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    self_id = _bot_user_for(channel).id
    fc = _FakeChannel(id=42)
    own_msg = _make_guild_message(
        msg_id=1, sender_id=self_id, content="my own reply",
        fake_channel=fc, sender_name="TestBot", bot=True,
    )
    other_bot = _make_guild_message(
        msg_id=2, sender_id=555, content="webhook announcement",
        fake_channel=fc, sender_name="GitHubBot", bot=True,
    )
    user_msg = _make_guild_message(
        msg_id=3, sender_id=222, content="real user",
        fake_channel=fc, sender_name="alice",
    )
    await channel._on_message(own_msg)  # type: ignore[arg-type]
    await channel._on_message(other_bot)  # type: ignore[arg-type]
    await channel._on_message(user_msg)  # type: ignore[arg-type]
    buf = channel._channel_buffers[42]
    assert [m.text for m in buf] == ["webhook announcement", "real user"]


@pytest.mark.asyncio
async def test_history_seed_excludes_only_self_keeps_other_bots(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    self_id = _bot_user_for(channel).id
    fc = _FakeChannel(id=42)
    fc.history_messages = [
        # discord.py yields newest-first
        _hist_msg(303, 222, "alice", "user message"),
        _hist_msg(302, 555, "GitHubBot", "PR opened", bot=True),
        _hist_msg(301, self_id, "TestBot", "my own old reply", bot=True),
    ]
    trigger = _make_guild_message(
        msg_id=400, sender_id=111, content="status?",
        fake_channel=fc, mention_bot=channel,
    )
    await channel._on_message(trigger)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    ctx = kwargs["context"]
    # Self-bot filtered, other-bot kept, real user kept; oldest-first.
    assert [m.text for m in ctx] == ["PR opened", "user message"]


@pytest.mark.asyncio
async def test_seeded_buffer_excludes_trigger_message(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trigger gets recorded into the buffer at the top of
    `_on_message`; when we build context for it, we must drop the most
    recent entry so the agent doesn't see its own prompt twice."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    fc = _FakeChannel(id=42)
    channel._seeded_channels.add(42)
    import collections

    channel._channel_buffers[42] = collections.deque(maxlen=20)
    chatter = _make_guild_message(
        msg_id=1, sender_id=222, content="lead-up", fake_channel=fc,
        sender_name="alice",
    )
    await channel._on_message(chatter)  # type: ignore[arg-type]
    trigger = _make_guild_message(
        msg_id=2, sender_id=111, content="hello bot", fake_channel=fc,
        mention_bot=channel,
    )
    await channel._on_message(trigger)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    ctx = kwargs["context"]
    assert [m.text for m in ctx] == ["lead-up"]
    assert kwargs["message"] == "hello bot"


# ---------------------------------------------------------------------------
# [reply_to] resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_to_uses_resolved_when_available(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `message.reference.resolved` is set, use it directly — no
    buffer lookup, no API call."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    fc = _FakeChannel(id=42)
    referenced = _FakeMessage(
        id=999,
        author=_FakeAuthor(id=555, display_name="GitHubBot", bot=True),
        channel=fc,
        clean_content="PR #42 opened",
        guild=_FakeGuild(id=7, name="my-guild"),
    )
    trigger = _make_guild_message(
        msg_id=1000, sender_id=111, content="thoughts?",
        fake_channel=fc, mention_bot=channel,
    )
    trigger.reference = _FakeReference(message_id=999, resolved=referenced)

    await channel._on_message(trigger)  # type: ignore[arg-type]
    assert fc.fetch_calls == []
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    rt = kwargs["reply_to"]
    assert rt is not None
    assert rt.sender_id == "555"
    assert rt.sender_display == "GitHubBot"
    assert rt.text == "PR #42 opened"


@pytest.mark.asyncio
async def test_reply_to_falls_back_to_buffer(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `resolved`, the per-channel id index resolves the
    referenced message with zero API calls."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    fc = _FakeChannel(id=42)
    earlier = _make_guild_message(
        msg_id=500, sender_id=222, content="the bug is in foo.py",
        fake_channel=fc, sender_name="alice",
    )
    await channel._on_message(earlier)  # type: ignore[arg-type]

    trigger = _make_guild_message(
        msg_id=501, sender_id=111, content="where exactly?",
        fake_channel=fc, mention_bot=channel,
    )
    trigger.reference = _FakeReference(message_id=500, resolved=None)
    await channel._on_message(trigger)  # type: ignore[arg-type]
    assert fc.fetch_calls == []
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    rt = kwargs["reply_to"]
    assert rt is not None
    assert rt.sender_id == "222"
    assert rt.text == "the bug is in foo.py"


@pytest.mark.asyncio
async def test_reply_to_falls_back_to_fetch_message(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither `resolved` nor the buffer has it → one REST fetch."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    fc = _FakeChannel(id=42)
    fetched = _FakeMessage(
        id=900,
        author=_FakeAuthor(id=222, display_name="alice"),
        channel=fc,
        clean_content="from way back",
        guild=_FakeGuild(id=7, name="my-guild"),
    )
    fc.fetched[900] = fetched

    trigger = _make_guild_message(
        msg_id=901, sender_id=111, content="hmm",
        fake_channel=fc, mention_bot=channel,
    )
    trigger.reference = _FakeReference(message_id=900, resolved=None)
    await channel._on_message(trigger)  # type: ignore[arg-type]

    assert fc.fetch_calls == [900]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    rt = kwargs["reply_to"]
    assert rt is not None
    assert rt.text == "from way back"
    assert rt.sender_id == "222"


@pytest.mark.asyncio
async def test_reply_to_fetch_failure_skipped_silently(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `fetch_message` raises (deleted, rate-limited, etc.) the
    agent still gets the mention with `reply_to=None`."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    fc = _FakeChannel(id=42)
    fc.fetch_raises = RuntimeError("rate limited")

    trigger = _make_guild_message(
        msg_id=1, sender_id=111, content="hmm",
        fake_channel=fc, mention_bot=channel,
    )
    trigger.reference = _FakeReference(message_id=900, resolved=None)
    await channel._on_message(trigger)  # type: ignore[arg-type]
    channel.handle_message.assert_called_once()  # type: ignore[attr-defined]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["reply_to"] is None


@pytest.mark.asyncio
async def test_reply_to_truncated_when_long(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quoted text longer than `REPLY_TEXT_MAX` is truncated with an
    ellipsis to keep the prompt focused."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    from app.channels.discord import REPLY_TEXT_MAX

    fc = _FakeChannel(id=42)
    long_text = "a" * 600
    referenced = _FakeMessage(
        id=999, author=_FakeAuthor(id=222, display_name="alice"),
        channel=fc, clean_content=long_text,
        guild=_FakeGuild(id=7, name="my-guild"),
    )
    trigger = _make_guild_message(
        msg_id=1000, sender_id=111, content="?",
        fake_channel=fc, mention_bot=channel,
    )
    trigger.reference = _FakeReference(message_id=999, resolved=referenced)
    await channel._on_message(trigger)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    rt = kwargs["reply_to"]
    assert rt is not None
    assert len(rt.text) == REPLY_TEXT_MAX
    assert rt.text.endswith("…")


@pytest.mark.asyncio
async def test_no_reference_means_no_reply_to(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain mention without a Discord reply → `reply_to=None`."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    fc = _FakeChannel(id=42)
    trigger = _make_guild_message(
        msg_id=1, sender_id=111, content="hi",
        fake_channel=fc, mention_bot=channel,
    )
    await channel._on_message(trigger)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["reply_to"] is None
    assert fc.fetch_calls == []


@pytest.mark.asyncio
async def test_deleted_referenced_message_skipped(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `DeletedReferencedMessage` resolved value falls through to
    buffer/fetch — and if neither finds it, returns None."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    import discord

    class _StubParent:
        message_id = 999
        channel_id = 42
        guild_id = 7

    deleted = discord.DeletedReferencedMessage(_StubParent())  # type: ignore[arg-type]

    fc = _FakeChannel(id=42)
    # No buffer entry for 999, no fetched entry, fetch will raise
    # (KeyError on dict access) — but we can't rely on that, so set
    # fetch_raises explicitly.
    fc.fetch_raises = RuntimeError("not found")
    trigger = _make_guild_message(
        msg_id=1, sender_id=111, content="hmm",
        fake_channel=fc, mention_bot=channel,
    )
    trigger.reference = _FakeReference(message_id=999, resolved=deleted)
    await channel._on_message(trigger)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["reply_to"] is None


@pytest.mark.asyncio
async def test_reply_to_emits_even_when_context_disabled(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`DISCORD_CONTEXT_HISTORY_LINES=0` disables ambient `[context]` and
    the buffer/index. `[reply_to]` must still resolve via `resolved`
    (the only path that doesn't depend on the index)."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_CONTEXT_HISTORY_LINES", "0")
    from app.channels.discord import _history_limit

    _history_limit.cache_clear()

    fc = _FakeChannel(id=42)
    referenced = _FakeMessage(
        id=999, author=_FakeAuthor(id=222, display_name="alice"),
        channel=fc, clean_content="anchored",
        guild=_FakeGuild(id=7, name="my-guild"),
    )
    trigger = _make_guild_message(
        msg_id=1000, sender_id=111, content="?",
        fake_channel=fc, mention_bot=channel,
    )
    trigger.reference = _FakeReference(message_id=999, resolved=referenced)
    await channel._on_message(trigger)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["context"] is None
    assert kwargs["reply_to"] is not None
    assert kwargs["reply_to"].text == "anchored"


@pytest.mark.asyncio
async def test_reply_to_to_self_bot_still_emits(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user replying to one of OUR own messages must still surface
    `[reply_to]` — that's the disambiguation we want. The `[context]`
    self-filter does NOT apply to `[reply_to]`."""
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    self_id = _bot_user_for(channel).id
    fc = _FakeChannel(id=42)
    bot_msg = _FakeMessage(
        id=999, author=_FakeAuthor(id=self_id, display_name="TestBot", bot=True),
        channel=fc, clean_content="my earlier reply",
        guild=_FakeGuild(id=7, name="my-guild"),
    )
    trigger = _make_guild_message(
        msg_id=1000, sender_id=111, content="why?",
        fake_channel=fc, mention_bot=channel,
    )
    trigger.reference = _FakeReference(message_id=999, resolved=bot_msg)
    await channel._on_message(trigger)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    rt = kwargs["reply_to"]
    assert rt is not None
    assert rt.sender_id == str(self_id)
    assert rt.text == "my earlier reply"


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def _make_dm_with_attachments(
    attachments: list[_FakeAttachment], content: str = "look at this"
) -> _FakeMessage:
    return _FakeMessage(
        id=1,
        author=_FakeAuthor(id=1, display_name="papi"),
        channel=_FakeChannel(id=42),
        clean_content=content,
        attachments=attachments,
    )


@pytest.mark.asyncio
async def test_attachment_image_becomes_inline_data(
    channel: DiscordChannel,
) -> None:
    img = _FakeAttachment(
        filename="screenshot.png",
        content_type="image/png",
        size=1024,
        _data=b"\x89PNG" + b"\x00" * 1020,
    )
    msg = _make_dm_with_attachments([img])
    await channel._on_message(msg)  # type: ignore[arg-type]
    channel.handle_message.assert_called_once()  # type: ignore[attr-defined]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    parts = kwargs["attachments"]
    assert len(parts) == 1
    assert parts[0].inline_data.mime_type == "image/png"
    assert parts[0].inline_data.data == img._data
    assert kwargs["attachments_skipped"] == []


@pytest.mark.asyncio
async def test_attachment_unsupported_mime_skipped(
    channel: DiscordChannel,
) -> None:
    docx = _FakeAttachment(
        filename="report.docx",
        content_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        size=20_000,
        _data=b"PK" + b"\x00" * 19_998,
    )
    msg = _make_dm_with_attachments([docx])
    await channel._on_message(msg)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["attachments"] == []
    skipped = kwargs["attachments_skipped"]
    assert len(skipped) == 1
    assert skipped[0].filename == "report.docx"
    assert "unsupported" in skipped[0].reason


@pytest.mark.asyncio
async def test_attachment_too_large_skipped(channel: DiscordChannel) -> None:
    huge = _FakeAttachment(
        filename="big.png",
        content_type="image/png",
        size=50_000_000,
        _data=b"",  # `_data` not used because size check fires first
    )
    msg = _make_dm_with_attachments([huge])
    await channel._on_message(msg)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["attachments"] == []
    skipped = kwargs["attachments_skipped"]
    assert len(skipped) == 1
    assert "per-attachment cap" in skipped[0].reason


@pytest.mark.asyncio
async def test_attachments_total_cap_greedy_fill(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three 8-MB images with 18 MB total cap → first two pass,
    third skipped."""
    from app.channels.discord import (
        _attachment_max_bytes,
        _attachments_max_total_bytes,
    )

    monkeypatch.setenv("DISCORD_ATTACHMENT_MAX_BYTES", "10000000")
    monkeypatch.setenv("DISCORD_ATTACHMENTS_MAX_TOTAL_BYTES", "18000000")
    _attachment_max_bytes.cache_clear()
    _attachments_max_total_bytes.cache_clear()

    eight_mb = b"x" * 8_000_000
    imgs = [
        _FakeAttachment(
            filename=f"img{i}.png",
            content_type="image/png",
            size=len(eight_mb),
            _data=eight_mb,
        )
        for i in range(3)
    ]
    msg = _make_dm_with_attachments(imgs)
    await channel._on_message(msg)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert len(kwargs["attachments"]) == 2
    skipped = kwargs["attachments_skipped"]
    assert len(skipped) == 1
    assert skipped[0].filename == "img2.png"
    assert "total cap" in skipped[0].reason


@pytest.mark.asyncio
async def test_attachment_text_inlined_into_prompt(
    channel: DiscordChannel,
) -> None:
    log = _FakeAttachment(
        filename="error.log",
        content_type="text/plain",
        size=20,
        _data=b"NullPointerException at line 42",
    )
    log.size = len(log._data)
    msg = _make_dm_with_attachments([log], content="what's wrong?")
    await channel._on_message(msg)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["attachments"] == []
    assert kwargs["attachments_skipped"] == []
    prompt = kwargs["message"]
    assert '[attachment_text filename="error.log"]' in prompt
    assert "NullPointerException at line 42" in prompt
    assert "[/attachment_text]" in prompt
    assert prompt.endswith("what's wrong?")


@pytest.mark.asyncio
async def test_attachment_text_too_large_skipped(
    channel: DiscordChannel,
) -> None:
    big_log = _FakeAttachment(
        filename="huge.log",
        content_type="text/plain",
        size=200_000,
        _data=b"x" * 200_000,
    )
    msg = _make_dm_with_attachments([big_log])
    await channel._on_message(msg)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["attachments"] == []
    assert "[attachment_text" not in kwargs["message"]
    skipped = kwargs["attachments_skipped"]
    assert len(skipped) == 1
    assert "text too large" in skipped[0].reason


@pytest.mark.asyncio
async def test_attachment_download_failure_reported(
    channel: DiscordChannel,
) -> None:
    img = _FakeAttachment(
        filename="screenshot.png",
        content_type="image/png",
        size=1024,
        _read_raises=RuntimeError("network drop"),
    )
    msg = _make_dm_with_attachments([img])
    await channel._on_message(msg)  # type: ignore[arg-type]
    channel.handle_message.assert_called_once()  # type: ignore[attr-defined]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["attachments"] == []
    skipped = kwargs["attachments_skipped"]
    assert len(skipped) == 1
    assert "download failed" in skipped[0].reason


@pytest.mark.asyncio
async def test_attachment_only_message_still_dispatches(
    channel: DiscordChannel,
) -> None:
    """Image with no text body should still trigger the agent."""
    img = _FakeAttachment(
        filename="screenshot.png",
        content_type="image/png",
        size=10,
        _data=b"\x89PNG\x00\x00\x00\x00\x00\x00",
    )
    msg = _FakeMessage(
        id=1,
        author=_FakeAuthor(id=1, display_name="papi"),
        channel=_FakeChannel(id=42),
        clean_content="",  # no text
        attachments=[img],
    )
    await channel._on_message(msg)  # type: ignore[arg-type]
    channel.handle_message.assert_called_once()  # type: ignore[attr-defined]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert len(kwargs["attachments"]) == 1
    assert kwargs["message"] == ""


@pytest.mark.asyncio
async def test_no_attachments_no_extra_block(
    channel: DiscordChannel,
) -> None:
    msg = _make_message(sender_id=1, content="hi", is_dm=True)
    await channel._on_message(msg)  # type: ignore[arg-type]
    kwargs = channel.handle_message.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["attachments"] == []
    assert kwargs["attachments_skipped"] == []
