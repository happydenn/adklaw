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
import mimetypes
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from google.adk.apps import App
from google.adk.artifacts import BaseArtifactService, InMemoryArtifactService
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
class OutboundFile:
    """A file the agent emitted in this turn that the channel should
    deliver to the user.

    Sourced from two places by `ChannelBase.handle_message`:
    `Part(inline_data=Blob(...))` Parts on final-response events
    (model-emitted), and entries in
    `event.actions.artifact_delta` (tool-saved via
    `tool_context.save_artifact`). Both end up here; channels treat
    them uniformly.
    """

    filename: str
    mime: str | None
    data: bytes


@dataclass(frozen=True)
class AgentReply:
    """One agent turn's output, in transport-agnostic shape.

    Channels consume this and decide how to deliver: Discord
    attaches `files` as `discord.File` objects on the same
    `message.reply(...)`; future Slack / Telegram do the analogous
    thing. `text` may be empty when the agent's response is purely
    file output.
    """

    text: str
    files: tuple[OutboundFile, ...] = field(default_factory=tuple)


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


_FILENAME_BAD = re.compile(r"[\x00\\/]+")


def _sanitize_filename(name: str | None, fallback: str = "file") -> str:
    """Normalize an outbound filename so transports don't reject it.

    Strips path separators / NULs (path-traversal hygiene), strips
    leading dots (avoid hidden-file weirdness), and truncates to 100
    chars. Falls back to `"file"` if the input collapses to empty.
    """
    if not name:
        return fallback
    n = _FILENAME_BAD.sub("_", name).lstrip(".")[:100]
    return n or fallback


def _synthesize_filename(mime: str | None, idx: int) -> str:
    """Mint a filename for a model-emitted `inline_data` Part.

    Gemini Parts have no filename slot, so we synthesize one from
    the mime type. `idx` disambiguates multiple parts in one reply.
    """
    ext = mimetypes.guess_extension(mime or "") or ".bin"
    return f"agent_{idx}{ext}"


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
        artifact_service: BaseArtifactService | None = None,
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
        # Default to in-memory artifacts. The Cloud Run / Vertex deploy
        # path in `app/fast_api_app.py` builds its own Runner with a
        # GCS-backed service; channels run in-process, so the simpler
        # in-memory store is fine for the personal-bot scope.
        self._artifact_service = artifact_service or InMemoryArtifactService()
        self._runner = Runner(
            app=app,
            session_service=session_service,
            artifact_service=self._artifact_service,
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
    ) -> AgentReply:
        """Run one turn of the agent and return its assistant output.

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
            An `AgentReply(text, files)`. `text` is the joined final
            assistant text; `files` carries any binary output the
            agent produced this turn — both `Part(inline_data=...)`
            Parts on final-response events (model-emitted) and
            artifacts saved via `tool_context.save_artifact(...)`.
            Tool calls and partial streaming chunks are excluded.
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
        inline_files: list[OutboundFile] = []
        artifact_versions: dict[str, int] = {}
        async with self._lock_for(session_id):
            async with Aclosing(
                self._runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=new_message,
                )
            ) as events:
                async for event in events:
                    chunk_text, new_inline = _collect_event(
                        event, len(inline_files)
                    )
                    if chunk_text:
                        chunks.append(chunk_text)
                    inline_files.extend(new_inline)
                    if event.actions and event.actions.artifact_delta:
                        # Last write wins: the final version per
                        # filename is what we should load.
                        artifact_versions.update(event.actions.artifact_delta)

        artifact_files = await self._load_artifact_files(
            user_id=user_id,
            session_id=session_id,
            versions=artifact_versions,
            already_named={f.filename for f in inline_files},
        )
        return AgentReply(
            text="".join(chunks).strip(),
            files=tuple(inline_files + artifact_files),
        )

    async def _load_artifact_files(
        self,
        *,
        user_id: str,
        session_id: str,
        versions: dict[str, int],
        already_named: set[str],
    ) -> list[OutboundFile]:
        """Pull bytes out of the artifact service for each saved
        artifact this turn.

        Skips filenames already covered by an inline Part so we
        don't double-deliver. Also skips internal artifacts whose
        names start with `_` — these are working data the agent
        needs (e.g. bytes cached by `web_fetch` for the
        `load_artifacts` tool to surface back to the model) but
        the user shouldn't receive as a chat attachment.
        """
        out: list[OutboundFile] = []
        for fname, version in versions.items():
            if fname.startswith("_"):
                continue
            sanitized = _sanitize_filename(fname)
            if sanitized in already_named:
                continue
            try:
                part = await self._artifact_service.load_artifact(
                    app_name=self._app.name,
                    user_id=user_id,
                    session_id=session_id,
                    filename=fname,
                    version=version,
                )
            except Exception:
                logger.exception(
                    "Failed to load artifact %s v%s", fname, version
                )
                continue
            if (
                part is None
                or not part.inline_data
                or not part.inline_data.data
            ):
                continue
            out.append(
                OutboundFile(
                    filename=sanitized,
                    mime=part.inline_data.mime_type,
                    data=bytes(part.inline_data.data),
                )
            )
        return out


def _collect_event(
    event: Event, inline_offset: int
) -> tuple[str, list[OutboundFile]]:
    """Pull text and `inline_data` Parts out of a final-response event.

    Returns `("", [])` for tool calls, tool responses, partial
    streaming chunks, and code-execution events — those should not
    be surfaced verbatim to channel users. `inline_offset` is the
    number of inline files already collected this turn, used to
    keep synthesized filenames unique across events.
    """
    if not event.is_final_response():
        return "", []
    if not event.content or not event.content.parts:
        return "", []
    text_chunks: list[str] = []
    inline: list[OutboundFile] = []
    for part in event.content.parts:
        if part.text:
            text_chunks.append(part.text)
        elif part.inline_data and part.inline_data.data:
            inline.append(
                OutboundFile(
                    filename=_synthesize_filename(
                        part.inline_data.mime_type,
                        inline_offset + len(inline),
                    ),
                    mime=part.inline_data.mime_type,
                    data=bytes(part.inline_data.data),
                )
            )
    return "".join(text_chunks), inline
