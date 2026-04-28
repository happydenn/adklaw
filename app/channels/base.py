"""Shared base class for transport channels.

Each channel (Discord, Slack, future Telegram, ...) inherits from
`ChannelBase` and only implements the transport-specific glue: receiving
messages from its SDK, mapping the SDK's user/conversation identifiers
to ADK `(user_id, session_id)` pairs, and posting responses back. The
ADK invocation itself (Runner, session management, event collection) is
handled here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from google.adk.apps import App
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.adk.utils.context_utils import Aclosing
from google.genai import types

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Origin:
    """Stable, transport-agnostic description of where a message came from.

    `transport`, `sender_id`, and `location_id` are mandatory — every
    sensible chat transport exposes at least an opaque user identifier
    and an opaque conversation/channel identifier. Display names are
    optional because some transports (SMS, raw Telegram private chats,
    WhatsApp Business) don't expose a separate human-readable name.
    Channels for those leave the displays as `None`; the prelude
    formatter degrades to bare `id=…` lines.
    """

    transport: str
    sender_id: str
    location_id: str
    sender_display: str | None = None
    location_display: str | None = None


@dataclass(frozen=True)
class ContextMessage:
    """One prior message in a channel, used to backfill conversational context.

    Channels collect these from their transport — Discord pulls them
    from `channel.history()` or its in-memory buffer; future Slack /
    Telegram channels would do the analogous thing — and pass them
    through to the agent so the bot can reason about lead-up chatter
    it didn't directly receive.
    """

    sender_id: str
    text: str
    sender_display: str | None = None


@dataclass(frozen=True)
class DroppedAttachment:
    """An attachment the channel saw but couldn't forward to the model.

    Surfaced to the agent as an `[attachments_skipped]` block so it can
    tell the user what was filtered (and why) instead of pretending
    nothing was attached.
    """

    filename: str
    mime: str | None
    size: int
    reason: str


def _id_label(display: str | None, id_: str) -> str:
    return f"{display} (id={id_})" if display else f"id={id_}"


# Wire format. The agent-side explanation of these blocks lives with
# the channel that emits them (e.g. `DISCORD_CHANNEL_INSTRUCTION` in
# `app/channels/discord.py`), passed into `build_app(extra_instruction=...)`
# at channel startup. Update both when adding or renaming an envelope.


def _format_origin(o: Origin) -> str:
    return (
        "[origin]\n"
        f"transport: {o.transport}\n"
        f"sender: {_id_label(o.sender_display, o.sender_id)}\n"
        f"location: {_id_label(o.location_display, o.location_id)}\n"
        "[/origin]\n\n"
    )


def _format_context(messages: list[ContextMessage]) -> str:
    if not messages:
        return ""
    lines = ["[context] (recent messages, oldest first)"]
    for m in messages:
        lines.append(f"{_id_label(m.sender_display, m.sender_id)}: {m.text}")
    lines.append("[/context]\n")
    return "\n".join(lines) + "\n"


def _format_reply_to(m: ContextMessage) -> str:
    return (
        "[reply_to] (the user is replying to this specific message)\n"
        f"{_id_label(m.sender_display, m.sender_id)}: {m.text}\n"
        "[/reply_to]\n\n"
    )


def _format_attachments_skipped(items: Sequence[DroppedAttachment]) -> str:
    if not items:
        return ""
    lines = ["[attachments_skipped]"]
    for it in items:
        size_mb = it.size / 1_000_000 if it.size else 0
        mime = it.mime or "?"
        lines.append(
            f"- {it.filename} ({mime}, {size_mb:.1f} MB) — {it.reason}"
        )
    lines.append("[/attachments_skipped]\n")
    return "\n".join(lines) + "\n"


class ChannelBase:
    """Base class for channel adapters.

    Subclasses construct a `ChannelBase` with the agent's `App` and a
    `BaseSessionService`, then call `handle_message()` whenever a message
    arrives on their transport.

    Concurrent messages targeting the **same** `session_id` are
    serialized with an in-process `asyncio.Lock`. ADK's session services
    use optimistic concurrency on `last_update_time`, so two overlapping
    `runner.run_async()` invocations against one session would race and
    the second would fail with a stale-session error. Per-session
    serialization avoids that without blocking unrelated conversations.
    """

    def __init__(
        self,
        app: App | None = None,
        session_service: BaseSessionService | None = None,
        *,
        extra_tools: Sequence[Any] = (),
        extra_instruction: str = "",
    ):
        if session_service is None:
            raise TypeError(
                "ChannelBase: session_service is required"
            )
        if app is None:
            # Lazy import to avoid pulling Vertex deps when subclasses
            # are imported in test contexts that stub the runner.
            from app.agent import build_app

            app = build_app(
                extra_tools=extra_tools,
                extra_instruction=extra_instruction,
            )
        elif extra_tools or extra_instruction:
            raise ValueError(
                "ChannelBase: pass either an explicit App or "
                "extra_tools/extra_instruction, not both."
            )
        self._app = app
        self._session_service = session_service
        self._runner = Runner(
            app=app,
            session_service=session_service,
            auto_create_session=True,
        )
        # session_id -> Lock. All channel work runs on a single asyncio
        # loop, so a plain dict is safe. Entries are not evicted; for a
        # personal bot the working set is small. Add an LRU if the
        # process ever serves thousands of distinct sessions.
        self._session_locks: dict[str, asyncio.Lock] = {}

    @property
    def runner(self) -> Runner:
        """Exposed for advanced subclasses that want to stream events."""
        return self._runner

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    async def handle_message(
        self,
        *,
        user_id: str,
        session_id: str,
        message: str,
        origin: Origin | None = None,
        reply_to: ContextMessage | None = None,
        context: list[ContextMessage] | None = None,
        attachments: Sequence[types.Part] = (),
        attachments_skipped: Sequence[DroppedAttachment] = (),
    ) -> str:
        """Run one turn of the agent and return its assistant text.

        Args:
            user_id: ADK user_id. Channels pass their native user
                identifier (e.g. Discord `author.id` as a string).
            session_id: ADK session_id. Channels pass whatever they
                consider a conversation boundary (e.g. Discord channel
                id, Slack thread ts).
            message: The user's plain-text message.
            origin: Optional structured description of where the
                message came from. When provided, an `[origin]…[/origin]`
                prelude is prepended to the user content so the agent
                can identify the sender and location. Channels build
                this; CLI / tests can omit it.
            reply_to: Optional single prior message the user is
                explicitly replying to (Discord's reply feature, etc.).
                Rendered as a `[reply_to]…[/reply_to]` block between
                origin and context. The agent should anchor its
                response on this message when present.
            context: Optional list of prior messages from the same
                location, oldest-first, used to backfill conversational
                context the agent didn't directly receive. Rendered as
                a `[context]…[/context]` block after `[reply_to]` and
                before the user message.
            attachments: Multimodal `Part`s (images, audio, video,
                PDF, etc.) the channel downloaded and prepared for
                the model. Appended after the text Part on the user
                message's `Content`.
            attachments_skipped: Attachments the channel saw but
                couldn't forward (unsupported mime, too large, etc.).
                Rendered as an `[attachments_skipped]` block in the
                text prefix so the agent can tell the user what was
                dropped.

        Returns:
            The agent's final assistant text. Tool calls and partial
            streaming events are collected internally and excluded from
            the returned string.
        """
        prefix = (
            (_format_origin(origin) if origin else "")
            + (_format_reply_to(reply_to) if reply_to else "")
            + (_format_context(context) if context else "")
            + _format_attachments_skipped(attachments_skipped)
        )
        text = prefix + message
        parts: list[types.Part] = [types.Part(text=text)]
        parts.extend(attachments)
        new_message = types.Content(role="user", parts=parts)
        chunks: list[str] = []
        async with self._lock_for(session_id):
            async with Aclosing(
                self._runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=new_message,
                )
            ) as events:
                async for event in events:
                    text = _final_text(event)
                    if text:
                        chunks.append(text)
        return "".join(chunks).strip()


def _final_text(event: Event) -> str:
    """Pull plain text out of an event if it is a final assistant response.

    Returns an empty string for tool calls, tool responses, partial
    streaming chunks, and code-execution results — those should not be
    surfaced verbatim to channel users.
    """
    if not event.is_final_response():
        return ""
    if not event.content or not event.content.parts:
        return ""
    parts: list[str] = []
    for part in event.content.parts:
        if part.text:
            parts.append(part.text)
    return "".join(parts)
