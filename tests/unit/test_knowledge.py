"""Tests for the knowledge service: backends, tools, and prompt rendering."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.knowledge import (
    InMemoryKnowledgeBackend,
    KnowledgeIndexEntry,
    LocalKnowledgeBackend,
    render_index,
)
from app.knowledge.local import InvalidSlugError, _parse, _serialize
from app.knowledge.base import KnowledgeEntry


# ---------------------------------------------------------------------------
# LocalKnowledgeBackend round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_write_creates_directory_and_file(workspace_dir: Path) -> None:
    backend = LocalKnowledgeBackend(workspace_dir)
    assert not (workspace_dir / ".knowledge").exists()
    entry = await backend.write_knowledge(
        "papi-discord", "papi's Discord ID", "ID is 123."
    )
    assert (workspace_dir / ".knowledge" / "papi-discord.md").is_file()
    assert entry.summary == "papi's Discord ID"
    assert entry.content == "ID is 123."
    assert entry.created_at == entry.updated_at


@pytest.mark.asyncio
async def test_local_round_trip(workspace_dir: Path) -> None:
    backend = LocalKnowledgeBackend(workspace_dir)
    await backend.write_knowledge("foo", "the foo", "body of foo")
    got = await backend.read_knowledge("foo")
    assert got is not None
    assert got.slug == "foo"
    assert got.summary == "the foo"
    assert got.content == "body of foo"


@pytest.mark.asyncio
async def test_local_update_preserves_created_at(workspace_dir: Path) -> None:
    backend = LocalKnowledgeBackend(workspace_dir)
    first = await backend.write_knowledge("foo", "v1", "content1")
    # Advance time by writing again — implementation uses datetime.now,
    # so a tiny sleep guarantees updated_at advances.
    await asyncio.sleep(0.01)
    second = await backend.write_knowledge("foo", "v2", "content2")
    assert second.created_at == first.created_at
    assert second.updated_at > first.updated_at
    assert second.summary == "v2"
    assert second.content == "content2"


@pytest.mark.asyncio
async def test_local_read_missing_returns_none(workspace_dir: Path) -> None:
    backend = LocalKnowledgeBackend(workspace_dir)
    assert await backend.read_knowledge("nope") is None


@pytest.mark.asyncio
async def test_local_delete(workspace_dir: Path) -> None:
    backend = LocalKnowledgeBackend(workspace_dir)
    await backend.write_knowledge("foo", "x", "y")
    assert await backend.delete_knowledge("foo") is True
    assert await backend.read_knowledge("foo") is None
    assert await backend.delete_knowledge("foo") is False


# ---------------------------------------------------------------------------
# Index ordering and caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_index_sorted_by_created_at(workspace_dir: Path) -> None:
    backend = LocalKnowledgeBackend(workspace_dir)
    await backend.write_knowledge("zebra", "Z", "z")
    await asyncio.sleep(0.01)
    await backend.write_knowledge("apple", "A", "a")
    await asyncio.sleep(0.01)
    await backend.write_knowledge("banana", "B", "b")
    index = await backend.list_knowledge()
    # Append-only: oldest first regardless of slug alphabetics.
    assert [e.slug for e in index] == ["zebra", "apple", "banana"]


@pytest.mark.asyncio
async def test_local_index_empty_when_no_entries(workspace_dir: Path) -> None:
    backend = LocalKnowledgeBackend(workspace_dir)
    assert await backend.list_knowledge() == []


@pytest.mark.asyncio
async def test_local_index_picks_up_out_of_band_edits(
    workspace_dir: Path,
) -> None:
    """Mtime check should detect an external file write."""
    backend = LocalKnowledgeBackend(workspace_dir)
    await backend.write_knowledge("a", "A", "content a")
    first = await backend.list_knowledge()
    assert len(first) == 1
    # Drop a file via raw filesystem, simulating an out-of-band edit.
    raw = (
        "---\n"
        "summary: out of band\n"
        "created_at: 2026-01-01T00:00:00+00:00\n"
        "updated_at: 2026-01-01T00:00:00+00:00\n"
        "---\n"
        "content"
    )
    # Touch a future mtime so the cache invalidation kicks in even if
    # the test machine has poor filesystem time resolution.
    path = workspace_dir / ".knowledge" / "external.md"
    path.write_text(raw, encoding="utf-8")
    import os
    import time

    future = time.time() + 1
    os.utime(path, (future, future))
    second = await backend.list_knowledge()
    assert {e.slug for e in second} == {"a", "external"}


@pytest.mark.asyncio
async def test_local_skips_malformed_files(workspace_dir: Path) -> None:
    """A broken file shouldn't poison the whole index."""
    backend = LocalKnowledgeBackend(workspace_dir)
    await backend.write_knowledge("good", "ok", "content")
    (workspace_dir / ".knowledge" / "broken.md").write_text(
        "no frontmatter at all", encoding="utf-8"
    )
    index = await backend.list_knowledge()
    assert [e.slug for e in index] == ["good"]


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_invalid_slug_rejected(workspace_dir: Path) -> None:
    backend = LocalKnowledgeBackend(workspace_dir)
    for bad in ("", "Has-Caps", "with spaces", "/escape", "../traverse"):
        with pytest.raises(InvalidSlugError):
            await backend.write_knowledge(bad, "x", "y")


# ---------------------------------------------------------------------------
# Frontmatter serialize/parse
# ---------------------------------------------------------------------------


def test_serialize_and_parse_roundtrip() -> None:
    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    entry = KnowledgeEntry(
        slug="foo",
        summary="the foo",
        content="body line 1\nbody line 2",
        created_at=now,
        updated_at=now + timedelta(hours=1),
    )
    raw = _serialize(entry)
    parsed = _parse("foo", raw)
    assert parsed.slug == "foo"
    assert parsed.summary == "the foo"
    assert parsed.content == "body line 1\nbody line 2"
    assert parsed.created_at == entry.created_at
    assert parsed.updated_at == entry.updated_at


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inmemory_round_trip() -> None:
    backend = InMemoryKnowledgeBackend()
    await backend.write_knowledge("foo", "the foo", "content")
    got = await backend.read_knowledge("foo")
    assert got is not None
    assert got.summary == "the foo"
    index = await backend.list_knowledge()
    assert [e.slug for e in index] == ["foo"]
    assert await backend.delete_knowledge("foo") is True
    assert await backend.list_knowledge() == []


# ---------------------------------------------------------------------------
# Prompt index rendering
# ---------------------------------------------------------------------------


def test_render_index_empty_returns_empty_string() -> None:
    assert render_index([]) == ""


def test_render_index_basic() -> None:
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    out = render_index(
        [
            KnowledgeIndexEntry(slug="papi-discord", summary="papi's id", created_at=now),
            KnowledgeIndexEntry(slug="deploy-target", summary="agent runtime us-east1", created_at=now),
        ]
    )
    assert "## Knowledge index" in out
    assert "`papi-discord` — papi's id" in out
    assert "`deploy-target` — agent runtime us-east1" in out
    # No timestamps in the rendered string — see cache stability rules.
    assert "2026" not in out


def test_render_index_handles_missing_summary() -> None:
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    out = render_index(
        [KnowledgeIndexEntry(slug="orphan", summary="", created_at=now)]
    )
    assert "`orphan` — (no summary)" in out


# ---------------------------------------------------------------------------
# Tool wrappers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_write_then_read_then_list_then_delete(
    workspace_dir: Path,
) -> None:
    from app.tools import (
        delete_knowledge,
        list_knowledge,
        read_knowledge,
        write_knowledge,
    )

    written = await write_knowledge("foo", "summary of foo", "body of foo")
    assert written["status"] == "success"

    listed = await list_knowledge()
    assert listed["status"] == "success"
    slugs = [e["slug"] for e in listed["entries"]]
    assert slugs == ["foo"]

    got = await read_knowledge("foo")
    assert got["status"] == "success"
    assert got["content"] == "body of foo"

    removed = await delete_knowledge("foo")
    assert removed["status"] == "success"

    again = await read_knowledge("foo")
    assert again["status"] == "error"


@pytest.mark.asyncio
async def test_tool_read_unknown_slug_errors(workspace_dir: Path) -> None:
    from app.tools import read_knowledge

    res = await read_knowledge("does-not-exist")
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_tool_invalid_slug_errors_cleanly(workspace_dir: Path) -> None:
    from app.tools import write_knowledge

    res = await write_knowledge("Bad Slug!", "x", "y")
    assert res["status"] == "error"
    assert "invalid slug" in res["error"]


# ---------------------------------------------------------------------------
# Service singleton — backend selection via env
# ---------------------------------------------------------------------------


def test_service_defaults_to_local(
    workspace_dir: Path,
) -> None:
    from app.knowledge import get_knowledge_service

    service = get_knowledge_service()
    assert isinstance(service, LocalKnowledgeBackend)
    assert service.directory == workspace_dir / ".knowledge"


def test_service_unknown_backend_raises(
    workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.knowledge import get_knowledge_service

    monkeypatch.setenv("ADKLAW_KNOWLEDGE_BACKEND", "nonsense")
    with pytest.raises(ValueError):
        get_knowledge_service()


# ---------------------------------------------------------------------------
# Instruction provider integration — index appended at the tail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instruction_provider_appends_index_at_tail(
    workspace_dir: Path,
) -> None:
    """When the store has entries, the prompt ends with the index."""
    from unittest.mock import MagicMock

    from google.adk.agents.readonly_context import ReadonlyContext

    from app.agent import build_app
    from app.tools import write_knowledge

    await write_knowledge("entry-a", "first entry", "content a")

    app = build_app()
    rendered = await app.root_agent.instruction(
        MagicMock(spec=ReadonlyContext)
    )

    # Entry is in the prompt, and it appears last (tail position).
    assert "## Knowledge index" in rendered
    assert "`entry-a` — first entry" in rendered
    assert rendered.rstrip().endswith("`entry-a` — first entry")


@pytest.mark.asyncio
async def test_instruction_provider_omits_index_when_empty(
    workspace_dir: Path,
) -> None:
    """No knowledge entries → no index section in the prompt."""
    from unittest.mock import MagicMock

    from google.adk.agents.readonly_context import ReadonlyContext

    from app.agent import build_app

    app = build_app()
    rendered = await app.root_agent.instruction(
        MagicMock(spec=ReadonlyContext)
    )
    # The rendered-index section is identified by its lead-in
    # prose, which only appears when entries are actually listed.
    # We can't just check for `## Knowledge index` as a substring
    # because BASE_INSTRUCTION refers to that header in its docs.
    assert "Durable facts you have recorded" not in rendered
