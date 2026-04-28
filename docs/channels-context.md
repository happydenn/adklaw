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

## Transport-agnostic seam

The `ContextMessage(sender_id, text, sender_display=None)` dataclass
in `app/channels/base.py` is the contract. `_format_context()` in the
same file owns the wire format. Future Slack / Telegram channels plug
into the same seam — they just need to source `ContextMessage` lists
from their own SDKs (Slack's `conversations.history`, Telegram's
`getChat` history, etc.) and pass them into `ChannelBase.handle_message`.

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
