# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

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

from google.adk.apps import App
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.adk.utils.context_utils import Aclosing
from google.genai import types

logger = logging.getLogger(__name__)


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

    def __init__(self, app: App, session_service: BaseSessionService):
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
    ) -> str:
        """Run one turn of the agent and return its assistant text.

        Args:
            user_id: ADK user_id. Channels pass their native user
                identifier (e.g. Discord `author.id` as a string).
            session_id: ADK session_id. Channels pass whatever they
                consider a conversation boundary (e.g. Discord channel
                id, Slack thread ts).
            message: The user's plain-text message.

        Returns:
            The agent's final assistant text. Tool calls and partial
            streaming events are collected internally and excluded from
            the returned string.
        """
        new_message = types.Content(role="user", parts=[types.Part(text=message)])
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
