"""Discord channel.

Wires the agent up to a Discord bot account. In guild channels the bot
only responds when @-mentioned (so it doesn't interject on every
message); in DMs it always responds. Each Discord channel/thread/DM
gets its own ADK session, so conversations are isolated per location.

Run with:

    uv sync --extra discord
    DISCORD_BOT_TOKEN=... uv run python -m app.channels.discord
"""

from __future__ import annotations

import collections
import functools
import io
import logging
import os
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

from google.adk.apps import App
from google.adk.sessions import BaseSessionService
from google.genai import types

from .base import (
    AgentReply,
    ChannelBase,
    ContextMessage,
    DroppedAttachment,
    Origin,
    OutboundFile,
    _sanitize_filename,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import discord

    _FirstSender = Callable[..., Awaitable[None]]
    _FollowSender = Callable[..., Awaitable[None]]

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000


@functools.cache
def _allowed_user_ids() -> frozenset[str]:
    """Return the configured allowlist as a frozenset of user IDs.

    Empty (env var unset or blank) means "no allowlist — allow all."
    Cached for the process lifetime; restart the bot to pick up env
    changes.
    """
    raw = os.environ.get("DISCORD_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


DEFAULT_HISTORY_LINES = 20

REPLY_TEXT_MAX = 500


@functools.cache
def _history_limit() -> int:
    """How many prior channel messages to ship as `[context]` on a guild
    mention. `0` disables the feature entirely (no buffer, no API calls).

    Configured via `DISCORD_CONTEXT_HISTORY_LINES`; default 20.
    """
    raw = os.environ.get("DISCORD_CONTEXT_HISTORY_LINES", "").strip()
    if not raw:
        return DEFAULT_HISTORY_LINES
    try:
        n = int(raw)
    except ValueError:
        logger.warning(
            "Invalid DISCORD_CONTEXT_HISTORY_LINES=%r; defaulting to %d.",
            raw,
            DEFAULT_HISTORY_LINES,
        )
        return DEFAULT_HISTORY_LINES
    return max(0, n)


@functools.cache
def _allowlist_scope() -> str:
    """Return how broadly the allowlist applies: "dm" (default) or "all".

    - "dm": only DMs are gated. Guild mentions always bypass the
      allowlist (anyone who can @-mention the bot in a server you've
      invited it to is implicitly trusted enough).
    - "all": both DMs and guild mentions are gated.
    """
    raw = os.environ.get("DISCORD_ALLOWLIST_SCOPE", "dm").strip().lower()
    if raw not in ("dm", "all"):
        logger.warning(
            "Unknown DISCORD_ALLOWLIST_SCOPE=%r; defaulting to 'dm'.", raw
        )
        return "dm"
    return raw


def _parse_bool(raw: str, *, default: bool, var: str) -> bool:
    """Permissive bool parser shared by the bot-handling toggles.

    Accepts true/false, 1/0, yes/no (case-insensitive). Empty string →
    default. Anything else → log a warning and return the default.
    """
    s = raw.strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    if s == "":
        return default
    logger.warning("Unknown %s=%r; defaulting to %s.", var, raw, default)
    return default


@functools.cache
def _reply_to_bots() -> bool:
    """Whether to reply to @-mentions from other bots (webhooks,
    bridges, integrations, friendly bots).

    Default false: blocks bot-to-bot ping-pong. Self-bot messages are
    always skipped, regardless of this toggle. Pair with
    `DISCORD_ALLOWED_USER_IDS` + `DISCORD_ALLOWLIST_SCOPE=all` for
    fine-grained restriction to specific bots.
    """
    return _parse_bool(
        os.environ.get("DISCORD_REPLY_TO_BOTS", ""),
        default=False,
        var="DISCORD_REPLY_TO_BOTS",
    )


@functools.cache
def _quote_bot_replies() -> bool:
    """Whether to use `message.reply(...)` (quoted reference) when the
    original author is a bot.

    Default false → use plain `channel.send(...)`, so the response
    carries no implicit mention or `MessageReference` pointing back at
    the other bot. This is the recommended config when
    `DISCORD_REPLY_TO_BOTS=true`: it prevents the most common form of
    bot-to-bot ping-pong (the other bot triggering on a mention of
    itself in our reply chain). Set true if you specifically want the
    visual quote and accept the loop risk. Has no effect on responses
    to human authors — those always use `message.reply(...)`.
    """
    return _parse_bool(
        os.environ.get("DISCORD_QUOTE_BOT_REPLIES", ""),
        default=False,
        var="DISCORD_QUOTE_BOT_REPLIES",
    )


# Mime types Gemini accepts as `inline_data`. `text/*` is handled
# separately (decoded and inlined into the prompt for small files).
GEMINI_SUPPORTED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/heic",
        "image/heif",
        "audio/wav",
        "audio/mp3",
        "audio/aiff",
        "audio/aac",
        "audio/ogg",
        "audio/flac",
        "video/mp4",
        "video/mpeg",
        "video/mov",
        "video/avi",
        "video/x-flv",
        "video/mpg",
        "video/webm",
        "video/wmv",
        "video/3gpp",
        "application/pdf",
    }
)

TEXT_ATTACHMENT_MAX_BYTES = 65_536
DEFAULT_ATTACHMENT_MAX_BYTES = 10_000_000
DEFAULT_ATTACHMENTS_MAX_TOTAL_BYTES = 18_000_000


@functools.cache
def _attachment_max_bytes() -> int:
    """Per-attachment hard cap. Anything larger is skipped and
    reported. Configurable via `DISCORD_ATTACHMENT_MAX_BYTES`."""
    raw = os.environ.get("DISCORD_ATTACHMENT_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_ATTACHMENT_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Invalid DISCORD_ATTACHMENT_MAX_BYTES=%r; defaulting to %d.",
            raw,
            DEFAULT_ATTACHMENT_MAX_BYTES,
        )
        return DEFAULT_ATTACHMENT_MAX_BYTES


@functools.cache
def _attachments_max_total_bytes() -> int:
    """Total bytes across all attachments on a single message. Stops
    the model request from blowing past Gemini's inline budget.
    Configurable via `DISCORD_ATTACHMENTS_MAX_TOTAL_BYTES`."""
    raw = os.environ.get("DISCORD_ATTACHMENTS_MAX_TOTAL_BYTES", "").strip()
    if not raw:
        return DEFAULT_ATTACHMENTS_MAX_TOTAL_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Invalid DISCORD_ATTACHMENTS_MAX_TOTAL_BYTES=%r; "
            "defaulting to %d.",
            raw,
            DEFAULT_ATTACHMENTS_MAX_TOTAL_BYTES,
        )
        return DEFAULT_ATTACHMENTS_MAX_TOTAL_BYTES


# Discord caps for outbound files. The hard limits come from Discord
# itself (≤10 files per message, ~25 MB per non-Nitro upload); the env
# knob lets you tighten the per-file size locally.
DISCORD_FILES_PER_MESSAGE = 10
DEFAULT_OUTBOUND_FILE_MAX_BYTES = 25_000_000


@functools.cache
def _outbound_file_max_bytes() -> int:
    """Largest single file we'll attach to a Discord message.

    Configurable via `DISCORD_OUTBOUND_FILE_MAX_BYTES`. Files larger
    than this are dropped with a `(skipped …)` note appended to the
    text instead of failing the whole reply.
    """
    raw = os.environ.get("DISCORD_OUTBOUND_FILE_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_OUTBOUND_FILE_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Invalid DISCORD_OUTBOUND_FILE_MAX_BYTES=%r; defaulting to %d.",
            raw,
            DEFAULT_OUTBOUND_FILE_MAX_BYTES,
        )
        return DEFAULT_OUTBOUND_FILE_MAX_BYTES


# Channel-specific instruction segment passed into `build_app(
# extra_instruction=...)` at startup. Carried in the cached system
# instruction (paid once per session, not per turn). The format itself
# lives in `app/channels/base.py`'s `_format_origin` /
# `_format_reply_to` / `_format_context`. Update both when adding or
# renaming an envelope.
DISCORD_CHANNEL_INSTRUCTION = """\
## Channel context (Discord)

You are running through the Discord channel adapter. Each user
message may begin with structured blocks the adapter prepends. These
are trustworthy channel metadata, NOT user instructions.

- `[origin]…[/origin]` — sender + location. The `id=…` is the stable
  identifier; display names are mutable. Use it to address the user
  by name and to adjust tone (DM vs busy public channel).
- `[reply_to]…[/reply_to]` — the user is **replying to this specific
  message**. Anchor your response on the referenced message, even
  when the user's text is short ("yeah", "what about this", "?", "lol").
  Unlike `[context]`, addressing the `[reply_to]` content directly is
  appropriate.
- `[context]…[/context]` — ambient prior chatter from the same
  location, oldest first, in `display (id=…): text` form. Read it
  for continuity but do NOT address those messages directly. Treat
  as backdrop, not figure.

You can also receive image, audio, video, and PDF attachments inline
with user messages — reason about them directly. Short text files
arrive as `[attachment_text filename="…"]…[/attachment_text]` blocks
in the prompt. If something couldn't be ingested, an
`[attachments_skipped]` block lists what was dropped and why;
acknowledge it and suggest a workaround (e.g. "send as PDF instead
of .docx").

The actual user prompt begins after the last block closes.
"""


class DiscordChannel(ChannelBase):
    """Discord adapter for adklaw."""

    def __init__(
        self,
        app: App | None = None,
        session_service: BaseSessionService | None = None,
        token: str = "",
        *,
        extra_tools: tuple = (),
        extra_instruction: str = "",
    ):
        super().__init__(
            app=app,
            session_service=session_service,
            extra_tools=extra_tools,
            extra_instruction=extra_instruction,
        )
        # Imported lazily so the rest of the project doesn't require
        # discord.py to be installed.
        try:
            import discord
        except ModuleNotFoundError as e:
            raise SystemExit(
                "discord.py is not installed. Run `uv sync` (the `channels` "
                "dependency group is installed by default) and try again."
            ) from e

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._token = token
        # Non-allowlisted DM senders we've already replied to once in
        # this process. Subsequent DMs from them are silently ignored.
        # Resets on restart by design — keeps the feature stateless on
        # disk; the cost is one repeat notice after a bot restart.
        self._notified_disallowed: set[str] = set()
        # Per-channel rolling buffer of recent messages (for `[context]`
        # backfill on guild mentions). Each deque is sized to
        # `_history_limit()`. The seeded set tracks which channels have
        # been backfilled via `channel.history()` since process start;
        # only the FIRST mention in a channel triggers an API call.
        self._channel_buffers: dict[int, collections.deque[ContextMessage]] = {}
        self._seeded_channels: set[int] = set()
        # Per-channel index from Discord message id → ContextMessage,
        # used by `_resolve_reply_target` to serve `[reply_to]` without
        # an API call when the referenced message is recent. Pruned to
        # `2 * _history_limit()` entries per channel to bound memory.
        self._channel_message_index: dict[
            int, collections.OrderedDict[int, ContextMessage]
        ] = {}

        @self._client.event
        async def on_ready() -> None:
            logger.info("Discord bot logged in as %s", self._client.user)

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            await self._on_message(message)

    def _index_message(
        self, channel_id: int, msg_id: int, cm: ContextMessage
    ) -> None:
        """Record `msg_id → cm` in the per-channel id index, evicting
        the oldest entries when the index exceeds `2 * _history_limit()`."""
        limit = _history_limit()
        if limit <= 0:
            return
        cap = 2 * limit
        idx = self._channel_message_index.get(channel_id)
        if idx is None:
            idx = collections.OrderedDict()
            self._channel_message_index[channel_id] = idx
        # Re-inserting moves the entry to the end (most recent).
        if msg_id in idx:
            idx.move_to_end(msg_id)
        idx[msg_id] = cm
        while len(idx) > cap:
            idx.popitem(last=False)

    def _record_in_buffer(self, message: discord.Message) -> None:
        """Append a guild message to its channel's rolling context buffer.

        Skips our own bot's messages (already in the ADK session) and
        empty content, but **keeps** messages from other bots — webhooks,
        bridges (PluralKit, IRC relays), GitHub/news integrations, etc.
        are conversational content the agent should see for continuity.
        Creates the deque lazily so we only allocate for channels we
        actually see.
        """
        limit = _history_limit()
        if limit <= 0:
            return
        if message.guild is None:
            return
        if self._client.user is not None and message.author.id == self._client.user.id:
            return
        text = message.clean_content
        if not text:
            return
        buf = self._channel_buffers.get(message.channel.id)
        if buf is None:
            buf = collections.deque(maxlen=limit)
            self._channel_buffers[message.channel.id] = buf
        cm = ContextMessage(
            sender_id=str(message.author.id),
            sender_display=message.author.display_name,
            text=text,
        )
        buf.append(cm)
        self._index_message(message.channel.id, message.id, cm)

    async def _build_context(
        self, message: discord.Message
    ) -> list[ContextMessage]:
        """Return up to N prior messages from the channel for `[context]`.

        First mention in a channel since startup → fetch via
        `channel.history(limit=N, before=message)` and seed the buffer.
        Subsequent mentions → read straight from the buffer (zero API
        calls). On history fetch failure, return an empty list and do
        NOT mark the channel seeded so the next mention will retry.
        """
        limit = _history_limit()
        if limit <= 0:
            return []
        channel_id = message.channel.id
        if channel_id in self._seeded_channels:
            buf = self._channel_buffers.get(channel_id)
            if buf is None:
                return []
            # The trigger message was appended at the top of _on_message,
            # so it's the last entry. Drop it; the caller is the trigger.
            buf_list = list(buf)
            return buf_list[:-1] if buf_list else []

        # First mention since startup — backfill via REST.
        # Collect (msg_id, ContextMessage) pairs so we can populate the
        # id index alongside the buffer.
        pairs: list[tuple[int, ContextMessage]] = []
        bot_user_id = (
            self._client.user.id if self._client.user is not None else None
        )
        try:
            async for hist in message.channel.history(
                limit=limit, before=message
            ):
                # Skip only our own messages (already in the session).
                # Other bots / webhooks / bridges are real conversational
                # content and stay in.
                if bot_user_id is not None and hist.author.id == bot_user_id:
                    continue
                text = hist.clean_content
                if not text:
                    continue
                pairs.append(
                    (
                        hist.id,
                        ContextMessage(
                            sender_id=str(hist.author.id),
                            sender_display=hist.author.display_name,
                            text=text,
                        ),
                    )
                )
        except Exception:
            logger.exception(
                "Failed to fetch channel history for %s; "
                "proceeding without [context] block",
                channel_id,
            )
            return []
        # discord.py yields newest-first; reverse to oldest-first.
        pairs.reverse()
        msgs = [cm for _, cm in pairs]
        # Seed the buffer with what we got so subsequent mentions can
        # serve from memory.
        buf: collections.deque[ContextMessage] = collections.deque(
            msgs, maxlen=limit
        )
        self._channel_buffers[channel_id] = buf
        self._seeded_channels.add(channel_id)
        # Populate the id index for `_resolve_reply_target`.
        for mid, cm in pairs:
            self._index_message(channel_id, mid, cm)
        return msgs

    async def _resolve_reply_target(
        self, message: discord.Message
    ) -> ContextMessage | None:
        """Return the message the user is explicitly replying to, or None.

        Resolution waterfall:
        1. `message.reference.resolved` (cheap, in-cache).
        2. Per-channel id index (zero API calls).
        3. `channel.fetch_message(id)` (one REST call).
        4. Any failure → return None and proceed without `[reply_to]`.
        """
        try:
            import discord
        except ModuleNotFoundError:
            return None
        ref = getattr(message, "reference", None)
        if ref is None:
            return None
        ref_msg_id = getattr(ref, "message_id", None)
        if ref_msg_id is None:
            return None

        referenced: discord.Message | None = None
        resolved = getattr(ref, "resolved", None)
        if resolved is not None and not isinstance(
            resolved, discord.DeletedReferencedMessage
        ):
            referenced = resolved
        else:
            idx = self._channel_message_index.get(message.channel.id)
            cached = idx.get(ref_msg_id) if idx is not None else None
            if cached is not None:
                return cached
            try:
                referenced = await message.channel.fetch_message(ref_msg_id)
            except Exception:
                logger.debug(
                    "Failed to fetch reply target %s on channel %s",
                    ref_msg_id,
                    message.channel.id,
                )
                return None

        if referenced is None:
            return None
        text = (referenced.clean_content or "").strip()
        if not text:
            return None
        if len(text) > REPLY_TEXT_MAX:
            text = text[: REPLY_TEXT_MAX - 1].rstrip() + "…"
        return ContextMessage(
            sender_id=str(referenced.author.id),
            sender_display=referenced.author.display_name,
            text=text,
        )

    async def _collect_attachments(
        self, message: discord.Message
    ) -> tuple[list[types.Part], str, list[DroppedAttachment]]:
        """Download supported attachments and shape them for the model.

        Returns ``(parts, inline_text_block, skipped)``:

        - ``parts``: ``inline_data`` Parts for images / audio / video /
          PDF, in declared order.
        - ``inline_text_block``: concatenated `[attachment_text …]`
          blocks for small `text/*` attachments. Caller prepends to
          the user prompt.
        - ``skipped``: items filtered out (mime not supported, too
          large, or a download error). Reported back as an
          `[attachments_skipped]` block.

        Greedy fill: take attachments in declared order until the
        per-message total cap; subsequent ones are reported as
        skipped.
        """
        parts: list[types.Part] = []
        text_blocks: list[str] = []
        skipped: list[DroppedAttachment] = []
        if not message.attachments:
            return parts, "", skipped

        per_cap = _attachment_max_bytes()
        total_cap = _attachments_max_total_bytes()
        used = 0

        for att in message.attachments:
            mime = att.content_type or ""
            is_text = mime.startswith("text/")
            is_supported_inline = mime in GEMINI_SUPPORTED_MIME_TYPES

            if not (is_text or is_supported_inline):
                skipped.append(
                    DroppedAttachment(
                        filename=att.filename,
                        mime=mime or None,
                        size=att.size,
                        reason="unsupported type",
                    )
                )
                continue
            if is_text and att.size > TEXT_ATTACHMENT_MAX_BYTES:
                skipped.append(
                    DroppedAttachment(
                        filename=att.filename,
                        mime=mime,
                        size=att.size,
                        reason=(
                            f"text too large (>{TEXT_ATTACHMENT_MAX_BYTES} bytes)"
                        ),
                    )
                )
                continue
            if att.size > per_cap:
                skipped.append(
                    DroppedAttachment(
                        filename=att.filename,
                        mime=mime,
                        size=att.size,
                        reason=(
                            f"exceeds per-attachment cap ({per_cap} bytes)"
                        ),
                    )
                )
                continue
            if used + att.size > total_cap:
                skipped.append(
                    DroppedAttachment(
                        filename=att.filename,
                        mime=mime,
                        size=att.size,
                        reason=(
                            f"would exceed total cap ({total_cap} bytes)"
                        ),
                    )
                )
                continue

            try:
                data = await att.read()
            except Exception as e:
                logger.warning(
                    "Failed to download attachment %s: %s",
                    att.filename,
                    e,
                )
                skipped.append(
                    DroppedAttachment(
                        filename=att.filename,
                        mime=mime,
                        size=att.size,
                        reason=f"download failed: {e}",
                    )
                )
                continue

            used += len(data)
            if is_text:
                decoded = data.decode("utf-8", errors="replace")
                text_blocks.append(
                    f'[attachment_text filename="{att.filename}"]\n'
                    f"{decoded}\n"
                    f"[/attachment_text]\n"
                )
            else:
                parts.append(
                    types.Part(
                        inline_data=types.Blob(data=data, mime_type=mime)
                    )
                )

        return parts, "\n".join(text_blocks), skipped

    async def _dispatch_reply(
        self,
        reply: AgentReply,
        send_first: _FirstSender,
        send_follow: _FollowSender,
    ) -> None:
        """Deliver an `AgentReply` to Discord, handling text chunking,
        file batching, and the empty-response case.

        The first emitted message reuses whichever send mode the
        caller picked (quoted reply for humans, plain `channel.send`
        for bots). Follow-up messages always go through
        `channel.send` — pinning every chunk back to the original
        message would be noisy.

        File-attach failures fall back to text-only with a
        `(could not attach …)` note so a bad CDN moment doesn't
        eat the whole reply.
        """
        text = reply.text
        files, skipped_notes = _to_discord_files(reply.files)
        if skipped_notes:
            text = (text + "\n" + "\n".join(skipped_notes)).strip()

        if not text and not files:
            await send_first("(no response)")
            return

        text_chunks = _split_for_discord(text) if text else [""]
        file_batches = list(_batched(files, DISCORD_FILES_PER_MESSAGE)) or [[]]

        # Pair the first text chunk with the first file batch on a
        # single send. Remaining text chunks go out as plain
        # follow-ups; remaining file batches go out as files-only
        # follow-ups.
        first_text, *rest_text = text_chunks
        first_files, *rest_files = file_batches

        try:
            await send_first(first_text, first_files or None)
        except Exception:
            logger.exception(
                "Failed to send Discord reply with files; falling back to text-only."
            )
            note = ""
            if first_files:
                names = ", ".join(_filename_of(f) for f in first_files)
                note = f"\n(could not attach {names})"
            await send_first((first_text + note).strip() or "(no response)")
            rest_files = []  # don't keep retrying file sends after a failure

        for chunk in rest_text:
            await send_follow(chunk)
        for batch in rest_files:
            try:
                await send_follow("", batch)
            except Exception:
                names = ", ".join(_filename_of(f) for f in batch)
                logger.exception(
                    "Failed to send Discord follow-up with files (%s).",
                    names,
                )
                await send_follow(f"(could not attach {names})")

    async def _on_message(self, message: discord.Message) -> None:
        # Record every guild message into the channel's rolling buffer
        # before any gating — even messages that don't trigger the bot
        # are valuable conversational context for the next mention.
        self._record_in_buffer(message)

        # Always skip our own messages — prevents self-loops regardless
        # of any other toggle.
        if (
            self._client.user is not None
            and message.author.id == self._client.user.id
        ):
            return

        # Other bots only respond when explicitly opted in. Pair with
        # DISCORD_ALLOWED_USER_IDS + DISCORD_ALLOWLIST_SCOPE=all to
        # restrict which bots can trigger replies.
        if message.author.bot and not _reply_to_bots():
            return

        is_dm = message.guild is None
        sender_id = str(message.author.id)

        # Allowlist gate. Empty allowlist means "allow all" (default).
        # `DISCORD_ALLOWLIST_SCOPE` decides whether the allowlist applies
        # to DMs only ("dm", default) or every surface ("all"). Under "dm",
        # guild mentions are never gated by the allowlist — they're already
        # restricted to people who share a server with the bot.
        # First-time non-allowed DMs get one polite reply telling the
        # operator how to add the user; everything else is silent.
        allowed = _allowed_user_ids()
        scope = _allowlist_scope()
        gated = (scope == "all") or is_dm
        if allowed and gated and sender_id not in allowed:
            if is_dm and sender_id not in self._notified_disallowed:
                self._notified_disallowed.add(sender_id)
                await message.reply(
                    f"You are not on the allowlist. Add `{sender_id}` "
                    f"to `DISCORD_ALLOWED_USER_IDS` and try again."
                )
                logger.info("Notified non-allowlisted DM sender %s", sender_id)
            else:
                logger.info(
                    "Ignoring %s from non-allowlisted user %s",
                    "DM" if is_dm else f"mention in #{message.channel.name}",
                    sender_id,
                )
            return

        # Activation policy: respond in DMs always; in guilds, only when
        # the bot is mentioned. Avoids spamming busy channels.
        is_mentioned = self._client.user in message.mentions
        if not is_dm and not is_mentioned:
            return

        # Strip the bot mention out of the prompt so the agent doesn't
        # see "@adklaw what skills do you have?" — it sees just the
        # actual question.
        prompt = message.clean_content
        if is_mentioned and self._client.user is not None:
            mention_name = f"@{self._client.user.display_name}"
            if prompt.startswith(mention_name):
                prompt = prompt[len(mention_name) :].lstrip()

        # Pull attachments before the empty-prompt guard so that an
        # attachment-only message (e.g. just an image, no text) still
        # triggers the agent.
        attach_parts, attach_text, attach_skipped = (
            await self._collect_attachments(message)
        )

        if not prompt and not attach_parts and not attach_text and not attach_skipped:
            return

        # Build the Origin envelope so the agent knows who/where it's
        # talking to. IDs are stable; display names are mutable but
        # readable for the LLM.
        if is_dm:
            location_display = "DM"
        else:
            guild_name = message.guild.name if message.guild else "?"
            location_display = (
                f"guild '{guild_name}'"
                + (f" (id={message.guild.id})" if message.guild else "")
                + f" / channel #{message.channel.name}"
            )
        origin = Origin(
            transport="discord",
            sender_id=sender_id,
            sender_display=message.author.display_name,
            location_id=str(message.channel.id),
            location_display=location_display,
        )

        # Backfill prior chatter only for guild mentions. DMs already
        # round-trip every message through the agent, so the session
        # has full DM history without any extra fetching.
        context: list[ContextMessage] = []
        if not is_dm:
            context = await self._build_context(message)

        # Surface the message the user is explicitly replying to (if
        # any) as a `[reply_to]` block. Always evaluated — DMs support
        # replies too, and the explicit anchor is valuable even when
        # the session has full history.
        reply_to = await self._resolve_reply_target(message)

        # When responding to another bot, default to a plain
        # `channel.send(...)` so the response carries no `MessageReference`
        # mention back at the other bot. Human authors always get the
        # quoted reply (current behaviour).
        author_is_bot = message.author.bot
        use_quoted_reply = (not author_is_bot) or _quote_bot_replies()

        # The first message of a multi-part reply uses the quoted-reply
        # mechanic (or `channel.send` for bots); follow-up messages
        # always go through `channel.send` so we don't pin every chunk
        # back to the original message.
        async def _send_first(
            text: str, files: list[discord.File] | None = None
        ) -> None:
            kwargs: dict[str, object] = {}
            if files:
                kwargs["files"] = files
            if use_quoted_reply:
                await message.reply(text, **kwargs)
            else:
                await message.channel.send(text, **kwargs)

        async def _send_follow(
            text: str, files: list[discord.File] | None = None
        ) -> None:
            kwargs: dict[str, object] = {}
            if files:
                kwargs["files"] = files
            await message.channel.send(text, **kwargs)

        prompt_with_text_attachments = (
            attach_text + prompt if attach_text else prompt
        )

        try:
            async with message.channel.typing():
                reply = await self.handle_message(
                    user_id=sender_id,
                    session_id=str(message.channel.id),
                    message=prompt_with_text_attachments,
                    origin=origin,
                    reply_to=reply_to,
                    context=context or None,
                    attachments=attach_parts,
                    attachments_skipped=attach_skipped,
                )
        except Exception:
            logger.exception("Agent run failed for Discord message %s", message.id)
            await _send_first(
                "Sorry — something went wrong handling that message. "
                "Check the bot logs."
            )
            return

        await self._dispatch_reply(reply, _send_first, _send_follow)

    def run(self) -> None:
        """Block on the Discord client. Returns when the bot disconnects."""
        self._client.run(self._token)


def _to_discord_files(
    files: Sequence[OutboundFile],
) -> tuple[list[discord.File], list[str]]:
    """Build `discord.File` objects from `OutboundFile`s.

    Filenames are sanitized at the send boundary (defense in depth —
    `ChannelBase` also sanitizes artifact names). Files larger than
    `DISCORD_OUTBOUND_FILE_MAX_BYTES` are dropped and a `(skipped …)`
    note is returned for the agent's reply text.
    """
    import discord  # local import — discord.py is an optional extra

    cap = _outbound_file_max_bytes()
    out: list[discord.File] = []
    skipped_notes: list[str] = []
    for f in files:
        name = _sanitize_filename(f.filename)
        if len(f.data) > cap:
            skipped_notes.append(f"(skipped {name}: too large)")
            continue
        out.append(discord.File(fp=io.BytesIO(f.data), filename=name))
    return out, skipped_notes


def _batched(
    items: list[discord.File], n: int
) -> Iterator[list[discord.File]]:
    """Yield successive `n`-sized chunks from `items`. Empty input
    yields nothing — the caller decides whether the empty case is
    distinct from a single empty batch."""
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _filename_of(f: discord.File) -> str:
    """`discord.File.filename` may be `None`; surface a printable
    name so error notes don't say 'could not attach None'."""
    return f.filename or "file"


def _split_for_discord(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split a long response into Discord-sized chunks at line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Try to break at the last newline before the limit; fall back
        # to a hard split if no newline is reachable.
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def main() -> None:
    """Entrypoint for `python -m app.channels.discord`."""
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_BOT_TOKEN is not set. Add it to .env or your shell environment."
        )

    # Imported here (not at module top) so importing the module doesn't
    # require Vertex AI credentials and so the agent's heavy imports
    # only happen when actually launching the bot.
    from google.adk.sessions.sqlite_session_service import SqliteSessionService

    from app.state import get_state_dir

    db_path = get_state_dir() / "sessions.db"
    session_service = SqliteSessionService(db_path=str(db_path))
    DiscordChannel(
        session_service=session_service,
        token=token,
        extra_instruction=DISCORD_CHANNEL_INSTRUCTION,
    ).run()


if __name__ == "__main__":
    main()
