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
from google.adk.events import Event, EventActions
from google.genai import types

from app.channels.base import (
    ChannelBase,
    ContextMessage,
    DroppedAttachment,
    Origin,
)


def _final_text_event(text: str) -> Event:
    """Build an ADK Event that looks like a final assistant response."""
    return Event(
        author="root_agent",
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


class _RecordingRunner:
    """Stub Runner that yields a fixed event stream and records call args."""

    def __init__(
        self,
        *,
        events: list[Event] | None = None,
        app: Any = None,
        session_service: Any = None,
        artifact_service: Any = None,
        auto_create_session: bool = True,
    ) -> None:
        self._events = events or [_final_text_event("hello")]
        self.calls: list[dict[str, Any]] = []
        self.app = app
        self.session_service = session_service
        self.artifact_service = artifact_service

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


def _make_channel(
    runner: Any, artifact_service: Any | None = None
) -> ChannelBase:
    ch = ChannelBase.__new__(ChannelBase)
    ch._app = MagicMock()
    ch._app.name = "test-app"
    ch._session_service = MagicMock()
    ch._artifact_service = artifact_service or MagicMock()
    ch._runner = runner
    ch._session_locks = {}
    return ch


@pytest.mark.asyncio
async def test_handle_message_runs_runner_returns_agent_reply() -> None:
    runner = _RecordingRunner(events=[_final_text_event("hello")])
    ch = _make_channel(runner)
    out = await ch.handle_message(
        user_id="u1", session_id="s1", message="hi"
    )
    assert out.text == "hello"
    assert out.files == ()
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


def _file_event(filename: str, mime: str, data: bytes) -> Event:
    """Build a final-response Event whose content has an `inline_data`
    Part — what the model emits when it produces a binary asset."""
    return Event(
        author="root_agent",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    inline_data=types.Blob(data=data, mime_type=mime),
                ),
            ],
        ),
    )


def _artifact_event(filename: str, version: int) -> Event:
    """Build a non-final event that signals a tool saved an artifact."""
    return Event(
        author="root_agent",
        actions=EventActions(artifact_delta={filename: version}),
    )


class _StubArtifactService:
    """Minimal `BaseArtifactService` for tests. `entries[(filename,
    version)] = bytes` provides the bytes returned by load_artifact."""

    def __init__(self, entries: dict[tuple[str, int], tuple[bytes, str]]) -> None:
        self._entries = entries
        self.calls: list[dict[str, Any]] = []

    async def load_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        filename: str,
        version: int | None = None,
    ) -> Any:
        self.calls.append(
            {
                "app_name": app_name,
                "user_id": user_id,
                "session_id": session_id,
                "filename": filename,
                "version": version,
            }
        )
        key = (filename, version or 0)
        if key not in self._entries:
            return None
        data, mime = self._entries[key]
        return types.Part(inline_data=types.Blob(data=data, mime_type=mime))


@pytest.mark.asyncio
async def test_handle_message_collects_inline_data_parts() -> None:
    """Inline `Part(inline_data=...)` on a final event surfaces as
    an `OutboundFile` with a synthesized filename."""
    runner = _RecordingRunner(
        events=[
            _final_text_event("here you go: "),
            _file_event("ignored", "image/png", b"\x89PNG-bytes"),
        ]
    )
    ch = _make_channel(runner)
    reply = await ch.handle_message(user_id="u", session_id="s", message="draw a duck")
    assert reply.text == "here you go:"
    assert len(reply.files) == 1
    f = reply.files[0]
    assert f.data == b"\x89PNG-bytes"
    assert f.mime == "image/png"
    assert f.filename.startswith("agent_")
    assert f.filename.endswith(".png")


@pytest.mark.asyncio
async def test_handle_message_collects_artifacts_from_delta() -> None:
    """When a tool saves an artifact, the delta entry on the event
    drives a `load_artifact` call against the registered service
    and the bytes surface as an `OutboundFile`."""
    artifacts = _StubArtifactService(
        entries={("chart.png", 1): (b"chart-bytes", "image/png")}
    )
    runner = _RecordingRunner(
        events=[
            _artifact_event("chart.png", 1),
            _final_text_event("done"),
        ]
    )
    ch = _make_channel(runner, artifact_service=artifacts)
    reply = await ch.handle_message(user_id="u", session_id="s", message="render")
    assert reply.text == "done"
    assert len(reply.files) == 1
    assert reply.files[0].filename == "chart.png"
    assert reply.files[0].data == b"chart-bytes"
    assert reply.files[0].mime == "image/png"
    # And the service got called with the right (app, user, session, filename, version).
    assert artifacts.calls == [
        {
            "app_name": "test-app",
            "user_id": "u",
            "session_id": "s",
            "filename": "chart.png",
            "version": 1,
        }
    ]


@pytest.mark.asyncio
async def test_handle_message_dedupes_inline_and_artifact_by_name() -> None:
    """If an inline Part and an artifact share a sanitized filename,
    keep the inline copy and skip the artifact load — same byte
    payload, no point delivering twice."""
    artifacts = _StubArtifactService(
        entries={("agent_0.png", 1): (b"artifact-bytes", "image/png")}
    )
    runner = _RecordingRunner(
        events=[
            _file_event("inline", "image/png", b"inline-bytes"),
            _artifact_event("agent_0.png", 1),
        ]
    )
    ch = _make_channel(runner, artifact_service=artifacts)
    reply = await ch.handle_message(user_id="u", session_id="s", message="x")
    assert len(reply.files) == 1
    assert reply.files[0].data == b"inline-bytes"


@pytest.mark.asyncio
async def test_handle_message_skips_underscore_prefixed_artifacts() -> None:
    """Underscore-prefixed artifacts are internal (e.g. `web_fetch`
    byte caches the model needs to read but the user shouldn't
    receive). They must not surface as `AgentReply.files`, and we
    shouldn't even waste a `load_artifact` call on them."""
    artifacts = _StubArtifactService(
        entries={
            ("report.pdf", 1): (b"keep-me", "application/pdf"),
            ("_fetched_x.png", 1): (b"hide-me", "image/png"),
        }
    )
    runner = _RecordingRunner(
        events=[
            _final_text_event("done"),
            _artifact_event("_fetched_x.png", 1),
            _artifact_event("report.pdf", 1),
        ]
    )
    ch = _make_channel(runner, artifact_service=artifacts)
    reply = await ch.handle_message(user_id="u", session_id="s", message="x")
    assert len(reply.files) == 1
    assert reply.files[0].filename == "report.pdf"
    # Internal artifact never read.
    fetched_calls = [
        c for c in artifacts.calls if c["filename"] == "_fetched_x.png"
    ]
    assert fetched_calls == []


@pytest.mark.asyncio
async def test_handle_message_round_trips_save_artifact_via_real_service() -> None:
    """End-to-end through the real `InMemoryArtifactService`: a tool
    pre-saves bytes, the runner emits an `artifact_delta` event,
    `handle_message` re-loads via the service and packs them into
    `AgentReply.files`. Catches drift with the actual ADK contract
    that the stub-based tests don't."""
    from google.adk.artifacts import InMemoryArtifactService

    service = InMemoryArtifactService()
    # Pre-save what a tool would have written mid-run.
    version = await service.save_artifact(
        app_name="test-app",
        user_id="u",
        session_id="s",
        filename="report.pdf",
        artifact=types.Part(
            inline_data=types.Blob(
                data=b"%PDF-fake", mime_type="application/pdf"
            )
        ),
    )

    runner = _RecordingRunner(
        events=[
            _final_text_event("here is the report"),
            _artifact_event("report.pdf", version),
        ]
    )
    ch = _make_channel(runner, artifact_service=service)
    reply = await ch.handle_message(user_id="u", session_id="s", message="x")
    assert reply.text == "here is the report"
    assert len(reply.files) == 1
    f = reply.files[0]
    assert f.filename == "report.pdf"
    assert f.mime == "application/pdf"
    assert f.data == b"%PDF-fake"


@pytest.mark.asyncio
async def test_handle_message_swallows_artifact_load_failures() -> None:
    """A load_artifact crash shouldn't kill the whole reply — log,
    skip the artifact, return whatever else we collected."""

    class _FailingService:
        async def load_artifact(self, **kwargs: Any) -> Any:
            raise RuntimeError("storage down")

    runner = _RecordingRunner(
        events=[
            _final_text_event("here"),
            _artifact_event("broken.png", 1),
        ]
    )
    ch = _make_channel(runner, artifact_service=_FailingService())
    reply = await ch.handle_message(user_id="u", session_id="s", message="x")
    assert reply.text == "here"
    assert reply.files == ()


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
        def __init__(
            self,
            *,
            app: Any,
            session_service: Any,
            artifact_service: Any = None,
            auto_create_session: bool,
        ):
            captured["app"] = app

    monkeypatch.setattr(base_module, "Runner", _StubRunner)

    ch = base_module.ChannelBase(
        session_service=MagicMock(),
        extra_instruction="MARKER-FROM-CHANNEL",
    )
    from google.adk.agents.readonly_context import ReadonlyContext

    rendered = await ch._app.root_agent.instruction(
        MagicMock(spec=ReadonlyContext)
    )
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
