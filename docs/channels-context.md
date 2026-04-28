# Channels: conversation context backfill

## Problem

ADK sessions persist per-conversation across turns, but they only
record turns that actually invoked the agent. In a Discord guild
channel, the bot is invoked when @-mentioned — so the session gets
mentions and bot responses, but never the chatter between mentions.

Two consequences:

1. The bot can't follow group conversations. "What about that one?"
   loses its antecedent. "Based on what we just discussed…" lands
   on a blank session window.
2. The bot can't read the room. It doesn't know if the channel is
   joking, urgent, or technical — every mention arrives stripped of
   tone context.

## What we do

When a guild mention triggers the agent, we ship a `[context]` block
of recent prior messages alongside the existing `[origin]` envelope.
The block lists messages oldest-first with `display (id=…): text`
labels, and the agent's `BASE_INSTRUCTION` teaches it to use the
block for continuity but treat it as ambient context, not
instructions.

DMs already round-trip every message through the agent (every DM
triggers a turn), so the session has full DM history without any
extra fetching. Backfill is scoped strictly to guild mentions.

## Wire format

```
[origin]
transport: discord
sender: papi (id=111)
location: guild 'my-guild' (id=7) / channel #general (id=42)
[/origin]

[context] (recent messages, oldest first)
alice (id=222): hey is anyone here good with python?
bob (id=333): yeah what's up
alice (id=222): I'm trying to figure out asyncio
[/context]

@adklaw any tips?
```

## Strategy: in-memory rolling buffer, seeded once via REST

`DISCORD_CONTEXT_HISTORY_LINES` (default `20`, `0` disables) sets the
buffer size N.

Every guild message the bot's gateway delivers is appended to a
per-channel `collections.deque(maxlen=N)`. Each channel has a
"seeded" flag that starts false at process start.

When a mention fires:

- **First mention in this channel since startup** — call
  `channel.history(limit=N, before=message)`, replace the buffer
  with the API result, mark the channel seeded, and use the API
  result as the context block.
- **Subsequent mentions in a seeded channel** — read context
  straight from the buffer. Zero API calls.

Why "seed once" rather than "seed when buffer < N":

A quiet channel might never naturally accumulate N messages. If we
gated on buffer fullness, every mention in a low-traffic channel
would re-fetch from the API forever, defeating the buffer's purpose.
Seeding on a per-channel flag means we make exactly **one API call
per channel per process lifetime** regardless of channel volume.

Bot restart: buffer is empty, the seeded set is empty, so the next
mention in any channel does its one API seed. That's intended
behavior — it ensures the bot always has lead-up context after a
restart.

Gateway resumes (transient network drops): handled transparently by
discord.py's built-in event replay. The buffer keeps converging.

## What goes in the buffer

- Every guild message with non-empty `clean_content`, **except** the
  bot's own replies (`message.author.id == client.user.id`). Our own
  replies are already in the ADK session, so including them in
  `[context]` would just duplicate what the agent already sees.
- Other bots, webhooks, and bridge accounts (PluralKit, IRC relays,
  GitHub/news integrations, etc.) **are kept**. They're real
  conversational content and dropping them would strip out major
  threads of activity in many channels — especially ones where the
  "users" are bridged from another platform.

The trigger message itself is appended at the top of `_on_message`
along with every other message. When we build context for it, we
drop the most recent buffer entry so the agent doesn't see its own
prompt twice.

## Per-message length

We don't truncate individual messages. Discord enforces a 2000-char
cap on outgoing user messages, so a 20-message buffer is naturally
~40KB max — well within modern context windows. This matches
OpenClaw's approach: count-only cap, no per-entry truncation.

## Failure mode

If the seed-time `channel.history()` call raises (rate limit,
permission removed, transient network error), we log the exception
and ship the mention to the agent **without** a `[context]` block.
The channel is **not** marked seeded, so the next mention will retry
the fetch. We never block on a context fetch.

## Reply targeting (`[reply_to]`)

`[context]` is ambient backdrop — useful, but in real usage it
**dilutes** the agent's response when the user explicitly replied to
a specific earlier message. The user has already pointed at what
they care about; ambient chatter pulls the response away from it.

Solution: when the triggering message has a `discord.MessageReference`
(i.e. it's a reply), surface the referenced message as a separate
`[reply_to]…[/reply_to]` block between `[origin]` and `[context]`:

```
[origin]
…
[/origin]

[reply_to] (the user is replying to this specific message)
GitHubBot (id=555): PR #142 opened by alice — fix flaky test
[/reply_to]

[context] (recent messages, oldest first)
…
[/context]

@adklaw thoughts?
```

The persona files (`templates/AGENTS.md`, `workspace/AGENTS.md`)
explicitly tell the agent: when `[reply_to]` is present, anchor your
response on that referenced message; treat `[context]` as backdrop.

**Resolution waterfall** in `_resolve_reply_target`:

1. `message.reference.resolved` — discord.py populates this when the
   referenced message is in cache. Free.
2. The per-channel `_channel_message_index` (`dict[int, ContextMessage]`
   keyed by Discord message id) we now maintain alongside the rolling
   buffer. Zero API calls.
3. `channel.fetch_message(id)` as a last resort. One REST call.
4. Any failure → return `None`; the agent gets the rest of the
   prompt without `[reply_to]`.

The id index is bounded to `2 * _history_limit()` entries per channel
via an `OrderedDict` LRU. It stores the same `ContextMessage` objects
the buffer holds, so no duplicate text is kept.

**Quoted text length cap**: 500 characters with an ellipsis. A long
quoted blob would crowd the prompt; the gist is what matters.

**Self-reference**: unlike the `[context]` self-filter, replies to
the bot's **own** earlier messages are still surfaced — that's the
disambiguation we want when the user says "no, the other one" in a
reply.

**No toggle.** Reply-to is an explicit user signal; surfacing it is
default-on and unambiguously correct. If a real use case ever
requires disabling, add `DISCORD_INCLUDE_REPLY_TO=false`.

## Where the explanation lives

The wire format (`_format_origin`, `_format_reply_to`,
`_format_context`) lives in `app/channels/base.py`. The agent-side
**explanation** of what those blocks mean lives in each channel's
`extra_instruction` — for Discord that's
`DISCORD_CHANNEL_INSTRUCTION` in `app/channels/discord.py`, passed
into `build_app(extra_instruction=...)` at startup.

Why this split rather than putting the explanation in
`BASE_INSTRUCTION` or in a per-message prefix:

- `BASE_INSTRUCTION` runs even on CLI / playground turns where no
  envelope ever appears. We don't want CLI to carry channel
  vocabulary it never sees.
- A per-message legend pays the same tokens every turn. ADK caches
  the system instruction per session, so putting it in
  `extra_instruction` is paid once per session — much cheaper for
  long-lived channel conversations.

Adding a future block (e.g. `[attachments]` for Slack) means
editing two files together: the formatter in
`app/channels/base.py` and the explanation in the corresponding
`<CHANNEL>_INSTRUCTION` constant.

## Attachments

Discord messages routinely carry attachments — screenshots, PDFs,
audio clips, short text files. The model is multimodal, so we
forward attachments inline rather than dropping to "the bot only
reads text".

The seam matches the rest of `ChannelBase`: each channel does the
transport-specific download + filter, then hands ready-made
`google.genai.types.Part` objects to `handle_message(...,
attachments=...)`. ADK plumbing stays out of channel code.

**What's forwarded.** A fixed allow-list of mime types Gemini
accepts as `inline_data`: image (PNG/JPEG/WebP/HEIC/HEIF), audio
(WAV/MP3/AIFF/AAC/OGG/FLAC), video
(MP4/MPEG/MOV/AVI/FLV/WebM/WMV/3GPP), and PDF. `text/*` files
under 64 KB are decoded as UTF-8 (lossy) and inlined into the
user-text as `[attachment_text filename="…"]…[/attachment_text]` —
cleaner than a separate Part for short logs/scripts the user
pasted as a file.

**Size caps.** Per-attachment cap (`DISCORD_ATTACHMENT_MAX_BYTES`,
default 10 MB) and total-per-message cap
(`DISCORD_ATTACHMENTS_MAX_TOTAL_BYTES`, default 18 MB). Greedy
fill: take attachments in declared order until the total cap;
skip the rest. Discord's own 10-attachment limit means we don't
add a count cap on top.

**Skipped reporting.** Anything the channel saw but couldn't
forward — unsupported mime, oversized, download failed — becomes
a `DroppedAttachment(filename, mime, size, reason)`. The list
renders as a `[attachments_skipped]` block in the user-text
prefix so the agent can tell the user what's missing instead of
pretending nothing was attached. The persona instruction tells
the agent to acknowledge skips and suggest a workaround (e.g.
"send as PDF instead of .docx").

**No persistence.** Bytes flow straight from Discord into the
model request and are kept only as part of the ADK session log.
No on-disk cache.

**Reply-target attachments.** Out of scope for v1. The
`[reply_to]` resolver currently surfaces only text. Surfacing
the referenced message's attachments doubles the download budget
question; defer until there's a concrete need.

### Outbound (agent → user)

The reverse direction is symmetric. `ChannelBase.handle_message`
returns an `AgentReply(text, files)` instead of plain text; each
file is an `OutboundFile(filename, mime, data)` with bytes ready
to ship. Two collection paths feed it:

1. **Inline `Part(inline_data=...)`** on `is_final_response()`
   events. The model can emit binary parts directly (native
   image generation, etc.). Filenames are synthesized as
   `agent_<n>.<ext>` from the mime type since Gemini Parts have
   no filename slot.

2. **Artifacts.** `ChannelBase` registers a
   `BaseArtifactService` (in-memory default; the Cloud Run /
   Vertex deploy in `app/fast_api_app.py` builds its own with
   `gs://…`) on the `Runner`. Any tool that calls
   `tool_context.save_artifact(filename, Part(...))` produces an
   `event.actions.artifact_delta` entry; after the run, channels
   load the saved bytes by filename + version. Filenames sourced
   here are sanitized at the channel boundary (path separators
   stripped, leading dots dropped, truncated to 100 chars).

Inline and artifact sources dedup by filename — if both surface
the same name, the inline copy wins (no double delivery).

Discord-specific delivery: files ride on the same
`message.reply(...)` / `channel.send(...)` call as the text via
`discord.File(fp=BytesIO(data), filename=...)`. Discord caps at
10 files per message; we batch overflow into follow-up
`channel.send` messages. Per-file size is capped by
`DISCORD_OUTBOUND_FILE_MAX_BYTES` (default 25 MB); oversize files
are dropped with a `(skipped …: too large)` line appended to the
reply so the agent can explain. A transient send failure (CDN
hiccup) falls back to text-only with a `(could not attach …)`
note rather than swallowing the whole reply.

## Transport-agnostic seam

The `ContextMessage(sender_id, text, sender_display=None)` dataclass
in `app/channels/base.py` is the contract. `_format_context()` and
`_format_reply_to()` in the same file own the wire formats. Future
Slack / Telegram channels plug into the same seam — they just need to
source `ContextMessage` instances from their own SDKs (Slack's
`conversations.history` and message `thread_ts` for replies,
Telegram's `getChat` history and `reply_to_message`, etc.), pass
them into `ChannelBase.handle_message`, and supply their own
`extra_instruction` describing the blocks they emit.

## What's not done

- **Cross-channel context.** Sessions remain per-channel. Bringing DM
  history into a guild channel (or vice versa) is out of scope.
- **Token-budget-aware truncation.** Hard count cap is good enough
  for the personal-bot scope.
- **Persisting context in the ADK session.** Each turn fetches /
  reads fresh; the session doesn't grow with backfill entries.
- **LRU eviction of channel buffers.** The buffer dict grows by one
  deque per channel ever observed. For a personal bot this is
  trivially small; if a deployment ever fans out across thousands of
  channels, swap the dict for an LRU map. Follow-up.
