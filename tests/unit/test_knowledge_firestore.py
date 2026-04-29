"""Tests for `FirestoreKnowledgeBackend`.

Tests run against a hand-rolled in-memory fake that mirrors the
shape of `google.cloud.firestore.AsyncClient` (collection →
document → get/set/delete; collection.select(…).order_by(…).stream()).
This gives us coverage of the backend's logic — wire format,
projection, ordering, missing-doc handling — without requiring
a Firestore emulator (gcloud isn't available everywhere).

Production validation against real Firestore happens at deploy
time in PR 5, not in this test suite.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.knowledge.firestore import FirestoreKnowledgeBackend
from app.knowledge.local import InvalidSlugError


# ---------------------------------------------------------------------------
# Fakes — mirror the real AsyncClient surface area we use.
# ---------------------------------------------------------------------------


class _FakeSnapshot:
    def __init__(self, slug: str, data: dict | None) -> None:
        self.id = slug
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict | None:
        return None if self._data is None else dict(self._data)


class _FakeDocument:
    def __init__(self, parent: "_FakeCollection", slug: str) -> None:
        self._parent = parent
        self._slug = slug

    async def get(self) -> _FakeSnapshot:
        return _FakeSnapshot(self._slug, self._parent.docs.get(self._slug))

    async def set(self, data: dict) -> None:
        # Real Firestore accepts datetime values as Timestamps; we
        # just pass them through.
        self._parent.docs[self._slug] = dict(data)
        self._parent.writes.append(("set", self._slug, dict(data)))

    async def delete(self) -> None:
        self._parent.docs.pop(self._slug, None)
        self._parent.writes.append(("delete", self._slug, None))


class _FakeQuery:
    def __init__(
        self,
        collection: "_FakeCollection",
        projection: list[str] | None = None,
        order_by_field: str | None = None,
    ) -> None:
        self._collection = collection
        self._projection = projection
        self._order_by_field = order_by_field

    def select(self, fields: list[str]) -> "_FakeQuery":
        return _FakeQuery(self._collection, fields, self._order_by_field)

    def order_by(self, field: str) -> "_FakeQuery":
        return _FakeQuery(self._collection, self._projection, field)

    async def stream(self):
        items = list(self._collection.docs.items())
        if self._order_by_field:
            # Match real Firestore: order_by excludes docs without
            # the field server-side. Docs with non-comparable values
            # surface here so the backend's defensive coercion runs.
            items = [
                kv for kv in items if self._order_by_field in kv[1]
            ]
            items.sort(
                key=lambda kv: (
                    isinstance(kv[1][self._order_by_field], datetime),
                    (
                        kv[1][self._order_by_field]
                        if isinstance(
                            kv[1][self._order_by_field], datetime
                        )
                        else 0
                    ),
                )
            )
        for slug, full in items:
            data = (
                {k: full[k] for k in self._projection if k in full}
                if self._projection is not None
                else dict(full)
            )
            yield _FakeSnapshot(slug, data)


class _FakeCollection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.docs: dict[str, dict] = {}
        self.writes: list[tuple[str, str, dict | None]] = []

    def document(self, slug: str) -> _FakeDocument:
        return _FakeDocument(self, slug)

    def select(self, fields: list[str]) -> _FakeQuery:
        return _FakeQuery(self, fields, None)

    def order_by(self, field: str) -> _FakeQuery:
        return _FakeQuery(self, None, field)


class _FakeClient:
    def __init__(self, *, project: str) -> None:
        self.project = project
        self._collections: dict[str, _FakeCollection] = {}

    def collection(self, name: str) -> _FakeCollection:
        if name not in self._collections:
            self._collections[name] = _FakeCollection(name)
        return self._collections[name]


@pytest.fixture
def firestore_backend():
    """Build a backend whose internal client is the fake."""
    backend = FirestoreKnowledgeBackend(
        project="test-project", collection="test_knowledge"
    )
    fake = _FakeClient(project="test-project")
    backend._client = fake  # type: ignore[attr-defined]
    return backend, fake


# ---------------------------------------------------------------------------
# Round-trip + ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_firestore_write_then_read(firestore_backend):
    backend, _ = firestore_backend
    entry = await backend.write_knowledge("foo", "the foo", "body of foo")
    assert entry.slug == "foo"
    assert entry.summary == "the foo"
    assert entry.content == "body of foo"
    assert entry.created_at == entry.updated_at

    got = await backend.read_knowledge("foo")
    assert got is not None
    assert got.summary == "the foo"
    assert got.content == "body of foo"


@pytest.mark.asyncio
async def test_firestore_update_preserves_created_at(firestore_backend):
    backend, _ = firestore_backend
    first = await backend.write_knowledge("foo", "v1", "c1")
    import asyncio

    await asyncio.sleep(0.001)
    second = await backend.write_knowledge("foo", "v2", "c2")
    assert second.created_at == first.created_at
    assert second.updated_at > first.updated_at
    assert second.content == "c2"


@pytest.mark.asyncio
async def test_firestore_read_missing_returns_none(firestore_backend):
    backend, _ = firestore_backend
    assert await backend.read_knowledge("nope") is None


@pytest.mark.asyncio
async def test_firestore_delete(firestore_backend):
    backend, fake = firestore_backend
    await backend.write_knowledge("foo", "x", "y")
    assert await backend.delete_knowledge("foo") is True
    assert await backend.read_knowledge("foo") is None
    assert await backend.delete_knowledge("foo") is False
    # The second delete shouldn't issue an actual Firestore delete.
    delete_calls = [
        w for w in fake.collection("test_knowledge").writes if w[0] == "delete"
    ]
    assert len(delete_calls) == 1


@pytest.mark.asyncio
async def test_firestore_list_sorted_by_created_at(firestore_backend):
    backend, _ = firestore_backend
    import asyncio

    await backend.write_knowledge("zebra", "Z", "z")
    await asyncio.sleep(0.001)
    await backend.write_knowledge("apple", "A", "a")
    await asyncio.sleep(0.001)
    await backend.write_knowledge("banana", "B", "b")
    index = await backend.list_knowledge()
    # Append-only: oldest first regardless of slug alphabetics.
    assert [e.slug for e in index] == ["zebra", "apple", "banana"]


@pytest.mark.asyncio
async def test_firestore_list_empty(firestore_backend):
    backend, _ = firestore_backend
    assert await backend.list_knowledge() == []


# ---------------------------------------------------------------------------
# Wire format — verify the data we send Firestore matches our spec
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_firestore_write_sends_expected_fields(firestore_backend):
    backend, fake = firestore_backend
    await backend.write_knowledge("foo", "summary text", "content text")
    writes = fake.collection("test_knowledge").writes
    assert len(writes) == 1
    op, slug, data = writes[0]
    assert op == "set"
    assert slug == "foo"
    assert set(data.keys()) == {
        "summary",
        "content",
        "created_at",
        "updated_at",
    }
    assert data["summary"] == "summary text"
    assert data["content"] == "content text"
    assert isinstance(data["created_at"], datetime)
    assert isinstance(data["updated_at"], datetime)
    # We use timezone-aware datetimes so Firestore stores UTC, not
    # local time.
    assert data["created_at"].tzinfo is not None


@pytest.mark.asyncio
async def test_firestore_uses_configured_collection(firestore_backend):
    backend, fake = firestore_backend
    await backend.write_knowledge("foo", "x", "y")
    assert "test_knowledge" in fake._collections
    # No accidental writes to other collections.
    assert list(fake._collections.keys()) == ["test_knowledge"]


# ---------------------------------------------------------------------------
# Slug validation — same rules as Local
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_firestore_invalid_slug_rejected(firestore_backend):
    backend, _ = firestore_backend
    for bad in ("", "Has-Caps", "with spaces", "/escape", "../traverse"):
        with pytest.raises(InvalidSlugError):
            await backend.write_knowledge(bad, "x", "y")
        with pytest.raises(InvalidSlugError):
            await backend.read_knowledge(bad)
        with pytest.raises(InvalidSlugError):
            await backend.delete_knowledge(bad)


# ---------------------------------------------------------------------------
# Resilience — malformed documents shouldn't poison the index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_firestore_list_skips_docs_with_uncoercible_created_at(
    firestore_backend,
):
    """In real Firestore, `order_by('created_at')` excludes docs
    that don't have the field at all (server-side filter). The
    case the backend actually defends against is docs where the
    field is *present but bad* — e.g., an admin set it to a
    string in the console, or a mid-flight schema migration left
    a non-Timestamp value behind."""
    backend, fake = firestore_backend
    coll = fake.collection("test_knowledge")
    coll.docs["good"] = {
        "summary": "ok",
        "content": "x",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    coll.docs["broken"] = {
        "summary": "bad created_at value",
        "content": "x",
        "created_at": "not-a-date",
        "updated_at": "not-a-date",
    }
    index = await backend.list_knowledge()
    assert [e.slug for e in index] == ["good"]


@pytest.mark.asyncio
async def test_firestore_read_uncoercible_created_at_returns_none(
    firestore_backend,
):
    backend, fake = firestore_backend
    coll = fake.collection("test_knowledge")
    coll.docs["broken"] = {
        "summary": "x",
        "content": "y",
        "created_at": "not-a-date",
    }
    assert await backend.read_knowledge("broken") is None


# ---------------------------------------------------------------------------
# Backend selection via env vars
# ---------------------------------------------------------------------------


def test_service_uses_firestore_with_env_overrides(
    workspace_dir, monkeypatch
):
    """Env vars route us to Firestore with the configured project +
    collection. Lazy-construction of the gRPC client means we can
    verify selection without hitting Firestore."""
    from app.knowledge import get_knowledge_service

    monkeypatch.setenv("ADKLAW_KNOWLEDGE_BACKEND", "firestore")
    monkeypatch.setenv("ADKLAW_KNOWLEDGE_FIRESTORE_PROJECT", "my-proj")
    monkeypatch.setenv(
        "ADKLAW_KNOWLEDGE_FIRESTORE_COLLECTION", "my_coll"
    )

    service = get_knowledge_service()
    assert isinstance(service, FirestoreKnowledgeBackend)
    assert service.project == "my-proj"
    assert service.collection_name == "my_coll"


def test_service_falls_back_to_google_cloud_project(
    workspace_dir, monkeypatch
):
    from app.knowledge import get_knowledge_service

    monkeypatch.setenv("ADKLAW_KNOWLEDGE_BACKEND", "firestore")
    monkeypatch.delenv("ADKLAW_KNOWLEDGE_FIRESTORE_PROJECT", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "fallback-proj")

    service = get_knowledge_service()
    assert isinstance(service, FirestoreKnowledgeBackend)
    assert service.project == "fallback-proj"


def test_service_default_collection_when_unset(workspace_dir, monkeypatch):
    from app.knowledge import DEFAULT_FIRESTORE_COLLECTION, get_knowledge_service

    monkeypatch.setenv("ADKLAW_KNOWLEDGE_BACKEND", "firestore")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.delenv(
        "ADKLAW_KNOWLEDGE_FIRESTORE_COLLECTION", raising=False
    )

    service = get_knowledge_service()
    assert service.collection_name == DEFAULT_FIRESTORE_COLLECTION


def test_service_firestore_without_project_raises(
    workspace_dir, monkeypatch
):
    """Misconfiguration surfaces with a clear message rather than
    a cryptic gRPC error at first use."""
    from app.knowledge import get_knowledge_service

    monkeypatch.setenv("ADKLAW_KNOWLEDGE_BACKEND", "firestore")
    monkeypatch.delenv("ADKLAW_KNOWLEDGE_FIRESTORE_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    with pytest.raises(ValueError, match="GCP project"):
        get_knowledge_service()


# ---------------------------------------------------------------------------
# Lazy gRPC client construction
# ---------------------------------------------------------------------------


def test_firestore_client_constructed_lazily():
    """Constructing the backend doesn't open a gRPC channel —
    that happens on first method call. This matters because the
    real `AsyncClient` requires an event loop."""
    backend = FirestoreKnowledgeBackend(
        project="p", collection="c"
    )
    assert backend._client is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_firestore_client_uses_configured_project():
    """When the real client is constructed, it gets our project."""
    backend = FirestoreKnowledgeBackend(
        project="my-test-project", collection="c"
    )
    captured = {}

    class _SpyClient:
        def __init__(self, *, project):
            captured["project"] = project

        def collection(self, name):
            class _Empty:
                def select(self, fields):
                    class _Q:
                        def order_by(self, f):
                            return self

                        async def stream(self):
                            return
                            yield  # pragma: no cover

                    return _Q()

            return _Empty()

    with patch(
        "app.knowledge.firestore.AsyncClient", _SpyClient
    ):
        await backend.list_knowledge()

    assert captured["project"] == "my-test-project"
