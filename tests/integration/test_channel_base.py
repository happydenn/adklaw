"""Integration tests for `ChannelBase.handle_message`.

These exercise the runner-event-collection loop and the per-session
serialization without spending real LLM tokens — the `Runner` is replaced
with a stub that yields synthetic ADK events. This is what verifies that
the lock map actually serializes overlapping work, and that the
`[origin]` block is prepended to the user content.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.events import Event
from google.genai import types

from app.channels.base import ChannelBase, ContextMessage, Origin


def _final_text_event(text: str) -> Event:
    """Build an ADK Event that looks like a final assistant response."""
    return Event(
        author="root_agent",
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


class _RecordingRunner:
    """Stub Runner that yields a fixed event stream and records call args."""

    def __init__(self, *, events: list[Event] | None = None) -> None:
        self._events = events or [_final_text_event("hello")]
        self.calls: list[dict[str, Any]] = []

    def run_async(
        self, *, user_id: str, session_id: str, new_message: types.Content
    ) -> AsyncIterator[Event]:
        self.calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "new_message": new_message,
            }
        )
        events = self._events

        async def _gen() -> AsyncIterator[Event]:
            for e in events:
                yield e

        return _gen()


def _make_channel(runner: Any) -> ChannelBase:
    ch = ChannelBase.__new__(ChannelBase)
    ch._app = MagicMock()
    ch._session_service = MagicMock()
    ch._runner = runner
    ch._session_locks = {}
    return ch


@pytest.mark.asyncio
async def test_handle_message_runs_runner_returns_string() -> None:
    runner = _RecordingRunner(events=[_final_text_event("hello")])
    ch = _make_channel(runner)
    out = await ch.handle_message(
        user_id="u1", session_id="s1", message="hi"
    )
    assert out == "hello"
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_handle_message_origin_appears_in_user_content() -> None:
    runner = _RecordingRunner()
    ch = _make_channel(runner)
    origin = Origin(
        transport="discord",
        sender_id="111",
        location_id="222",
        sender_display="papi",
        location_display="DM",
    )
    await ch.handle_message(
        user_id="111", session_id="222", message="hi there", origin=origin
    )
    sent = runner.calls[0]["new_message"]
    text = sent.parts[0].text
    assert text.startswith("[origin]\n")
    assert "[/origin]\n\n" in text
    assert text.endswith("hi there")
    assert "papi (id=111)" in text


@pytest.mark.asyncio
async def test_handle_message_context_block_appended_after_origin() -> None:
    runner = _RecordingRunner()
    ch = _make_channel(runner)
    origin = Origin(
        transport="discord",
        sender_id="111",
        location_id="42",
        sender_display="papi",
        location_display="guild 'g' (id=7) / channel #general",
    )
    context = [
        ContextMessage(
            sender_id="222", sender_display="alice", text="lead-up one"
        ),
        ContextMessage(
            sender_id="333", sender_display="bob", text="lead-up two"
        ),
    ]
    await ch.handle_message(
        user_id="111",
        session_id="42",
        message="any tips?",
        origin=origin,
        context=context,
    )
    text = runner.calls[0]["new_message"].parts[0].text
    origin_idx = text.index("[origin]")
    context_idx = text.index("[context]")
    user_idx = text.index("any tips?")
    assert origin_idx < context_idx < user_idx
    assert "alice (id=222): lead-up one" in text
    assert "bob (id=333): lead-up two" in text
    assert "[/context]" in text


@pytest.mark.asyncio
async def test_handle_message_no_context_block_when_omitted() -> None:
    runner = _RecordingRunner()
    ch = _make_channel(runner)
    await ch.handle_message(
        user_id="u", session_id="s", message="hi"
    )
    text = runner.calls[0]["new_message"].parts[0].text
    assert "[context]" not in text
    assert text == "hi"


@pytest.mark.asyncio
async def test_handle_message_serializes_same_session() -> None:
    """Three concurrent calls on one session_id must run one-at-a-time."""
    inflight = 0
    max_inflight = 0
    barrier = asyncio.Event()

    class _SerialRunner:
        def run_async(
            self,
            *,
            user_id: str,
            session_id: str,
            new_message: types.Content,
        ) -> AsyncIterator[Event]:
            async def _gen() -> AsyncIterator[Event]:
                nonlocal inflight, max_inflight
                inflight += 1
                max_inflight = max(max_inflight, inflight)
                # Yield the loop so other tasks get a chance to enter.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                inflight -= 1
                yield _final_text_event("ok")

            return _gen()

    ch = _make_channel(_SerialRunner())
    barrier.set()
    await asyncio.gather(
        ch.handle_message(user_id="u", session_id="same", message="a"),
        ch.handle_message(user_id="u", session_id="same", message="b"),
        ch.handle_message(user_id="u", session_id="same", message="c"),
    )
    assert max_inflight == 1


@pytest.mark.asyncio
async def test_handle_message_parallelizes_different_sessions() -> None:
    """Three concurrent calls on different session_ids may overlap."""
    inflight = 0
    max_inflight = 0
    enter_event = asyncio.Event()
    enter_count = 0

    class _ParallelRunner:
        def run_async(
            self,
            *,
            user_id: str,
            session_id: str,
            new_message: types.Content,
        ) -> AsyncIterator[Event]:
            async def _gen() -> AsyncIterator[Event]:
                nonlocal inflight, max_inflight, enter_count
                inflight += 1
                max_inflight = max(max_inflight, inflight)
                enter_count += 1
                if enter_count >= 3:
                    enter_event.set()
                # Block until all three have entered concurrently.
                await asyncio.wait_for(enter_event.wait(), timeout=2.0)
                inflight -= 1
                yield _final_text_event("ok")

            return _gen()

    ch = _make_channel(_ParallelRunner())
    await asyncio.gather(
        ch.handle_message(user_id="u", session_id="s1", message="a"),
        ch.handle_message(user_id="u", session_id="s2", message="b"),
        ch.handle_message(user_id="u", session_id="s3", message="c"),
    )
    assert max_inflight == 3
