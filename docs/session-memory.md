# Session memory (Tier 1)

How the agent's working memory works across turns and what
keeps it from blowing up over weeks of conversation.

## The problem

Every turn, ADK loads the full session event log and ships it
to the model as `contents`. Long-lived channels — Discord
guilds the bot has been in for months, a personal CLI
session that's been chatting daily — can accumulate thousands
of events. Gemini 3 has a 1M-token context window, but two
things degrade as you approach it:

1. **Token cost.** Every turn pays for the full transcript.
2. **Quality.** Models attend less reliably to the middle of
   very long contexts; relevant turns get drowned in
   ancient ones.

ADK ships two facilities for this — `events_compaction_config`
and `context_cache_config` on `App` — both off by default.
This PR turns both on.

## What we do

### Compaction

Configured on `app/agent.py:build_app` via
`EventsCompactionConfig`:

```python
EventsCompactionConfig(
    compaction_interval=10,
    overlap_size=3,
    token_threshold=700_000,
    event_retention_size=40,
)
```

What each knob means (from
`google.adk.apps.app.EventsCompactionConfig`):

- **`token_threshold=700_000`** — once a turn's prompt token
  count meets or exceeds this, ADK schedules a compaction
  pass *after* the invocation completes. 700k sits well
  under Gemini 3's 1M ceiling so we leave headroom for the
  next turn's response, tools, and any inline attachments.
- **`event_retention_size=40`** — when threshold-triggered
  compaction runs, it keeps the most recent 40 raw events
  un-summarized. This protects continuity at the tail (the
  agent still sees recent turns verbatim).
- **`compaction_interval=10`** — for the periodic
  invocation-count-based compaction, run every 10 new
  user-initiated invocations.
- **`overlap_size=3`** — successive compaction summaries
  overlap by 3 invocations to maintain context across the
  summary boundary.

What compaction *does* preserve: the agent's continuity. A
summarized window is collapsed into a model-generated
summary event; the agent still has access to "what happened"
in a compressed form. Recent turns are kept raw.

What compaction *does not* preserve: verbatim text from
collapsed turns, exact tool-call arguments, exact tool
results. If you need durable verbatim records, store them in
`.knowledge/` (Tier 3) — that's what knowledge is for.

### Context caching

Configured via `ContextCacheConfig`:

```python
ContextCacheConfig(
    cache_intervals=10,
    ttl_seconds=1800,
    min_tokens=4096,
)
```

What each knob means (from
`google.adk.agents.context_cache_config.ContextCacheConfig`):

- **`cache_intervals=10`** — the same cache entry serves up
  to 10 invocations before refreshing. Above 10, the cache
  is rebuilt on the next turn so it picks up new system
  prompt content (e.g., updated workspace markdown).
- **`ttl_seconds=1800`** (30 min) — cache entries expire 30
  minutes after creation regardless of usage.
- **`min_tokens=4096`** — only requests with at least ~4k
  tokens get cached. Small one-shot CLI runs aren't worth
  caching; cache-creation overhead can exceed the savings.

The cache key is the prompt prefix (system instruction +
tools + early `contents`). See `docs/instruction-layering.md`
for why we order our system prompt stable-prefix → volatile
tail — that ordering is what makes the cache actually hit
between turns.

### Manual session reset

If a session goes off the rails or accumulates content the
user wants to forget, deletion is the escape hatch. Routes:

- **CLI:** `agents-cli` doesn't expose a delete command yet.
  Direct sqlite: `sqlite3 .adklaw/sessions.db 'DELETE FROM
  sessions WHERE id = "<session_id>";'`. Inelegant — a
  proper CLI subcommand is a future quality-of-life add.
- **Discord:** no slash command yet. Same sqlite approach
  for now; a future `/reset` slash command is on the
  roadmap.

This is currently in the deferred-tooling category. Real
need has to materialize before we add UI.

## Verification

To confirm compaction kicks in, drive a session past 700k
tokens and check the session events table:

```bash
sqlite3 .adklaw/sessions.db \
  "SELECT id, count(*) FROM events GROUP BY session_id ORDER BY 2 DESC LIMIT 5;"
```

A session that's seen compaction will show *fewer* events
than turn count would suggest — collapsed events are
replaced by summary events.

To confirm caching, watch the request logs (or Gemini API
metrics if available) for `cachedContent` references on
follow-up turns. The first turn in a 30-minute window pays
full prompt cost; subsequent turns within `cache_intervals`
should land at the discounted cached rate.
