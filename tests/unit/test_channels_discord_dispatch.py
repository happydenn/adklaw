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
    history_messages: list[Any] = field(default_factory=list)
    history_calls: list[dict[str, Any]] = field(default_factory=list)
    history_raises: BaseException | None = None
    sends: list[str] = field(default_factory=list)

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
