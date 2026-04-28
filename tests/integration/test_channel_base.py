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

from app.channels.base import ChannelBase, ContextMessage, DroppedAttachment, Origin


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
async def test_handle_message_orders_origin_reply_to_context() -> None:
    runner = _RecordingRunner()
    ch = _make_channel(runner)
    origin = Origin(
        transport="discord",
        sender_id="111",
        location_id="42",
        sender_display="papi",
        location_display="guild 'g' (id=7) / channel #general",
    )
    reply_to = ContextMessage(
        sender_id="555", sender_display="GitHubBot", text="PR #42 opened"
    )
    context = [
        ContextMessage(
            sender_id="222", sender_display="alice", text="lead-up one"
        ),
    ]
    await ch.handle_message(
        user_id="111",
        session_id="42",
        message="thoughts?",
        origin=origin,
        reply_to=reply_to,
        context=context,
    )
    text = runner.calls[0]["new_message"].parts[0].text
    origin_idx = text.index("[origin]")
    reply_idx = text.index("[reply_to]")
    context_idx = text.index("[context]")
    user_idx = text.index("thoughts?")
    assert origin_idx < reply_idx < context_idx < user_idx
    assert "GitHubBot (id=555): PR #42 opened" in text
    assert "[/reply_to]" in text


@pytest.mark.asyncio
async def test_handle_message_reply_to_alone_emits_block_no_context() -> None:
    runner = _RecordingRunner()
    ch = _make_channel(runner)
    reply_to = ContextMessage(
        sender_id="555", sender_display="alice", text="the bug is in foo.py"
    )
    await ch.handle_message(
        user_id="u",
        session_id="s",
        message="where exactly?",
        reply_to=reply_to,
    )
    text = runner.calls[0]["new_message"].parts[0].text
    assert "[reply_to]" in text
    assert "alice (id=555): the bug is in foo.py" in text
    assert "[context]" not in text
    assert text.endswith("where exactly?")


@pytest.mark.asyncio
async def test_handle_message_omits_reply_to_when_none() -> None:
    runner = _RecordingRunner()
    ch = _make_channel(runner)
    context = [
        ContextMessage(sender_id="222", sender_display="alice", text="hi")
    ]
    await ch.handle_message(
        user_id="u", session_id="s", message="yo", context=context
    )
    text = runner.calls[0]["new_message"].parts[0].text
    assert "[reply_to]" not in text
    assert "[context]" in text


@pytest.mark.asyncio
async def test_handle_message_includes_attachment_parts() -> None:
    """Attachments ride alongside the text Part on the user Content;
    text is always first, attachment Parts come after."""
    runner = _RecordingRunner()
    ch = _make_channel(runner)
    img_part = types.Part(
        inline_data=types.Blob(data=b"\x89PNG...", mime_type="image/png")
    )
    pdf_part = types.Part(
        inline_data=types.Blob(data=b"%PDF...", mime_type="application/pdf")
    )
    await ch.handle_message(
        user_id="u",
        session_id="s",
        message="what is this?",
        attachments=[img_part, pdf_part],
    )
    sent = runner.calls[0]["new_message"]
    assert len(sent.parts) == 3
    assert sent.parts[0].text == "what is this?"
    assert sent.parts[1].inline_data.mime_type == "image/png"
    assert sent.parts[2].inline_data.mime_type == "application/pdf"


@pytest.mark.asyncio
async def test_handle_message_renders_skipped_block() -> None:
    """`attachments_skipped` items render an `[attachments_skipped]`
    block in the user text prefix so the agent can tell the user
    what was dropped."""
    runner = _RecordingRunner()
    ch = _make_channel(runner)
    skipped = [
        DroppedAttachment(
            filename="report.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size=1_500_000,
            reason="unsupported type",
        ),
    ]
    await ch.handle_message(
        user_id="u",
        session_id="s",
        message="take a look",
        attachments_skipped=skipped,
    )
    text = runner.calls[0]["new_message"].parts[0].text
    assert "[attachments_skipped]" in text
    assert "report.docx" in text
    assert "unsupported type" in text
    assert text.endswith("take a look")


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
async def test_channel_base_with_extras_builds_app(
    workspace_dir: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructing a ChannelBase with `app=None` + `extra_instruction`
    builds an App via `build_app(...)` whose agent instruction includes
    the extra. This is the channel-extension seam Discord uses."""
    # Patch out Runner so we don't dial the model; we only need the
    # app construction path.
    from app.channels import base as base_module

    captured: dict[str, Any] = {}

    class _StubRunner:
        def __init__(self, *, app: Any, session_service: Any, auto_create_session: bool):
            captured["app"] = app

    monkeypatch.setattr(base_module, "Runner", _StubRunner)

    ch = base_module.ChannelBase(
        session_service=MagicMock(),
        extra_instruction="MARKER-FROM-CHANNEL",
    )
    from google.adk.agents.readonly_context import ReadonlyContext

    rendered = ch._app.root_agent.instruction(MagicMock(spec=ReadonlyContext))
    assert "MARKER-FROM-CHANNEL" in rendered


def test_channel_base_rejects_app_plus_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels import base as base_module

    monkeypatch.setattr(base_module, "Runner", _RecordingRunner)

    with pytest.raises(ValueError, match="not both"):
        base_module.ChannelBase(
            app=MagicMock(),
            session_service=MagicMock(),
            extra_instruction="x",
        )


def test_channel_base_requires_session_service() -> None:
    from app.channels import base as base_module

    with pytest.raises(TypeError, match="session_service"):
        base_module.ChannelBase(app=MagicMock())


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
