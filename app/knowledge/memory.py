"""In-memory knowledge backend, used for tests."""

from __future__ import annotations

from datetime import datetime, timezone

from .base import BaseKnowledgeService, KnowledgeEntry, KnowledgeIndexEntry


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryKnowledgeBackend(BaseKnowledgeService):
    def __init__(self) -> None:
        self._entries: dict[str, KnowledgeEntry] = {}

    async def list_knowledge(self) -> list[KnowledgeIndexEntry]:
        return [
            KnowledgeIndexEntry(
                slug=e.slug, summary=e.summary, created_at=e.created_at
            )
            for e in sorted(
                self._entries.values(), key=lambda x: (x.created_at, x.slug)
            )
        ]

    async def read_knowledge(self, slug: str) -> KnowledgeEntry | None:
        return self._entries.get(slug)

    async def write_knowledge(
        self, slug: str, summary: str, content: str
    ) -> KnowledgeEntry:
        existing = self._entries.get(slug)
        now = _now()
        entry = KnowledgeEntry(
            slug=slug,
            summary=summary,
            content=content,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self._entries[slug] = entry
        return entry

    async def delete_knowledge(self, slug: str) -> bool:
        return self._entries.pop(slug, None) is not None
