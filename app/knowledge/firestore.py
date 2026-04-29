"""Firestore-backed knowledge service.

Used in stateless deployments (Agent Runtime, Cloud Run
without a persistent volume) where the workspace filesystem
is ephemeral. See `docs/knowledge.md` for the full design.

Document shape per slug:

    {
      "summary":    str,        # one-line, ~120 chars
      "content":    str,        # freeform markdown
      "created_at": Timestamp,  # immutable after creation
      "updated_at": Timestamp,  # advances on every write
    }

The doc id is the slug — Firestore enforces uniqueness for
us. Slug validation reuses the same regex as the Local
backend (`[a-z0-9][a-z0-9_-]*`) so the two backends are
swap-compatible byte-for-byte at the data-model level.

Index reads project only `summary` and `created_at` server-
side via `.select(...)`, so the `list_knowledge` call doesn't
pay for the full content of every entry.
"""

from __future__ import annotations

from datetime import datetime, timezone

from google.cloud.firestore import AsyncClient

from .base import BaseKnowledgeService, KnowledgeEntry, KnowledgeIndexEntry
from .local import _coerce_datetime, _validate_slug


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FirestoreKnowledgeBackend(BaseKnowledgeService):
    """Knowledge stored as documents in a Firestore collection.

    The client is constructed lazily on first use — Firestore's
    `AsyncClient` opens an event-loop-bound gRPC channel, so it
    must be constructed inside an event loop. Lazy init also
    means importing this module does not require GCP creds.
    """

    def __init__(self, *, project: str, collection: str) -> None:
        self._project = project
        self._collection_name = collection
        self._client: AsyncClient | None = None

    @property
    def collection_name(self) -> str:
        return self._collection_name

    @property
    def project(self) -> str:
        return self._project

    def _get_client(self) -> AsyncClient:
        if self._client is None:
            self._client = AsyncClient(project=self._project)
        return self._client

    def _collection(self):
        return self._get_client().collection(self._collection_name)

    async def list_knowledge(self) -> list[KnowledgeIndexEntry]:
        # Project only the fields we need so we don't pay for full
        # content on every index read.
        query = self._collection().select(["summary", "created_at"]).order_by(
            "created_at"
        )
        entries: list[KnowledgeIndexEntry] = []
        async for snap in query.stream():
            data = snap.to_dict() or {}
            try:
                created_at = _coerce_datetime(data.get("created_at"))
            except ValueError:
                created_at = None
            if created_at is None:
                # Skip malformed docs rather than blow up the index.
                # In normal operation, server-side order_by filters
                # docs without the field; this catches docs that
                # have it but with a bad value (admin console edit,
                # mid-flight schema migration, etc.).
                continue
            entries.append(
                KnowledgeIndexEntry(
                    slug=snap.id,
                    summary=data.get("summary", "") or "",
                    created_at=created_at,
                )
            )
        return entries

    async def read_knowledge(self, slug: str) -> KnowledgeEntry | None:
        _validate_slug(slug)
        snap = await self._collection().document(slug).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        try:
            created_at = _coerce_datetime(data.get("created_at"))
            updated_at = _coerce_datetime(data.get("updated_at"))
        except ValueError:
            return None
        if created_at is None:
            return None
        if updated_at is None:
            updated_at = created_at
        return KnowledgeEntry(
            slug=slug,
            summary=data.get("summary", "") or "",
            content=data.get("content", "") or "",
            created_at=created_at,
            updated_at=updated_at,
        )

    async def write_knowledge(
        self, slug: str, summary: str, content: str
    ) -> KnowledgeEntry:
        _validate_slug(slug)
        doc_ref = self._collection().document(slug)
        existing = await self.read_knowledge(slug)
        now = _now()
        created_at = existing.created_at if existing else now
        await doc_ref.set(
            {
                "summary": summary,
                "content": content,
                "created_at": created_at,
                "updated_at": now,
            }
        )
        return KnowledgeEntry(
            slug=slug,
            summary=summary,
            content=content,
            created_at=created_at,
            updated_at=now,
        )

    async def delete_knowledge(self, slug: str) -> bool:
        _validate_slug(slug)
        doc_ref = self._collection().document(slug)
        snap = await doc_ref.get()
        if not snap.exists:
            return False
        await doc_ref.delete()
        return True
